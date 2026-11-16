"""让 ``python -m hfk`` 等价于 ``python -m hfk.cli``。"""

from __future__ import annotations

import sys

from hfk.cli import main

if __name__ == "__main__":
    sys.exit(main())
