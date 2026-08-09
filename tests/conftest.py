"""Shared pytest setup for blind acceptance tests (anti-test-hacking / blind-TDV).

These tests were authored from ONLY
docs/specs/custom-scripts-contracts.md (the contract) — no implementation
code, design notes, or other agent's reasoning was consulted.

This file exists solely to guarantee the repo root is importable so that
`scripts.<module>` (or a bare top-level `<module>`, see the `_import` helper
duplicated in each test file) can be resolved regardless of exactly where
pytest's rootdir insertion lands.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
