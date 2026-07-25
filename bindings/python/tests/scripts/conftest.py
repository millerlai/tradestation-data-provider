from __future__ import annotations

import sys
from pathlib import Path

# tests/scripts/conftest.py → parents[2] is the Python binding root.
_BINDING_ROOT = Path(__file__).resolve().parents[2]

SCRIPTS_DIR = _BINDING_ROOT / "scripts"

# contract/tools/ sits outside this binding on purpose — it must not depend on
# any binding to be a neutral fixture recorder. This is the only test runner in
# the repo, so the reference binding hosts its smoke test.
CONTRACT_TOOLS_DIR = _BINDING_ROOT.parents[1] / "contract" / "tools"

for _d in (SCRIPTS_DIR, CONTRACT_TOOLS_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
