"""Test fixtures for the camera driver.

We insert the driver source root on ``sys.path`` so ``import main`` works
without packaging the driver as an installable module (it's a standalone
script, not a package).
"""

from __future__ import annotations

import sys
from pathlib import Path

_DRIVER_ROOT = Path(__file__).resolve().parent.parent
if str(_DRIVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DRIVER_ROOT))
