"""Settings for the app version (no command line): where the data folder is, and the Claude API key.

The data folder is remembered in a small JSON file in the user's Library. The API key lives in the macOS
Keychain (via `keyring`), never in a file. A key in the environment (`op run --env-file=.env -- ...`)
still wins, so the developer workflow is unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from rc import workspace as wsp

APP_NAME = "Reading Challenge"
KEYCHAIN_SERVICE = "Reading Challenge (Claude API key)"
KEYCHAIN_ACCOUNT = "anthropic"


def settings_path() -> Path:
    base = os.environ.get("READING_CHALLENGE_SETTINGS")
    if base:
        return Path(base)
    return Path.home() / "Library" / "Application Support" / APP_NAME / "settings.json"


def load() -> dict:
    p = settings_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(values: dict) -> None:
    current = load()
    current.update(values)
    wsp.write_atomic(settings_path(), json.dumps(current, indent=2))


def data_folder() -> Path | None:
    folder = os.environ.get("READING_CHALLENGE_DATA") or load().get("data_folder")
    return Path(folder).expanduser() if folder else None


def who(override: str = "") -> str:
    """The name recorded on every change (asked once in Settings; --who or READING_CHALLENGE_WHO override)."""
    return (override or os.environ.get("READING_CHALLENGE_WHO") or load().get("who") or "").strip()


# ---------------------------------------------------------------------------- API key
def _keyring():
    import keyring
    return keyring


def api_key_source() -> str:
    """'environment', 'keychain' or '' (none)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "environment"
    try:
        return "keychain" if _keyring().get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT) else ""
    except Exception:          # no keychain available (tests, other platforms)
        return ""


def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        return _keyring().get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT) or ""
    except Exception:
        return ""


def set_api_key(key: str) -> None:
    key = key.strip()
    if not key.startswith("sk-ant-") or len(key) < 30 or any(c.isspace() for c in key):
        raise ValueError("that does not look like a Claude API key (they start with sk-ant-)")
    _keyring().set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, key)


def forget_api_key() -> None:
    try:
        _keyring().delete_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    except Exception:
        pass
