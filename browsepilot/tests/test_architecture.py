"""Layer boundary guards: model <- agent <- app, single-direction imports."""

import ast
from pathlib import Path

import browsepilot  # noqa: F401  (fails until src/browsepilot exists)

PKG = Path(__file__).resolve().parent.parent / "src" / "browsepilot"

# 扫描覆盖面预期（覆盖度断言）：三条守卫都建立在"扫描结果"上 —— 目录被改名/误删时
# ``for path in []`` 会让守卫**真空通过**（"三条全绿"什么都没守卫）。所以下面把每个
# 扫描面真能扫到的模块数（model 5 / agent 9 / app.browser 6）与包内总数（32）钉死，
# 由 ``_layer_files()`` 在每次扫描时验证。模块增减时请同步更新这两行 —— 这是防真空
# 通过的必要代价：改包结构必须显式改这里，而不是静默少扫一个目录。
_EXPECTED_MODULES = {"model": 5, "agent": 9, "app/browser": 6}
_EXPECTED_PACKAGE_TOTAL = 32  # model 5 + agent 9 + app 17（含 app.browser 6 / app.services 5）+ 包根 1


def _imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


def _scan(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _layer_files(layer: str) -> list[Path]:
    """层内模块清单；**扫描结果不符合预期计数时直接失败**（而不是返回空列表）。"""
    files = _scan(PKG.joinpath(*layer.split("/")))
    expected = _EXPECTED_MODULES[layer]
    assert len(files) == expected, (
        f"{layer}/ 扫到 {len(files)} 个模块，期望 {expected}"
        " —— 目录改名/误删？守卫会真空通过"
    )
    total = len(_scan(PKG))
    assert total == _EXPECTED_PACKAGE_TOTAL, (
        f"包内模块总数 {total} != {_EXPECTED_PACKAGE_TOTAL}"
        " —— 包结构变了，请复核三条守卫的扫描面（见本文件顶部注释）"
    )
    return files


def test_model_layer_never_imports_upper_layers():
    for path in _layer_files("model"):
        for mod in _imported_modules(path):
            assert not mod.startswith("browsepilot.agent"), f"{path} imports {mod}"
            assert not mod.startswith("browsepilot.app"), f"{path} imports {mod}"


def test_agent_layer_never_imports_app_or_browser():
    """agent 层只经 ToolExecutor 协议与外部打交道：不 import app，
    **也不直接 import playwright**（浏览器驱动是 app.browser 的内部细节 ——
    docs/v2-p3-report.md 的冻结声明引用的就是这条守卫）。
    """
    for path in _layer_files("agent"):
        for mod in _imported_modules(path):
            assert not mod.startswith("browsepilot.app"), f"{path} imports {mod}"
            assert not mod.startswith("playwright"), f"{path} imports {mod}"


def test_app_browser_layer_is_self_contained():
    """app.browser 自包含（I4）：不 import ``browsepilot.agent``（agent 见过的只是 ToolExecutor
    协议），也不 import app 层其他模块（配置以**裸参数**传入：headless / max_pages / channel /
    mode / allowed_domains）。

    这条红线以前只是 ``app/browser/__init__.py`` 的 docstring 声称 —— 而且声称错了（说它依赖
    ``browsepilot.app.config``，其实一个 app 层模块都不依赖）；P3 final review I4 要求它真的有
    测试，而不只是文档承诺。
    """
    for path in _layer_files("app/browser"):
        for mod in _imported_modules(path):
            assert not mod.startswith("browsepilot.agent"), f"{path} imports {mod}"
            assert (
                not mod.startswith("browsepilot.app") or mod.startswith("browsepilot.app.browser")
            ), f"{path} imports {mod}"

