#!/usr/bin/env bash
# BrowsePilot 评测回归门禁（mock 模式）—— 本地一键复现 CI 的全部检查。
#
# 用法（脚本自己定位项目目录，在任意目录调用都可以）：
#   bash browsepilot/eval/gate.sh     # 仓库根
#   bash eval/gate.sh                 # browsepilot/ 目录内
#
# 依次执行（任一步失败即停 + 非零退出）：
#   1. uv run pytest tests/ -q                # 全量单测
#   2. uv run ruff check src/ eval/ tests/    # lint
#   3. uv run mypy src/browsepilot/           # 类型检查
#   4. uv run python -m eval.run_eval --mock  # 评测回归（FakeProvider/FakeExecutor）
#
# 门禁只跑 mock（P4-D8）：无网络、无 key、无浏览器 —— 与 CI 同口径。
# 真实模式（需 BP_MODEL_API_KEY + 浏览器）是手动/验收运行，不进 CI，例如：
#   BP_DEFAULT_MODEL=deepseek/deepseek-chat BP_MODEL_API_KEY=... \
#     uv run python -m eval.run_eval --tag acceptance --repeat 3 --md data/eval-reports/
# 详见 docs/v2-eval.md §6「运行指南」（门禁 / CI / 真实模式前置）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Windows 控制台默认代码页会把评测输出里的中文变成乱码（不影响退出码，但日志没法看）。
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

if ! command -v uv >/dev/null 2>&1; then
    echo "[gate] FATAL: uv 不在 PATH 上（安装：https://docs.astral.sh/uv/）" >&2
    exit 127
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(not a git repo)')"
HEAD_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo '-')"
echo "[gate] browsepilot eval gate (mock mode)"
echo "[gate] env: cwd=$PROJECT_DIR branch=$BRANCH head=$HEAD_SHA time=$(date '+%Y-%m-%d %H:%M:%S')"
echo "[gate] toolchain: $(uv --version 2>&1)"

# 任一步失败 → 打出标签 + 非零退出；成功路径什么都不额外打。
run_step() {
    local label="$1"
    shift
    echo ""
    echo "[gate] >>> $label"
    if ! "$@"; then
        echo "[gate] FAILED: $label（见上方输出）" >&2
        exit 1
    fi
}

run_step "pytest tests/ -q" uv run pytest tests/ -q
run_step "ruff check src/ eval/ tests/" uv run ruff check src/ eval/ tests/
run_step "mypy src/browsepilot/" uv run mypy src/browsepilot/
run_step "eval run_eval --mock" uv run python -m eval.run_eval --mock

echo ""
echo "[gate] ALL PASS"
