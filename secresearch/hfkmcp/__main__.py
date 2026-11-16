"""``python -m hfkmcp`` → 启动 MCP stdio server。"""

from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
