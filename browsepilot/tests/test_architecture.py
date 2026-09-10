"""Layer boundary guards: model <- agent <- app, single-direction imports."""

import ast
from pathlib import Path

import browsepilot  # noqa: F401  (fails until src/browsepilot exists)

PKG = Path(__file__).resolve().parent.parent / "src" / "browsepilot"


def _imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


def _layer_files(layer: str):
    return sorted((PKG / layer).rglob("*.py"))


def test_model_layer_never_imports_upper_layers():
    for path in _layer_files("model"):
        for mod in _imported_modules(path):
            assert not mod.startswith("browsepilot.agent"), f"{path} imports {mod}"
            assert not mod.startswith("browsepilot.app"), f"{path} imports {mod}"


def test_agent_layer_never_imports_app_or_browser():
    """agent 层只经 ToolExecutor 协议与外部打交道：不 import app/browser_mcp，
    **也不直接 import playwright**（浏览器驱动是 app.browser 的内部细节 ——
    docs/v2-p3-report.md 的冻结声明引用的就是这条守卫）。
    """
    for path in _layer_files("agent"):
        for mod in _imported_modules(path):
            assert not mod.startswith("browsepilot.app"), f"{path} imports {mod}"
            assert not mod.startswith("browser_mcp"), f"{path} imports {mod}"
            assert not mod.startswith("playwright"), f"{path} imports {mod}"


def test_app_browser_layer_is_self_contained():
    """app.browser 自包含（I4）：不 import ``browsepilot.agent``（agent 见过的只是 ToolExecutor
    协议），也不 import app 层其他模块（配置以**裸参数**传入：headless / max_pages / channel /
    mode / allowed_domains）。

    这条红线以前只是 ``app/browser/__init__.py`` 的 docstring 声称 —— 而且声称错了（说它依赖
    ``browsepilot.app.config``，其实一个 app 层模块都不依赖）；P3 final review I4 要求它真的有
    测试，而不只是文档承诺。
    """
    for path in sorted((PKG / "app" / "browser").rglob("*.py")):
        for mod in _imported_modules(path):
            assert not mod.startswith("browsepilot.agent"), f"{path} imports {mod}"
            assert (
                not mod.startswith("browsepilot.app") or mod.startswith("browsepilot.app.browser")
            ), f"{path} imports {mod}"


def test_model_layer_never_imports_browser():
    for path in _layer_files("model"):
        for mod in _imported_modules(path):
            assert not mod.startswith("browser_mcp"), f"{path} imports {mod}"
