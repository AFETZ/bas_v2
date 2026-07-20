#!/usr/bin/env python3
"""CLI entrypoint for the independent M4 capacity validator."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.validation.validate_m4_capacity import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
