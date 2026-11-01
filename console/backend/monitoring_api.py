# -*- coding: utf-8 -*-
"""Thin relocating shim — implementation lives in ``kernel/backend/monitoring_api.py``."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_IMPL = Path(__file__).resolve().parents[2] / "kernel" / "backend" / "monitoring_api.py"
_spec = importlib.util.spec_from_file_location(__name__, _IMPL)
if _spec is None or _spec.loader is None:
    raise ImportError("kernel backend missing: %s" % (_IMPL,))
_mod = importlib.util.module_from_spec(_spec)
sys.modules[__name__] = _mod
_spec.loader.exec_module(_mod)
