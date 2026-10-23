"""Make source-checkout imports available before dashboard bootstrap imports."""

import sys
from pathlib import Path

REPO_ROOT = None
for parent in Path(__file__).resolve().parents:
    if (parent / "main.py").is_file() and (parent / "src").is_dir():
        REPO_ROOT = parent
        if str(parent) not in sys.path:
            sys.path.insert(0, str(parent))
        break
