"""向后兼容入口。

旧 workflow（以及本地既有习惯）调用的是 `python glados_checkin.py`，
实现已迁移到 checkin.py（Devilstore 底座）。这里只做转发，避免入口分裂。
"""

import sys

from checkin import main

if __name__ == "__main__":
    sys.exit(main())