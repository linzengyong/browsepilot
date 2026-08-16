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
    for path in _layer_files("agent"):
        for mod in _imported_modules(path):
            assert not mod.startswith("browsepilot.app"), f"{path} imports {mod}"
            assert not mod.startswith("browser_mcp"), f"{path} imports {mod}"


def test_model_layer_never_imports_browser():
    for path in _layer_files("model"):
        for mod in _imported_modules(path):
            assert not mod.startswith("browser_mcp"), f"{path} imports {mod}"
