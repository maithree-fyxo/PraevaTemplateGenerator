"""
Ezekia integration layer.

This is the ONLY module that knows about Ezekia. It:
  1. extracts an assignment/project id from a pasted Ezekia URL
  2. fetches the project + candidates (+ contacts) from the Ezekia API
  3. maps the raw Ezekia payload into our internal `Assignment` model

Built against the live Ezekia OpenAPI spec (OAS 3.0, https://ezekia.com/api).

Key endpoints
-------------
  GET /v4/projects/{id}                      -> { data: project }
  GET /v4/projects/{id}/candidates           -> { data: [person] }
       ?fieldsWithCandidate[]=profile.positions
       &fieldsWithCandidate[]=profile.education
       &fieldsWithCandidate[]=profile.confidential
       &fieldsWithCandidate[]=profile.currentStatus
       &fieldsWithCandidate[]=profile.aspirations
     -> each person embeds profile.* (career, education, salary, location)
  GET /projects/{id}/candidates?fields=meta.candidate   (NON-v4)
     -> each person embeds meta.candidate.pipelineTags (the routing tags).
        Per Ezekia support, pipeline/status tags are ONLY returned by the
        non-versioned endpoint with the singular `fields=meta.candidate`
        parameter — the v4 endpoint + fieldsWithCandidate[] does NOT return
        them. We therefore fetch tags from this endpoint and merge them into
        the rich v4 profile records by candidate id.
  GET /v4/projects/{id}/contacts             -> { data: [person] }  (Prepared for)

Auth: Bearer token (set EZEKIA_TOKEN). Demo mode (mock data) is used when no
token is set or EZEKIA_USE_MOCK=1.

NOTE ON STAGE TAXONOMY
----------------------
Which section a candidate lands in (Engaged / Pipeline / Target / Discounted)
and whether they get a full profile slide is driven by their Ezekia *pipeline
tags* (meta.candidateInfo.pipelineTags[].text). Those tag names are specific to
each search firm's pipeline. The defaults in STAGE_TAGS below are seeded from
the sample deck's status labels — confirm/adjust them against a real assignment
(see README > Stage taxonomy).
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import httpx

from . import config
from .models import Assignment, Candidate, CareerEntry, Stage
from .mock_data import mock_assignment

# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
BASE_URL = os.getenv("EZEKIA_BASE_URL", "https://ezekia.com/api")


def use_mock() -> bool:
    """Demo mode: forced by env, or whenever no token is configured."""
    return config.use_mock()

# Profile fields to embed on the v4 candidates call (career, education, salary,
# location). The v4 endpoint returns these richly via fieldsWithCandidate[].
PROFILE_INCLUDES = [
    "profile.positions",
    "profile.education",
    "profile.confidential",
    "profile.currentStatus",
    "profile.aspirations",
]

# Pipeline/status tags come from the NON-versioned endpoint with the singular
# `fields=meta.candidate` parameter (confirmed against Ezekia support docs).
TAG_FIELDS_PARAM = ("fields", "meta.candidate")

# CONFIRMED via diagnose probe: the /v4 candidates LIST endpoint returns ONLY
# profile.positions and ignores every other field spelling for the other blocks.
# The v4 PERSON DETAIL endpoint DOES return them — but only ONE block per request
# (confirmed with Maithree): grouping several `fields` collapses to just one. So
# we fetch each block as its own call:
#   GET /v4/people/{id}?fields=profile.confidential    (salary, notice)
#   GET /v4/people/{id}?fields=profile.currentStatus   (location)
#   GET /v4/people/{id}?fields=profile.education
# This is only needed for FULL-PROFILE candidates (Engaged + Praeva-Discounted);
# table rows use role/company from the positions already in the list response.
PERSON_PROFILE_FIELDS = [
    "profile.confidential",
    "profile.currentStatus",
    "profile.education",
]

# Back-compat alias (diagnose + any external refs).
CANDIDATE_INCLUDES = PROFILE_INCLUDES

# How each Ezekia pipeline tag routes into the report.
#
# Candidates carry MULTIPLE tags (e.g. "Identified" + "Phone Interview" +
# "Not Interested"), so routing is by PRIORITY: the highest-priority tag a
# candidate holds decides their section. Tag names are matched case-insensitively.
#
# Priority policy = EXIT-WINS (confirmed with Praeva): a negative/exit tag
# overrides any earlier progress, so a candidate who was interviewed but then
# declined shows in Discounted, not Engaged. The Discounted block therefore sits
# ABOVE the progress block below. Within progress, the most-advanced stage wins.
#
# Each entry: (exact tag text, Stage, has_profile)
#   has_profile=True  -> full profile format (2 per page)
#   has_profile=False -> list-table format
# EDIT this ordered list if Praeva renames or re-prioritises its pipeline stages.
TAG_ROUTING: List[tuple[str, Stage, bool]] = [
    # --- exits (win over progress) ---
    ("Praeva - Discounted", Stage.DISCOUNTED, True),   # Praeva-side discount -> profile
    ("Not Interested",      Stage.DISCOUNTED, False),  # candidate declined   -> table
    # --- progress (most-advanced first) ---
    ("Praeva Interview",    Stage.ENGAGED,   True),
    ("Phone Interview",     Stage.ENGAGED,   True),
    ("In Discussion",       Stage.PIPELINE,  False),
]
# Lower-cased lookup + explicit priority index (position in the list above).
_TAG_ROUTE_BY_TEXT: Dict[str, tuple[int, Stage, bool]] = {
    text.strip().lower(): (i, stage, has_profile)
    for i, (text, stage, has_profile) in enumerate(TAG_ROUTING)
}
# SUPPRESS tags: holding ANY of these removes the candidate from the deck
# entirely, even if they also carry a routed tag (per Praeva — these people
# should never surface). Terminal exits + the advanced Client-Interview stage
# they chose not to show.
SUPPRESS_TAGS = {
    "client interview",
    "stood down - praeva",
    "withdrew",
}
# IGNORE tags (neutral, no effect on routing): Tier 2, Leave, Source,
# Contact - No reply, Identified (the base longlist tag on all candidates).
# A candidate carrying only these (and no routed tag) is excluded by default.
# The Target section is filled manually (placeholders), so no tag routes there.

CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$", "CAD": "C$"}


class EzekiaError(Exception):
    pass


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #
def extract_assignment_id(url: str) -> str:
    """Pull the project/assignment id from a pasted Ezekia URL.

    Handles e.g.:
        https://ezekia.com/#/assignments/907468/info   -> 907468
        https://ezekia.com/#/projects/907468           -> 907468
        907468                                          -> 907468
    """
    url = (url or "").strip()
    if not url:
        raise EzekiaError("No URL provided.")
    if url.isdigit():
        return url
    m = re.search(r"/(?:assignments?|projects?|searches?|opportunities?)/(\d+)", url)
    if m:
        return m.group(1)
    nums = re.findall(r"(\d{3,})", url)
    if nums:
        return nums[-1]
    raise EzekiaError(f"Could not find an assignment id in URL: {url!r}")


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
class EzekiaClient:
    def __init__(self, token: Optional[str] = None, base_url: str = BASE_URL, timeout: float = 45.0):
        self.token = token if token is not None else config.get_ezekia_token()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def fetch_raw(self, assignment_id: str) -> Dict[str, Any]:
        profile_params = [("fieldsWithCandidate[]", f) for f in PROFILE_INCLUDES]
        profile_params.append(("count", "500"))
        tag_params = [TAG_FIELDS_PARAM, ("count", "500")]
        with httpx.Client(timeout=self.timeout, headers=self._headers()) as client:
            project = self._get(client, f"/v4/projects/{assignment_id}").get("data", {})

            # 1) base records from the v4 list endpoint (positions + addresses + id)
            candidates = self._get(
                client, f"/v4/projects/{assignment_id}/candidates", params=profile_params
            ).get("data", []) or []

            # 2) pipeline tags from the documented non-v4 endpoint, merged by id
            tagged = []
            try:
                tagged = self._get(
                    client, f"/projects/{assignment_id}/candidates", params=tag_params
                ).get("data", []) or []
            except EzekiaError:
                tagged = []
            candidates = _merge_meta(candidates, tagged)

            # 3) enrich ONLY the full-profile candidates with the blocks the list
            #    endpoint omits (location/salary/notice/education), via per-person
            #    detail calls (one field per call, run concurrently).
            _enrich_profile_candidates(client, self.base_url, candidates)

            try:
                contacts = self._get(client, f"/v4/projects/{assignment_id}/contacts").get("data", [])
            except EzekiaError:
                contacts = []
        return {"project": project, "candidates": candidates, "contacts": contacts}

    def _get(self, client: "httpx.Client", path: str, params=None) -> Any:
        resp = client.get(f"{self.base_url}{path}", params=params)
        if resp.status_code == 401:
            raise EzekiaError("Ezekia authentication failed — check EZEKIA_TOKEN.")
        if resp.status_code == 403:
            raise EzekiaError(
                "Ezekia returned 403 (unauthorized) — this token's user cannot "
                "access this assignment. Confirm the token belongs to an account "
                "with access to this project."
            )
        if resp.status_code == 404:
            raise EzekiaError(f"Ezekia resource not found: {path}")
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _merge_meta(profiles: List[Dict[str, Any]], tagged: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge `meta` (pipeline tags) from the non-v4 tag call into the rich v4
    profile records, keyed by candidate id. If the profile call came back empty
    for some reason, fall back to the tagged records so routing can still run."""
    if not profiles:
        return tagged
    meta_by_id: Dict[Any, Dict[str, Any]] = {}
    for t in tagged:
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        meta = t.get("meta")
        if tid is not None and isinstance(meta, dict) and meta:
            meta_by_id[tid] = meta
    for c in profiles:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        incoming = meta_by_id.get(cid)
        if incoming:
            existing = c.get("meta")
            # keep anything already present; layer the tag meta underneath
            c["meta"] = {**incoming, **existing} if isinstance(existing, dict) else incoming
    return profiles


