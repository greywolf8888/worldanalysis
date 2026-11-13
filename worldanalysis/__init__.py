"""Worldanalysis: incremental local evidence and independent analysis.

Project-local dependencies never reuse subscription credentials or modify the host.
"""
from pathlib import Path
import sys

_LOCAL_DEPS = Path(__file__).resolve().parent.parent / '.runtime' / 'site-packages'
if _LOCAL_DEPS.is_dir() and str(_LOCAL_DEPS) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DEPS))

__version__ = '0.1.0'
