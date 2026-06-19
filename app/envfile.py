"""Minimal .env loader (no third-party dependency).

Loads ``KEY=VALUE`` lines from a ``.env`` file into ``os.environ`` so secrets
like ``ANTHROPIC_API_KEY`` live in a gitignored file instead of being typed on
the command line (or pasted anywhere they'd be logged). It **never overrides** a
variable already set in the real environment, and is a quiet no-op when the file
is absent — safe to call unconditionally at startup.
"""
import os
from typing import Dict


def load_dotenv(path: str = ".env") -> Dict[str, str]:
    """Load KEY=VALUE pairs from ``path`` into os.environ (without overriding).

    Returns the dict of keys actually applied. Lines that are blank, comments
    (``#``), or lack ``=`` are ignored; surrounding quotes on the value are
    stripped.
    """
    if not os.path.exists(path):
        return {}
    applied: Dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
                applied[key] = val
    return applied
