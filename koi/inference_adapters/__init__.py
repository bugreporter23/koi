"""Bundled inference adapters, loaded from adapters/NAME/relay.py beside koi.

Each adapter module runs as inference_adapters.NAME and shares common.py.
The set is fixed here; koi does not discover adapters at runtime.
"""

import importlib.util
from pathlib import Path
import sys

NAMES = ('codex', 'claude')
ROOT = Path(__file__).resolve().parents[2] / 'adapters'


def _load(name):
    spec = importlib.util.spec_from_file_location(__name__ + '.' + name, ROOT / name / 'relay.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


codex = _load('codex')
claude = _load('claude')
