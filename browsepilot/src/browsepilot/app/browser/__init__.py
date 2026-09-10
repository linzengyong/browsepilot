"""Tier3 浏览器层（app 层）：Page 池 + 无障碍树快照 refs。

层次红线（I4）：本包**自包含** —— 只依赖标准库、第三方库（playwright）与本包内部模块；
不 import ``browsepilot.agent.*``，也不 import app 层其他模块（配置以裸参数传入：
headless / max_pages / channel / policy mode / allowed_domains），由
``tests/test_architecture.py::test_app_browser_layer_is_self_contained`` 守卫。
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
