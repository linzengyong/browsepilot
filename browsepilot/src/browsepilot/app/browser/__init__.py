"""Tier3 浏览器层（app 层）：Page 池 + 无障碍树快照 refs。

层次红线：本包只依赖 ``browsepilot.app.config`` 与第三方库（playwright），
不 import ``browsepilot.agent.*``（由 ``tests/test_architecture.py`` 守卫）。
"""

from browsepilot.app.browser.browser_pool import BrowserPool, page_snapshot_refs
from browsepilot.app.browser.snapshot import PageSnapshot, parse_snapshot_refs, snapshot_to_refs

__all__ = [
    "BrowserPool",
    "PageSnapshot",
    "page_snapshot_refs",
    "parse_snapshot_refs",
    "snapshot_to_refs",
]
