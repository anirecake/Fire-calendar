"""One place to read settings: the process environment first (Vercel), then .env / .env.local (local dev)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    for fname in (".env", ".env.local"):
        f = ROOT / fname
        if not v and f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(f"{name}="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
    return v