def _merge_profile_block(candidate: Dict[str, Any], block: Dict[str, Any]) -> None:
    """Gap-fill a candidate's profile with sub-blocks from a person-detail call."""
    if not block:
        return
    prof = candidate.get("profile")
    if not isinstance(prof, dict):
        prof = {}
        candidate["profile"] = prof
    for k, v in block.items():
        if v not in (None, "", [], {}) and prof.get(k) in (None, "", [], {}):
            prof[k] = v


def _person_id(candidate: Dict[str, Any]):
    for key in ("id", "personId", "person_id"):
        v = candidate.get(key)
        if v not in (None, ""):
            return v
    return None


def _enrich_profile_candidates(client, base_url: str, candidates: List[Dict[str, Any]],
                               limit: Optional[int] = None) -> Dict[str, Any]:
    """For each FULL-PROFILE candidate, fetch profile.confidential /
    currentStatus / education from the v4 person endpoint (one field per call,
    concurrently) and merge into the record. Returns a small summary for
    diagnostics. Best-effort: failures leave the field blank, never raise."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    targets = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        route = _candidate_route(c)
        if route is not None and route[2] and _person_id(c) is not None:
            targets.append(c)
    if limit is not None:
        targets = targets[:limit]

    summary = {"profile_candidates": len(targets), "calls": 0, "errors": 0}
    if not targets:
        return summary

    by_id = {_person_id(c): c for c in targets}

    def fetch(pid, field):
        try:
            r = client.get(f"{base_url}/v4/people/{pid}", params=[("fields", field)])
            if r.status_code == 200:
                return pid, ((r.json() or {}).get("data", {}) or {}).get("profile") or {}
            return pid, None
        except Exception:
            return pid, None

    tasks = [(pid, field) for pid in by_id for field in PERSON_PROFILE_FIELDS]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(fetch, pid, field) for pid, field in tasks]
        for fut in as_completed(futures):
            summary["calls"] += 1
            pid, block = fut.result()
            if block is None:
                summary["errors"] += 1
                continue
            cand = by_id.get(pid)
            if cand is not None:
                _merge_profile_block(cand, block)
    return summary


def _g(d: Any, *keys, default=None):
    """Safe nested get: _g(obj, 'a', 'b') -> obj['a']['b'] or default."""
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur and cur[k] is not None:
            cur = cur[k]
        else:
            return default
    return cur


def _year(val: Any) -> str:
    if val is None or val == "":
        return ""
    s = str(val)
    m = re.search(r"(\d{4})", s)
    return m.group(1) if m else s


def _currency_symbol(currency: Any) -> str:
    if isinstance(currency, dict):
        if currency.get("symbol"):
            return currency["symbol"]
        code = (currency.get("code") or "").upper()
        return CURRENCY_SYMBOLS.get(code, code + " " if code else "")
    if isinstance(currency, str):
        return CURRENCY_SYMBOLS.get(currency.upper(), currency)
    return ""


# --------------------------------------------------------------------------- #
# mapping raw Ezekia -> internal model
# --------------------------------------------------------------------------- #
def map_assignment(raw: Dict[str, Any]) -> Assignment:
    project = raw.get("project", {}) or {}
    raw_candidates = raw.get("candidates", []) or []
    raw_contacts = raw.get("contacts", []) or []

    client_name = _client_name(project)
    assignment = Assignment(
        name=client_name,
        title=f"Search Update – {client_name}" if client_name else (project.get("title") or "Search Update"),
        prepared_for=_prepared_for(raw_contacts),
        date="",  # report date is chosen at generation time; left blank to fill in deck
    )
    for rc in raw_candidates:
        cand = _map_candidate(rc)
        if cand is not None:  # None = tags don't route into the report
            assignment.candidates.append(cand)
    # order profiles/tables by candidate rank when available
    assignment.candidates.sort(key=lambda c: c.__dict__.get("_rank", 1_000_000))
    return assignment


def _client_name(project: Dict[str, Any]) -> str:
    """The client company shown on the cover.

    project.default has no single guaranteed 'company' field, so try the most
    likely places. CONFIRM this against a real assignment on first live run.
    """
    for path in (("relationships", "company", "name"),
                 ("relationships", "client", "name"),
                 ("relationships", "company", "data", "name"),
                 ("company", "name"), ("company",),
                 ("name",), ("label",), ("projectId",), ("title",)):
        v = _g_path(project, path)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _g_path(d: Any, path):
    """Like _g but supports int indices for lists in the path."""
    cur = d
    for k in path:
        if isinstance(k, int):
            if isinstance(cur, list) and 0 <= k < len(cur):
                cur = cur[k]
            else:
                return None
        elif isinstance(cur, dict) and k in cur and cur[k] is not None:
            cur = cur[k]
        else:
            return None
    return cur


def _prepared_for(contacts: List[Dict[str, Any]]) -> List[str]:
    names = [c.get("name") or c.get("fullName") for c in contacts]
    names = [n for n in names if n]
    return [f"Prepared for: {', '.join(names)}"] if names else []


def _candidate_route(person: Dict[str, Any]):
    """Return (stage, status_label, has_profile) for the candidate's HIGHEST-
    priority pipeline tag, or None if they hold no routed tag (excluded).

    Candidates carry several tags at once; we pick the one with the lowest
    priority index in TAG_ROUTING (exit tags rank above progress tags)."""
    # v4/non-v4 both expose pipeline info under meta.candidate. Fall back to the
    # older meta.candidateInfo path just in case.
    tags = (_g(person, "meta", "candidate", "pipelineTags", default=None)
            or _g(person, "meta", "candidateInfo", "pipelineTags", default=[]) or [])
    texts = [t.get("text", "") for t in tags if isinstance(t, dict) and t.get("text")]
    # A suppress tag removes the candidate entirely, overriding any routed tag.
    if any(txt.strip().lower() in SUPPRESS_TAGS for txt in texts):
        return None
    best = None  # (priority_index, stage, label, has_profile)
    for txt in texts:
        route = _TAG_ROUTE_BY_TEXT.get(txt.strip().lower())
        if route is not None:
            idx, stage, has_profile = route
            if best is None or idx < best[0]:
                best = (idx, stage, txt.strip(), has_profile)
    if best is None:
        return None
    _, stage, label, has_profile = best
    return stage, label, has_profile


def _map_candidate(person: Dict[str, Any]) -> Optional[Candidate]:
    route = _candidate_route(person)
    if route is None:
        return None
    stage, status_label, has_profile = route
    profile = person.get("profile", {}) or {}
    positions = profile.get("positions", []) or []
    current = positions[0] if positions else {}

    cand = Candidate(
        name=person.get("name") or person.get("fullName")
        or f"{person.get('firstName','') or ''} {person.get('lastName','') or ''}".strip(),
        stage=stage,
        role=current.get("title") or "",
        company=_position_company(current),
        status=status_label,
        has_profile=has_profile,
        salary=_salary(profile),
        location=_location(person, profile),
        availability=_availability(profile),
        education=_education(profile.get("education", [])),
        career=_career(positions),
    )
    cand.__dict__["_rank"] = (_g(person, "meta", "candidate", "rank", default=None)
                              if _g(person, "meta", "candidate", "rank", default=None) is not None
                              else _g(person, "meta", "candidateInfo", "rank", default=1_000_000))
    return cand


def _position_company(pos: Dict[str, Any]) -> str:
    comp = pos.get("company")
    if isinstance(comp, dict):
        return comp.get("name") or ""
    if isinstance(comp, str):
        return comp
    return ""


def _career(positions: List[Dict[str, Any]]) -> List[CareerEntry]:
    out = []
    for p in positions:
        start = _year(p.get("startDate"))
        end = _year(p.get("endDate"))
        # Ezekia encodes an open-ended (current) role as year 9999 -> show "P".
        if end == "9999":
            end = "P"
        elif not end and (p.get("tense") or p.get("primary")):
            end = "P"
        dates = f"{start} - {end}".strip(" -") if (start or end) else ""
        out.append(CareerEntry(
            company=_position_company(p),
            role=p.get("title") or "",
            dates=dates,
        ))
    return out


def _education(edu: List[Dict[str, Any]]) -> str:
    lines = []
    for e in edu:
        school = e.get("school") or ""
        start = _year(e.get("startDate"))
        end = _year(e.get("endDate"))
        years = f"{start} - {end}".strip(" -") if (start or end) else ""
        lines.append(f"{school}\t{years}".rstrip("\t "))
        deg = ", ".join(x for x in (e.get("degree"), e.get("field")) if x)
        if deg:
            lines.append(deg)
    return "\n".join(lines)


def _salary(profile: Dict[str, Any]) -> str:
    conf = profile.get("confidential", {}) or {}
    base = _g(conf, "permanent", "baseSalary")
    parts = []
    if isinstance(base, dict) and base.get("amount"):
        sym = _currency_symbol(base.get("currency"))
        parts.append(f"{sym}{int(base['amount']):,} base")
    bonus = _g(conf, "permanent", "bonus") or conf.get("bonus")
    if bonus:
        parts.append(f"{bonus}% bonus")
    return ", ".join(parts)


def _loc_str(d: Any) -> str:
    """Render a location value (string or dict) to a display string.
    Handles both a named location ({name:...}) and a structured address
    ({city, region, country, ...}) -> "City, Country"."""
    if isinstance(d, str):
        return d.strip()
    if isinstance(d, dict):
        for key in ("name", "formatted", "label", "displayName", "fullName"):
            v = d.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # compose from structured parts
        parts = [d.get("city") or d.get("town"),
                 d.get("region") or d.get("state") or d.get("county"),
                 d.get("country") or d.get("countryName")]
        parts = [str(p).strip() for p in parts if p and str(p).strip()]
        # de-dupe while preserving order (e.g. city == region)
        seen, uniq = set(), []
        for p in parts:
            if p.lower() not in seen:
                seen.add(p.lower()); uniq.append(p)
        return ", ".join(uniq)
    return ""


def _first_loc(seq: Any) -> str:
    if isinstance(seq, list):
        for item in seq:
            s = _loc_str(item)
            if s:
                return s
    return ""


def _location(person: Dict[str, Any], profile: Dict[str, Any]) -> str:
    """Current location, tried across the shapes Ezekia uses. Order favours
    the person's CURRENT location over aspirational (desired) locations."""
    # 1) currentStatus.locations[] (array) or singular currentStatus.location
    s = _first_loc(_g(profile, "currentStatus", "locations", default=[]))
    if s:
        return s
    s = _loc_str(_g(profile, "currentStatus", "location", default=None))
    if s:
        return s
    # 2) person-level addresses[] (top-level on the candidate record)
    s = _first_loc(person.get("addresses"))
    if s:
        return s
    # 3) location on the current (first) position
    positions = _g(profile, "positions", default=[]) or []
    if positions and isinstance(positions[0], dict):
        s = _loc_str(positions[0].get("location"))
        if s:
            return s
    # 4) aspirational locations (where they WANT to be) — last resort
    s = _first_loc(_g(profile, "aspirations", "locations", default=[]))
    return s


