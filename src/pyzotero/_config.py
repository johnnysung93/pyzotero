"""Configuration loading for Pyzotero command-line helpers."""

from __future__ import annotations

import os
import shlex
from pathlib import Path


DEFAULT_ENV_PATH = Path("~/.config/pyzotero/.env").expanduser()


def load_env(path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Load simple KEY=VALUE pairs from a dotenv file into ``os.environ``.

    Existing environment variables take precedence, which lets container
    runtimes override values without rewriting the mounted config file.
    """
    env_path = Path(path).expanduser() if path else DEFAULT_ENV_PATH
    values: dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
        except ValueError:
            parsed = [value.strip("\"'")]
        clean_value = parsed[0] if parsed else ""
        values[key] = clean_value
        os.environ.setdefault(key, clean_value)
    return values


def env_bool(name: str, default: bool = False) -> bool:
    """Return an environment variable parsed as a boolean."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def getenv_any(*names: str, default: str = "") -> str:
    """Return the first non-empty environment value from a list of aliases."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def update_env_file(
    values: dict[str, str],
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Create or update a dotenv file while preserving unrelated lines."""
    env_path = Path(path).expanduser() if path else DEFAULT_ENV_PATH
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    pending = dict(values)
    updated: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            updated.append(line)
            continue
        key, _ = line.split("=", 1)
        clean_key = key.strip()
        if clean_key in pending:
            updated.append(f"{clean_key}={quote_env_value(pending.pop(clean_key))}")
        else:
            updated.append(line)

    if pending and updated and updated[-1].strip():
        updated.append("")
    for key, value in pending.items():
        updated.append(f"{key}={quote_env_value(value)}")

    env_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    return env_path


def quote_env_value(value: str) -> str:
    """Quote a dotenv value if needed."""
    if value == "":
        return '""'
    if any(ch.isspace() or ch in "#'\"\\$`" for ch in value):
        return shlex.quote(value)
    return value
