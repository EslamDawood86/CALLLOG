"""Imported first by run.py and manage.py: lets the app use the 'libs' folder next to it.

The first start installs Flask etc. into 'libs' inside the shared folder, so every PC that runs
the app from the share can use them without installing anything itself (it only needs Python).
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIBS = HERE / "libs"

if LIBS.is_dir() and str(LIBS) not in sys.path:
    sys.path.insert(0, str(LIBS))
