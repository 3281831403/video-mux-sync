"""把仓库根目录放进 sys.path，使 tests 可以直接 import mux。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