def _availability(profile: Dict[str, Any]) -> str:
    notice = _g(profile, "confidential", "notice")
    if notice is None or notice == "":
        return ""
    try:
        n = int(notice)
    except (ValueError, TypeError):
        return str(notice)
    if n <= 0:
        return "Immediately available"
    return f"{n} month" + ("s" if n != 1 else "")


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def test_token() -> Dict[str, Any]:
    """Lightweight check that the configured token authenticates.
    Returns {ok, status, detail}. Does not expose the token."""
    token = config.get_ezekia_token()
    if not token:
        return {"ok": False, "status": None, "detail": "No token configured."}
    try:
        with httpx.Client(timeout=20.0, headers={"Authorization": f"Bearer {token}",
                                                 "Accept": "application/json"}) as client:
            r = client.get(f"{BASE_URL}/v4/projects", params={"count": 1})
        if r.status_code == 200:
            return {"ok": True, "status": 200, "detail": "Token authenticates."}
        if r.status_code == 401:
            return {"ok": False, "status": 401, "detail": "Token rejected (401) — check the key."}
        if r.status_code == 403:
            return {"ok": False, "status": 403, "detail": "Authenticated but not authorized (403)."}
        return {"ok": False, "status": r.status_code, "detail": f"Unexpected status {r.status_code}."}
    except Exception as e:  # pragma: no cover
        return {"ok": False, "status": None, "detail": f"Connection error: {e}"}


