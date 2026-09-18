"""Import first in every test module: puts scripts/ on sys.path so `import
rewards`, `import wizard`, `import profile_paths` and `from backends import
...` resolve the same way they do when rewards.py is run directly, regardless
of the working directory the test runner was invoked from.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
