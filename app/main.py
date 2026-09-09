"""
FastAPI backend for the Praeva template generator.

Flow:
  POST /api/generate  { "url": "<ezekia assignment url>" }
    -> fetches assignment (mock until API wired)
    -> fills the PPTX template
    -> returns the .pptx as a download

GET /api/preview?url=...   -> JSON summary of what will be generated
GET /                      -> the web UI
"""
from __future__ import annotations

import io
import os
import re
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()  # load EZEKIA_* from .env before ezekia.py reads them
except ImportError:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, ezekia, pptx_engine

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "praeva_search_update.pptx")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="Praeva Template Generator")


class GenerateRequest(BaseModel):
    url: str


class TokenRequest(BaseModel):
    token: str


def _safe_filename(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9 _-]", "", name).strip() or "Search Update"
    stamp = datetime.now().strftime("%Y-%m-%d")
    return f"Praeva Search Update - {base} - {stamp}.pptx"


@app.get("/api/health")
def health():
    return {"status": "ok", "mock_mode": ezekia.use_mock()}


# --- configuration (Ezekia API key) ------------------------------------- #
def _config_status():
    return {
        "token_configured": config.is_configured(),
        "token_hint": config.token_hint(),      # masked, e.g. "••••4Blk" — never the full token
        "token_source": config.token_source(),  # "config" | "env" | "none"
        "mock_mode": ezekia.use_mock(),
    }


@app.get("/api/config")
def get_config():
    """Current config status. Never returns the token itself."""
    return _config_status()


@app.post("/api/config/token")
def save_token(req: TokenRequest):
    try:
        config.set_ezekia_token(req.token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _config_status()


@app.delete("/api/config/token")
def delete_token():
    config.clear_ezekia_token()
    return _config_status()


@app.post("/api/config/test")
def test_token():
    """Verify the stored token authenticates against Ezekia."""
    return ezekia.test_token()


@app.get("/api/preview")
def preview(url: str = ""):
    """Return a summary of what would be generated (no file)."""
    try:
        a = ezekia.get_assignment_from_url(url)
    except ezekia.EzekiaError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "mock_mode": ezekia.use_mock(),
        "assignment": a.name,
        "title": a.title,
        "date": a.date,
        "counts": {
            "engaged_profiles": len(a.engaged()),
            "pipeline_table": len(a.pipeline()),
            "discounted_profiles": len(a.discounted_profiles()),
            "discounted_table": len(a.discounted_table()),
            "target": "manual",
        },
    }


@app.get("/api/diagnose")
def diagnose(url: str = ""):
    """Structural diagnostics for an empty/unexpected live result.
    Returns statuses, key names, counts and pipeline-tag texts — no personal data."""
    return ezekia.diagnose(url)


@app.post("/api/generate")
def generate(req: GenerateRequest):
    try:
        assignment = ezekia.get_assignment_from_url(req.url)
    except ezekia.EzekiaError as e:
        raise HTTPException(status_code=400, detail=str(e))

    fname = _safe_filename(assignment.name)
    out_path = os.path.join("/tmp", fname)
    try:
        pptx_engine.generate(assignment, TEMPLATE_PATH, out_path)
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"Failed to build deck: {e}")

    # Set Content-Disposition explicitly with a simple quoted filename so the
    # browser keeps the full "Praeva Search Update - <company> - <date>.pptx"
    # name. (Starlette's FileResponse switches to the filename*=utf-8'' form
    # when the name has spaces, which the frontend then can't parse.)
    from urllib.parse import quote
    headers = {
        "Content-Disposition": f'attachment; filename="{fname}"; '
                               f"filename*=UTF-8''{quote(fname)}"
    }
    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers=headers,
    )


# static frontend (mounted last so /api/* wins)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