def _key_paths(obj: Any, prefix: str = "", out=None, cap: int = 500):
    """List nested KEY PATHS of an object (keys only, no values -> no PII)."""
    if out is None:
        out = []
    if len(out) >= cap:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            out.append(p)
            if isinstance(v, (dict, list)):
                _key_paths(v, p, out, cap)
    elif isinstance(obj, list) and obj:
        _key_paths(obj[0], prefix + "[]", out, cap)
    return out


# candidate-specific include names to probe for where pipeline tags live
_TAG_FIELD_GUESSES = [
    "meta.candidateInfo", "meta.candidate", "meta", "candidateInfo",
    "candidate", "pipelineTags", "pipeline", "statuses", "status", "tags",
]


def _leaf_paths(obj: Any, prefix: str = "", out=None, cap: int = 4000):
    """Yield (path, value) for every scalar leaf, collapsing list indices to []."""
    if out is None:
        out = []
    if len(out) >= cap:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                _leaf_paths(v, p, out, cap)
            else:
                out.append((p, v))
    elif isinstance(obj, list):
        for item in obj:
            p = f"{prefix}[]"
            if isinstance(item, (dict, list)):
                _leaf_paths(item, p, out, cap)
            else:
                out.append((p, item))
    return out


def _profile_field_probe(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Find where location / salary / notice actually live. Reports, per
    candidate-record leaf path matching those keywords, how many records HAVE
    the path and how many have a NON-EMPTY value. Values themselves are not
    returned — only paths and counts — so no personal data leaves the server."""
    KW = ("location", "city", "town", "country", "region", "state", "address",
          "salary", "compensation", "remuneration", "package", "bonus",
          "notice", "availab")
    present: Dict[str, int] = {}
    nonempty: Dict[str, int] = {}
    profile_key_presence = {k: 0 for k in
                            ("positions", "currentStatus", "confidential",
                             "aspirations", "education")}
    for it in items:
        if not isinstance(it, dict):
            continue
        prof = it.get("profile") or {}
        for k in profile_key_presence:
            v = prof.get(k)
            if v not in (None, "", [], {}):
                profile_key_presence[k] += 1
        # walk the whole record (profile + top-level addresses etc.)
        for path, val in _leaf_paths({"profile": prof,
                                      "addresses": it.get("addresses")}):
            low = path.lower()
            if any(k in low for k in KW):
                present[path] = present.get(path, 0) + 1
                if val not in (None, "", [], {}):
                    nonempty[path] = nonempty.get(path, 0) + 1
    # keep only paths that are non-empty for at least one candidate, sorted by fill
    filled = sorted(((p, nonempty.get(p, 0), present.get(p, 0))
                     for p in present if nonempty.get(p, 0) > 0),
                    key=lambda x: (-x[1], x[0]))
    return {
        "profile_blocks_nonempty": profile_key_presence,
        "location_salary_notice_paths": [
            {"path": p, "nonempty": ne, "present": pr} for p, ne, pr in filled
        ][:40],
    }


def diagnose(url: str) -> Dict[str, Any]:
    """Report the STRUCTURE of what Ezekia returns for a URL, to debug empty
    results. Safe: returns statuses, key names, counts and pipeline-tag texts —
    no candidate names or personal data."""
    out: Dict[str, Any] = {"mock_mode": use_mock()}
    if use_mock():
        out["note"] = "App is in demo mode (no token). Set a token to hit live Ezekia."
        return out
    try:
        assignment_id = extract_assignment_id(url)
    except EzekiaError as e:
        out["error"] = str(e)
        return out
    out["assignment_id"] = assignment_id

    token = config.get_ezekia_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    params = [("fieldsWithCandidate[]", f) for f in PROFILE_INCLUDES] + [("count", "500")]

    with httpx.Client(timeout=45.0, headers=headers) as client:
        # --- project ---
        pr = client.get(f"{BASE_URL}/v4/projects/{assignment_id}")
        proj_info: Dict[str, Any] = {"http_status": pr.status_code}
        if pr.status_code == 200:
            body = pr.json()
            data = body.get("data", body)
            data = data if isinstance(data, dict) else {}
            proj_info["data_keys"] = list(data.keys())
            proj_info["name_value"] = data.get("name")
            proj_info["relationships_keys"] = list(_g(data, "relationships", default={}).keys()) if isinstance(_g(data, "relationships"), dict) else None
            proj_info["relationship_paths"] = [p for p in _key_paths(_g(data, "relationships", default={})) if "name" in p.lower()][:20]
            proj_info["client_name_detected"] = _client_name(data)
        else:
            proj_info["body_snippet"] = pr.text[:200]
        out["project"] = proj_info

        # --- pipeline tags via the DOCUMENTED non-v4 endpoint --------------- #
        # Ezekia support: GET /projects/{id}/candidates?fields=meta.candidate
        # This is the ONLY endpoint/param combination that returns pipeline tags.
        tag_check: Dict[str, Any] = {}
        meta_by_id: Dict[Any, Dict[str, Any]] = {}
        try:
            tr = client.get(
                f"{BASE_URL}/projects/{assignment_id}/candidates",
                params=[TAG_FIELDS_PARAM, ("count", "500")],
            )
            tag_check["endpoint"] = f"/projects/{assignment_id}/candidates?fields=meta.candidate"
            tag_check["http_status"] = tr.status_code
            if tr.status_code == 200:
                titems = (tr.json() or {}).get("data", []) or []
                tag_check["raw_count"] = len(titems)
                tag_check["any_meta"] = any(isinstance(t, dict) and t.get("meta") for t in titems)
                tcensus: Dict[str, int] = {}
                for t in titems:
                    tid = t.get("id") if isinstance(t, dict) else None
                    m = t.get("meta") if isinstance(t, dict) else None
                    if tid is not None and isinstance(m, dict) and m:
                        meta_by_id[tid] = m
                    tags = (_g(t, "meta", "candidate", "pipelineTags")
                            or _g(t, "meta", "candidateInfo", "pipelineTags") or [])
                    for tag in tags:
                        if isinstance(tag, dict) and tag.get("text"):
                            tcensus[tag["text"]] = tcensus.get(tag["text"], 0) + 1
                tag_check["pipeline_tags_seen"] = tcensus
                # show where meta sits on the first tagged record
                for t in titems:
                    if isinstance(t, dict) and isinstance(t.get("meta"), dict) and t["meta"]:
                        tag_check["sample_meta_keys"] = list(t["meta"].keys())
                        cand_meta = _g(t, "meta", "candidate")
                        if isinstance(cand_meta, dict):
                            tag_check["sample_candidate_keys"] = list(cand_meta.keys())
                        break
            else:
                tag_check["body_snippet"] = tr.text[:200]
        except Exception as e:  # pragma: no cover
            tag_check["error"] = str(e)[:120]
        out["tag_endpoint_check"] = tag_check

        # --- candidates (rich v4 profiles, merged with tags) --------------- #
        cr = client.get(f"{BASE_URL}/v4/projects/{assignment_id}/candidates", params=params)
        cand_info: Dict[str, Any] = {"http_status": cr.status_code, "params_sent": [p[0]+"="+p[1] for p in params]}
        if cr.status_code == 200:
            body = cr.json()
            cand_info["envelope_keys"] = list(body.keys()) if isinstance(body, dict) else type(body).__name__
            data = body.get("data", body) if isinstance(body, dict) else body
            items = data if isinstance(data, list) else []
            # merge pipeline tags, then enrich a SAMPLE of profile candidates via
            # the v4 person endpoint (one field per call), exactly like production
            items = _merge_meta(items, [{"id": k, "meta": v} for k, v in meta_by_id.items()])
            out["person_enrich"] = _enrich_profile_candidates(client, BASE_URL, items, limit=5)
            cand_info["raw_count"] = len(items)
            cand_info["merged_with_tags"] = len(meta_by_id)
            # where do pipeline tags live? sample first item's key paths
            if items:
                first = items[0]
                cand_info["sample_top_keys"] = list(first.keys()) if isinstance(first, dict) else type(first).__name__
                cand_info["sample_meta_keys"] = list(_g(first, "meta", default={}).keys()) if isinstance(_g(first, "meta"), dict) else None
                cand_info["sample_candidate_keys"] = list(_g(first, "meta", "candidate", default={}).keys()) if isinstance(_g(first, "meta", "candidate"), dict) else None
                cand_info["sample_candidateInfo_keys"] = list(_g(first, "meta", "candidateInfo", default={}).keys()) if isinstance(_g(first, "meta", "candidateInfo"), dict) else None
                cand_info["sample_has_profile"] = "profile" in first if isinstance(first, dict) else False
            # tag census + routing
            tag_census: Dict[str, int] = {}
            routed = {"engaged": 0, "pipeline": 0, "discounted_profile": 0, "discounted_table": 0, "excluded": 0}
            for it in items:
                tags = (_g(it, "meta", "candidate", "pipelineTags", default=None)
                        or _g(it, "meta", "candidateInfo", "pipelineTags", default=[]) or [])
                texts = [t.get("text", "") for t in tags if isinstance(t, dict) and t.get("text")]
                for t in texts:
                    tag_census[t] = tag_census.get(t, 0) + 1
                route = _candidate_route(it)
                if route is None:
                    routed["excluded"] += 1
                else:
                    stage, _, has_profile = route
                    if stage == Stage.ENGAGED: routed["engaged"] += 1
                    elif stage == Stage.PIPELINE: routed["pipeline"] += 1
                    elif stage == Stage.DISCOUNTED and has_profile: routed["discounted_profile"] += 1
                    elif stage == Stage.DISCOUNTED: routed["discounted_table"] += 1
            cand_info["pipeline_tags_seen"] = tag_census
            cand_info["routed_counts"] = routed
            # confirm the enriched blocks now populate (only the sampled profile
            # candidates carry currentStatus/confidential after enrichment)
            out["profile_field_probe"] = _profile_field_probe(items)
        else:
            cand_info["body_snippet"] = cr.text[:200]
        out["candidates"] = cand_info

    return out


def get_assignment_from_url(url: str) -> Assignment:
    """Full pipeline: URL -> Assignment. Uses mock data in demo mode."""
    if use_mock():
        return mock_assignment()
    assignment_id = extract_assignment_id(url)
    raw = EzekiaClient().fetch_raw(assignment_id)
    return map_assignment(raw)
