#!/usr/bin/env python3
"""CLI entrypoint for the independent M3 external-matrix validator."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.validation.validate_m3_external_matrix import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
