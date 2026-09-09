# Praeva Search Update Generator

A web app that takes an **Ezekia assignment URL**, fetches the project and its
candidates, and auto-fills the Praeva "Search Update" PowerPoint template.

> **Status: working prototype, live Ezekia API wired.** It runs end-to-end in
> **demo mode** (bundled sample data) out of the box, and against the **live
> Ezekia API** once a token with access to the assignment is provided. The
> integration is built against Ezekia's OpenAPI spec (`https://ezekia.com/api`).

## What it fills (candidate-driven slides only)

| Slide | Filled |
|---|---|
| Cover | Project name, "Prepared for" contacts, date, title |
| Engaged candidate profiles | Name, salary, location, availability, education, full career-history table — **2 per slide, cloned as needed** |
| Select pipeline candidates | Name / Role / Company / Status table (variable rows) |
| Select target candidates | Same table, variable rows |
| Discounted candidate profiles | Same profile layout, cloned as needed |
| Select discounted candidates | Name / Role / Company / Status table |

**Left untouched for manual editing** (per scope): the executive-summary
dashboard (charts, metrics, timeline, market feedback), the target-landscape
logo slide, the section dividers and the back cover. Formatting, fonts, colours
and branding of the original template are preserved exactly — the engine clones
the styled template shapes and only swaps text.

## Quick start

```bash
pip install -r requirements.txt
python run.py
# open http://127.0.0.1:8000
```

Paste any URL and click **Preview** then **Generate PowerPoint**. In demo mode
it always returns the sample "Campfire" assignment so you can see the output.

## Connecting Ezekia

Everything Ezekia-specific lives in **`app/ezekia.py`**, already built against
the live API:

* **Base URL** `https://ezekia.com/api`, **Bearer token** auth.
* Assignments are "projects": `GET /v4/projects/{id}`.
* Candidates (one call, profile embedded):
  `GET /v4/projects/{id}/candidates?fieldsWithCandidate[]=profile.positions&…`
  — embeds positions (career), education, confidential (salary/notice),
  currentStatus (location), aspirations, and `meta.candidateInfo.pipelineTags`.
* "Prepared for" contacts: `GET /v4/projects/{id}/contacts`.

To go live, add your API key in the app's **Settings** (recommended), or via
`.env`:

* **In-app (recommended):** open **Settings** (top-right), paste the key, Save.
  The key is stored server-side in `.secrets.json` (owner-only `0600`
  permissions, gitignored) and is **write-only from the UI** — no endpoint ever
  returns it, so it can't be read or copied back out of the app; only a masked
  hint (last 4 chars) is shown. Use **Test connection** to verify it, **Clear
  key** to remove it. Saving a key automatically switches the app out of demo
  mode. An app-saved key takes precedence over the `.env` value.
* **.env:** copy `.env.example` to `.env`, set `EZEKIA_TOKEN`, `EZEKIA_USE_MOCK=0`.

Then paste an assignment URL (e.g. `https://ezekia.com/#/assignments/907468/info`)
and generate.

Two things to confirm on the **first real run** (both isolated and easy to
adjust; see below):

**A. Token access.** The token must belong to an Ezekia account with access to
the assignment. A `403 "This action is unauthorized"` means the token's user
can't see that project — regenerate the token from an account that owns/consults
on it, or have it shared. (During development, the supplied token returned 403
for assignment 907468 and an empty `/v4/projects` list, i.e. no visible
projects.)

**B. Stage taxonomy.** Which section a candidate lands in and in which format is
driven by their Ezekia **pipeline tags** (`meta.candidateInfo.pipelineTags[].text`),
configured in **`TAG_ROUTING`** in `app/ezekia.py`. Praeva's mapping:

| Pipeline tag | Section | Format |
|---|---|---|
| `Phone Interview`, `Praeva Interview` | Engaged candidates | Full profile (2 per page) |
| `In discussion` | Select pipeline candidates | Table |
| `Not Interested` | Select discounted candidates | Table |
| `Praeva discounted` | Select discounted candidates | Full profile |

The **Select target candidates** section is completed manually — no tag routes
there; the generator leaves blank placeholder rows. Candidates whose tags match
none of the above are left out of the report. Edit `TAG_ROUTING` if tag names
change.

**C. Client name field.** The cover's client company is read via `_client_name()`
with several fallbacks; confirm it picks the right field for your projects.

The rest of the app (template engine, API, UI) never needs to know about
Ezekia's schema.

## Architecture

```
app/
  models.py       Internal data model (Assignment, Candidate, CareerEntry) — stable
  ezekia.py       ONLY Ezekia-aware module: URL parse → fetch → map to models
  mock_data.py    Sample assignment mirroring the template (demo mode)
  pptx_engine.py  Fills/clones the template via python-pptx (no Ezekia knowledge)
  main.py         FastAPI: /api/preview, /api/generate, serves the UI
static/index.html Web UI (paste URL → preview → download)
templates/        The Praeva Search Update .pptx template
```

## How the engine stays faithful to the template

* **Position-based field mapping.** The template names its placeholders
  inconsistently, so fields are located by geometry (two-up columns; fixed
  vertical positions for name / location / salary / availability / education).
* **Clone, don't rebuild.** Extra profile slides and table rows are deep-copies
  of the styled originals, so all formatting is inherited; only run text is
  replaced.
* **Empty slots masked.** A single-candidate profile slide hides the unused half
  (its labels come from the slide layout) with a background-coloured cover.

## Notes / next steps

* Education alignment uses the template's tab stops; if a candidate's education
  string is long it may wrap — easy to refine once real data shape is known.
* If a search has many engaged candidates the deck grows by one slide per two
  profiles, as intended.
* Not yet wired: the executive-summary metrics (out of scope for this build —
  those slides are passed through untouched).
```
