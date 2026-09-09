"""
Server-side configuration store for secrets (the Ezekia API token).

Design goals
------------
* The token is entered once through the app and stored **on the server**, in a
  file with owner-only permissions (0600), never in the browser.
* The token is **write-only from the app's perspective**: no endpoint ever
  returns the stored value, so it cannot be read or copied back out of the UI.
  Only a masked hint (last 4 characters) is exposed, for the user to confirm
  which key is set.
* Precedence: a token saved through the app wins over the EZEKIA_TOKEN env var.

The file lives at <project root>/.secrets.json and is gitignored.
"""
from __future__ import annotations

import json
import os
import stat
from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_PATH = os.path.join(BASE_DIR, ".secrets.json")


def _load() -> dict:
    try:
        with open(SECRETS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    # write with owner-only permissions
    fd = os.open(SECRETS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
    finally:
        try:
            os.chmod(SECRETS_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass


# --- Ezekia token -------------------------------------------------------- #
def get_ezekia_token() -> str:
    """The active token: app-configured first, then EZEKIA_TOKEN env var."""
    return _load().get("ezekia_token") or os.getenv("EZEKIA_TOKEN", "") or ""


def set_ezekia_token(token: str) -> None:
    token = (token or "").strip()
    if not token:
        raise ValueError("Token is empty.")
    data = _load()
    data["ezekia_token"] = token
    _save(data)


def clear_ezekia_token() -> None:
    data = _load()
    data.pop("ezekia_token", None)
    _save(data)


def is_configured() -> bool:
    return bool(get_ezekia_token())


def token_source() -> str:
    if _load().get("ezekia_token"):
        return "config"
    if os.getenv("EZEKIA_TOKEN"):
        return "env"
    return "none"


def token_hint() -> Optional[str]:
    """A non-sensitive masked hint (last 4 chars). Never the full token."""
    tok = get_ezekia_token()
    if not tok:
        return None
    tail = tok[-4:] if len(tok) >= 4 else tok
    return f"••••{tail}"


def force_mock() -> bool:
    return os.getenv("EZEKIA_USE_MOCK", "").lower() in ("1", "true", "yes")


def use_mock() -> bool:
    """Demo mode when explicitly forced, or when no token is available."""
    return force_mock() or not is_configured()
