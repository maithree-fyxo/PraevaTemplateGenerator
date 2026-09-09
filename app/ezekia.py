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
     -> each person embeds profile.* AND meta.candidateInfo.pipelineTags
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

# Fields to embed on the candidates call (one round-trip for the whole deck).
CANDIDATE_INCLUDES = [
    "profile.positions",
    "profile.education",
    "profile.confidential",
    "profile.currentStatus",
    "profile.aspirations",
]

# How each Ezekia pipeline tag (lower-cased) routes into the report.
# Value = (Stage, has_profile):
#   has_profile=True  -> full profile format (2 per page)
#   has_profile=False -> list-table format
# EDIT here if Praeva renames its pipeline stages.
#
# Praeva's mapping:
#   Phone Interview / Praeva Interview -> Engaged        -> full profile
#   In discussion                      -> Pipeline       -> table
#   Not Interested                     -> Discounted     -> table
#   Praeva discounted                  -> Discounted     -> full profile
#   (Target section is filled manually -> placeholders, no tag routes here)
TAG_ROUTING: Dict[str, tuple[Stage, bool]] = {
    "phone interview": (Stage.ENGAGED, True),
    "praeva interview": (Stage.ENGAGED, True),
    "in discussion": (Stage.PIPELINE, False),
    "not interested": (Stage.DISCOUNTED, False),
    "praeva discounted": (Stage.DISCOUNTED, True),
}
# Candidates whose pipeline tags match none of the above are left OUT of the
# report (e.g. brand-new/unclassified candidates, or Target-section people who
# are added manually).

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
        params = [("fieldsWithCandidate[]", f) for f in CANDIDATE_INCLUDES]
        params.append(("count", "500"))
        with httpx.Client(timeout=self.timeout, headers=self._headers()) as client:
            project = self._get(client, f"/v4/projects/{assignment_id}").get("data", {})
            candidates = self._get(
                client, f"/v4/projects/{assignment_id}/candidates", params=params
            ).get("data", [])
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
    for path in (("company",), ("company", "name"),
                 ("relationships", "company", "data", "name"),
                 ("relationships", "companies", "data", 0, "name"),
                 ("meta", "company"), ("name",), ("label",), ("projectId",), ("title",)):
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
    """Return (stage, status_label, has_profile) from pipeline tags, or None if
    the candidate matches no routed tag (and so is excluded from the report)."""
    tags = _g(person, "meta", "candidateInfo", "pipelineTags", default=[]) or []
    texts = [t.get("text", "") for t in tags if isinstance(t, dict) and t.get("text")]
    for txt in texts:
        route = TAG_ROUTING.get(txt.strip().lower())
        if route is not None:
            stage, has_profile = route
            return stage, txt.strip(), has_profile
    return None


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
        or f"{person.get('firstName','')} {person.get('lastName','')}".strip(),
        stage=stage,
        role=current.get("title", ""),
        company=_position_company(current),
        status=status_label,
        has_profile=has_profile,
        salary=_salary(profile),
        location=_location(profile),
        availability=_availability(profile),
        education=_education(profile.get("education", [])),
        career=_career(positions),
    )
    cand.__dict__["_rank"] = _g(person, "meta", "candidateInfo", "rank", default=1_000_000)
    return cand


def _position_company(pos: Dict[str, Any]) -> str:
    comp = pos.get("company")
    if isinstance(comp, dict):
        return comp.get("name", "")
    if isinstance(comp, str):
        return comp
    return ""


def _career(positions: List[Dict[str, Any]]) -> List[CareerEntry]:
    out = []
    for p in positions:
        start = _year(p.get("startDate"))
        end = _year(p.get("endDate"))
        if not end and (p.get("tense") or p.get("primary")):
            end = "present"
        dates = f"{start} - {end}".strip(" -") if (start or end) else ""
        out.append(CareerEntry(
            company=_position_company(p),
            role=p.get("title", ""),
            dates=dates,
        ))
    return out


def _education(edu: List[Dict[str, Any]]) -> str:
    lines = []
    for e in edu:
        school = e.get("school", "")
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


def _location(profile: Dict[str, Any]) -> str:
    loc = _g(profile, "currentStatus", "location", "name")
    if loc:
        return loc
    asp_locs = _g(profile, "aspirations", "locations", default=[]) or []
    if asp_locs and isinstance(asp_locs[0], dict):
        return asp_locs[0].get("name", "")
    return ""


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
    params = [("fieldsWithCandidate[]", f) for f in CANDIDATE_INCLUDES] + [("count", "500")]

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

        # --- candidates ---
        cr = client.get(f"{BASE_URL}/v4/projects/{assignment_id}/candidates", params=params)
        cand_info: Dict[str, Any] = {"http_status": cr.status_code, "params_sent": [p[0]+"="+p[1] for p in params]}
        if cr.status_code == 200:
            body = cr.json()
            cand_info["envelope_keys"] = list(body.keys()) if isinstance(body, dict) else type(body).__name__
            data = body.get("data", body) if isinstance(body, dict) else body
            items = data if isinstance(data, list) else []
            cand_info["raw_count"] = len(items)
            # where do pipeline tags live? sample first item's key paths
            if items:
                first = items[0]
                cand_info["sample_top_keys"] = list(first.keys()) if isinstance(first, dict) else type(first).__name__
                cand_info["sample_meta_keys"] = list(_g(first, "meta", default={}).keys()) if isinstance(_g(first, "meta"), dict) else None
                cand_info["sample_candidateInfo_keys"] = list(_g(first, "meta", "candidateInfo", default={}).keys()) if isinstance(_g(first, "meta", "candidateInfo"), dict) else None
                cand_info["sample_has_profile"] = "profile" in first if isinstance(first, dict) else False
            # tag census + routing
            tag_census: Dict[str, int] = {}
            routed = {"engaged": 0, "pipeline": 0, "discounted_profile": 0, "discounted_table": 0, "excluded": 0}
            for it in items:
                tags = _g(it, "meta", "candidateInfo", "pipelineTags", default=[]) or []
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
        else:
            cand_info["body_snippet"] = cr.text[:200]
        out["candidates"] = cand_info

        # --- probe: request extra candidate-include fields, one at a time,
        #     and report where a tag/status/pipeline-like key then appears ---
        probe: Dict[str, Any] = {}
        for guess in _TAG_FIELD_GUESSES:
            gp = [("fieldsWithCandidate[]", guess), ("count", "3")]
            try:
                gr = client.get(f"{BASE_URL}/v4/projects/{assignment_id}/candidates", params=gp)
            except Exception as e:
                probe[guess] = {"error": str(e)[:80]}
                continue
            entry: Dict[str, Any] = {"status": gr.status_code}
            if gr.status_code == 200:
                items = (gr.json() or {}).get("data", [])
                if items:
                    paths = _key_paths(items[0])
                    entry["new_top_keys"] = [p for p in paths if "." not in p and "[]" not in p]
                    entry["tag_like_paths"] = [
                        p for p in paths
                        if any(w in p.lower() for w in ("pipelinetag", "candidateinfo", "status", "tag"))
                    ][:15]
                else:
                    entry["items"] = 0
            probe[guess] = entry
        out["tag_field_probe"] = probe

    return out


def get_assignment_from_url(url: str) -> Assignment:
    """Full pipeline: URL -> Assignment. Uses mock data in demo mode."""
    if use_mock():
        return mock_assignment()
    assignment_id = extract_assignment_id(url)
    raw = EzekiaClient().fetch_raw(assignment_id)
    return map_assignment(raw)
