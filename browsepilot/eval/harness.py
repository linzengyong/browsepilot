"""Real-mode eval harness — 组合根 + 池生命周期 + 用量/耗时采集 + 多轮接线（P4-T3）。

评测层的"真的把任务跑起来"这一半：怎么装配真实依赖、怎么让每个场景干净地开始、怎么把
评测需要的**数字**（用量/耗时/工具名）从运行里取出来。场景集/断言在 :mod:`eval.scenarios`
与 :mod:`eval.checks`，选择与打印在 :mod:`eval.run_eval`（本模块被它调用）。

四件事：

**1. 组合根（``build_real_deps``）**：Settings → 模型 + Tier1/2 服务 + BrowserPool +
``AppToolExecutor``（池与导航策略都从**同一个** Settings 显式构造，I3）。为什么必须在评测侧
装配：``AppToolExecutor`` 拿不到池时 6 个 Tier3 工具统一返回 ``browser_unavailable`` ——
tier3 场景会**全部**失败，而那是接线问题、不是任务失败（P3 final review 给 P4 的关键输入）。
导入刻意留在函数里：``--mock``（CI）不该拉起 litellm / playwright / ddgs 的依赖链。

**2. 池生命周期（``pool_lifecycle``）**：池的生命周期跨场景（start 一次 → 全部场景 → close），
**页面**的生命周期逐场景 —— 后者由 ``run_eval._run_real`` 的 ``teardown=executor.aclose`` 接线
（每个**跑过**的场景之后把当前页还给池；见 :mod:`eval.run_eval` 的 C2③ 说明）。
池起不来（本机没装 chromium rev / channel 写错）时打 WARNING 并把 ``attach_browser(None)``
传给 executor：宁可让 tier3 的失败在报告里**可见地**归因到环境，也不要静默产出误导性的成功率。

**3. 用量/耗时采集（P4-D3 的数据面，来源已核实）**：v2 的 ``AgentState`` 没有 token 字段
（``ChatResult.usage`` 在 answer 节点被丢弃），所以 harness 用装饰器
:class:`UsageTrackingProvider` 从**模型调用的返回值**里取用量、用 ``time.perf_counter()``
量整场景墙钟，并把结果注入 outcome：

===================  ============================================================
注入键                来源 / 语义
===================  ============================================================
``token_usage``      ``ChatResult.usage``（``UsageInfo``）逐次累加：``total_tokens``；
                     ``input_tokens`` / ``output_tokens`` / ``cost_usd`` 仅当 provider
                     真的报了才出现（未知不是 0）。自描述（I2）：``usage_source`` =
                     ``"chat_only"``（这组数字只覆盖 chat 调用）、``chat_calls`` /
                     ``model_calls``（后者含 structured/stream，是 token 数的**诚实分母**）。
                     **拿不到用量时字段缺席**（fail-loud）→ ``tokens_below`` FAIL。
``duration_ms``      整场景（含全部轮次）的墙钟毫秒。
``tool_calls``       ``call()`` 的**全部**工具名（含失败调用）；多轮场景 = 各轮累加。
===================  ============================================================

用量为什么只有 ``chat``：``structured`` 返回的是 schema 实例（litellm 的 usage 在重试循环里
被丢掉）、``stream`` 按 port 只 yield 字符串 —— 两个口都没有 usage 面（P4-T1 探测结论）。
真实评测的模型调用以 chat/structured 为主，可得的就是 chat 那些；其余不填数（见
:meth:`UsageAccumulator.snapshot`）。但装饰器看得到**全部**三个口，所以快照把
``chat_calls``（有数字的）与 ``model_calls``（实际发生的调用数）分开报 —— token/成本是
**chat-only 的下界**，必须自描述（P4-T3 审查 I2），否则 T4/T8 发布的数字会被系统性低估
而读者无从察觉。

**4. 多轮接线（``follow_up``，I4）**：``Scenario.follow_up`` 是第 2..n 轮的 prompt。
逐轮跑 ``run_task``，第 n 轮带上前几轮的 history —— 格式与 app 完全一致
（``Q: <task> / A: <answer>`` 摘要、压单行、截断 200 字符、只留最近 5 条，
见 :func:`history_entries` 与 ``app.main._build_history_entries``）；
**checks 评估最后一轮的 outcome**（场景 YAML 里注明），因为多轮的"结论"在最后一句回答上。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from browsepilot.agent.api import run_task
from browsepilot.agent.tools import ToolExecutor, ToolResult, ToolSpec
from browsepilot.agent.tracer import Tracer
from browsepilot.model.messages import ChatResult, Message, UsageInfo
from browsepilot.model.provider import ModelProvider
from eval.scenarios import Scenario

if TYPE_CHECKING:
    # 只在类型检查时导入：``--mock``（CI）不该拉起 playwright / litellm / pydantic-settings 的依赖链
    from browsepilot.app.browser.browser_pool import BrowserPool
    from browsepilot.app.config import Settings
    from browsepilot.app.executor import AppToolExecutor

HISTORY_MAX_ENTRIES = 5
"""history 最多注入几条（与 ``app.main._HISTORY_MAX_ENTRIES`` / graph 的 ``[-5:]`` 一致）。"""

HISTORY_MAX_ENTRY_CHARS = 200
"""单条 history 摘要的字符上限（与 ``app.main._HISTORY_MAX_ENTRY_CHARS`` 一致）。"""


# ── 观测面：工具名 / 用量 ──────────────────────────────────────────────


class ToolCallRecorder:
    """包装 executor，记录 ``call()`` 的**全部**工具名（含失败调用）。

    ``outcome["tool_calls"]`` 的唯一数据源：v2 的 ``AgentState`` 不写 ``execution_log``
    （见 ``eval/checks.py`` 的 ``_tool_names`` 说明），所以"图确实把任务交给了哪个工具"由 harness
    注入。失败调用同样记录——降级链走到哪一步、哪一步失败了，靠的就是这串工具名；无调用 →
    空列表，``tools_called`` 断言据此 FAIL（没数据不算通过）。
    """

    def __init__(self, executor: ToolExecutor) -> None:
        self._executor = executor
        self.calls: list[str] = []

    async def list_tools(self) -> list[ToolSpec]:
        return await self._executor.list_tools()

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append(name)
        return await self._executor.call(name, arguments)


@dataclass
class UsageAccumulator:
    """一个场景的用量累加器（**每场景新建** = 场景之间天然不串数）。

    两个口径必须分开（P4-T3 审查 I2）——

    - ``chat_calls``：**能报用量**的那部分调用（``chat``；``structured``/``stream`` 没有
      usage 面，见模块 docstring）。``estimated_calls`` 是其中"provider 没回报用量"的次数：
      这些调用**不计入** token 总数，也不填 0。
    - ``model_calls``：本场景实际发生的**全部**模型调用（chat + structured + stream）。
      token/成本只覆盖其中的 chat，它是这份数字的**诚实分母**（比例失真一眼可见）。

    拿不到数字就让它缺席（fail-loud），快照里的 0 只可能是真的 0。
    """

    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    chat_calls: int = 0  # 观察到的 chat 调用数（唯一有 usage 面的口）
    estimated_calls: int = 0  # 其中"没拿到用量"的次数（UsageInfo.estimated / None）
    structured_calls: int = 0  # 观察到的 structured 调用数（无 usage 面，只进分母）
    stream_calls: int = 0  # 观察到的 stream 调用数（无 usage 面，只进分母）

    @property
    def model_calls(self) -> int:
        """本场景发生的全部模型调用（token 数的诚实分母：只有 chat 报了数）。"""
        return self.chat_calls + self.structured_calls + self.stream_calls

    def add(self, usage: UsageInfo | None) -> None:
        """记一次 **chat** 调用的用量。``None`` / ``estimated`` = 没有可用数字。"""
        self.chat_calls += 1
        if usage is None or usage.estimated:
            self.estimated_calls += 1
            return
        self.total_tokens += usage.usage.total_tokens
        self.input_tokens += usage.usage.input
        self.output_tokens += usage.usage.output
        self.cost_usd += usage.cost.total

    def snapshot(self) -> dict[str, Any] | None:
        """可注入 outcome 的用量快照；**一个可用数字都没有时返回 ``None``**（字段缺席）。

        - ``total_tokens`` 优先用 provider 报的 total；它缺失（0）时退化为 input+output ——
          两个分项都在时报的和不是凭空造的。
        - ``input_tokens`` / ``output_tokens`` 只在两者之和 > 0 时出现：provider 只报 total 时
          分项是**未知**而不是 0（I2；"未知不是 0"是同一条规则）。
        - ``usage_source`` = ``"chat_only"``：这组数字只覆盖 chat 调用；``chat_calls`` /
          ``model_calls`` 给出所见调用数（后者含没有 usage 面的 structured/stream）。
        - ``cost_usd`` 只在 > 0 时出现：0 多半是"litellm 算不出这个模型的价"，
          与"免费"不是一回事，宁可缺席（报告侧按"未提供"读）。
        """
        if self.chat_calls == 0 or self.estimated_calls == self.chat_calls:
            return None
        total = self.total_tokens or (self.input_tokens + self.output_tokens)
        if total <= 0:
            return None
        data: dict[str, Any] = {
            "total_tokens": total,
            "usage_source": "chat_only",
            "chat_calls": self.chat_calls,
            "model_calls": self.model_calls,
        }
        if self.input_tokens + self.output_tokens > 0:
            data["input_tokens"] = self.input_tokens
            data["output_tokens"] = self.output_tokens
        if self.estimated_calls:
            data["estimated_calls"] = self.estimated_calls
        if self.cost_usd > 0:
            data["cost_usd"] = round(self.cost_usd, 6)
        return data


class UsageTrackingProvider:
    """``ModelProvider`` 装饰器：透传全部调用，记 chat 的用量、计全部模型调用的分母。

    只加一层"记账"，不改任何语义（返回值原样透传；``structured`` 的 ``model`` / ``max_retries``
    也照传）。放在这个位置而不是改模型层：评测的观测不该侵入生产代码路径（分层红线）。
    """

    def __init__(self, inner: ModelProvider, usage: UsageAccumulator | None = None) -> None:
        self._inner = inner
        self.usage = usage if usage is not None else UsageAccumulator()

    async def chat(self, messages: list[Message], *, model: str | None = None) -> ChatResult:
        result = await self._inner.chat(messages, model=model)
        self.usage.add(getattr(result, "usage", None))
        return result

    async def stream(
        self, messages: list[Message], *, model: str | None = None
    ) -> AsyncIterator[str]:
        # port 只 yield 字符串，没有 usage 面 → 记账无从下手，如实透传（不猜）；
        # 调用本身仍进诚实分母（model_calls），token 数就只能是下界。
        self.usage.stream_calls += 1
        async for chunk in self._inner.stream(messages, model=model):
            yield chunk

    async def structured(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel],
        model: str | None = None,
        max_retries: int = 3,
    ) -> BaseModel:
        # 返回 schema 实例（不是 ChatResult）→ 没有 usage 面（同 stream）；调用进分母。
        self.usage.structured_calls += 1
        return await self._inner.structured(
            messages, schema=schema, model=model, max_retries=max_retries
        )


# ── 多轮：history 摘要（与 app 同格式） ────────────────────────────────


def history_entry(task: str, answer: str) -> str:
    """一条 history 摘要：``Q: <task> / A: <answer>``、压成单行、截断 200 字符。

    与 ``app.main._build_history_entries`` 的格式**逐字一致** —— 生产的多轮上下文就是这么造的
    （/chat 落库的 turn 行 → 摘要条目 → ``AgentState.history``）。harness 逐字复刻它，
    "真实模式的多轮"才和生产的多轮是同一件事（有测试钉住这个等价）。
    """
    task_text = (task or "").replace("\n", " ").strip()
    answer_text = (answer or "").replace("\n", " ").strip()
    return f"Q: {task_text} / A: {answer_text}"[:HISTORY_MAX_ENTRY_CHARS]


def history_entries(turns: list[dict[str, Any]]) -> list[str]:
    """``[{"task": …, "answer": …}, …]`` → history 摘要列表（只留最近 5 条）。"""
    return [
        history_entry(turn.get("task") or "", turn.get("answer") or "")
        for turn in turns[-HISTORY_MAX_ENTRIES:]
    ]


# ── 场景执行 ─────────────────────────────────────────────────────────


async def run_scenario(
    scenario: Scenario,
    *,
    provider: ModelProvider,
    executor: ToolExecutor,
    tracer: Tracer,
) -> dict:
    """跑完场景的全部轮次（``prompt`` + ``follow_up``），返回**最后一轮**的 outcome。

    返回的 dict = ``run_task`` 的 state（最后一轮）+ harness 注入的
    ``tool_calls`` / ``duration_ms`` / ``token_usage``（字段名与来源见模块 docstring）。

    口径（三条都写进测试，避免"看起来是"）：

    - ``token_usage`` / ``duration_ms`` / ``tool_calls`` 是**整场景**（含所有轮次）的口径 ——
      报告要回答的是"这个场景花了多少"，把前几轮漏掉会低报成本。
    - ``checks`` 评估的是最后一轮的 state（``final_answer`` / ``result_data`` / ``stop_reason``
      …都取自最后一轮）—— 多轮的结论在最后一句回答上。
    - ``timeout_seconds`` 按**轮**强制（每轮一次预算；多轮场景不共享一个）：挂住的模型调用
      不该把整轮 acceptance 拖死。超时抛 :class:`TimeoutError`（消息带 id / 轮次 / 预算）；
      内层自己的 ``TimeoutError``（socket/HTTP 超时）**原样上抛**，不包装成场景超时。

    硬失败（模型/服务异常、超时）**不吞**：异常冒到 :func:`eval.run_eval.run_all`，那里逐场景
    捕获、打 ``[FAIL] … EXCEPTION …``、继续下一条（P4-D5；C1）。teardown 无论成败都在
    :func:`eval.run_eval._run_scenario` 的 ``finally`` 里跑。
    """
    usage = UsageAccumulator()
    tracked = UsageTrackingProvider(provider, usage)
    recorder = ToolCallRecorder(executor)
    prompts = [scenario.prompt, *scenario.follow_up]
    turns: list[dict[str, Any]] = []
    outcome: dict = {}
    start = time.perf_counter()
    for index, prompt in enumerate(prompts, start=1):
        turn_timeout = asyncio.timeout(scenario.timeout_seconds)
        try:
            async with turn_timeout:
                outcome = await run_task(
                    prompt,
                    provider=tracked,
                    executor=recorder,
                    tracer=tracer,
                    history=history_entries(turns),
                )
        except TimeoutError as exc:
            if not turn_timeout.expired():
                # 内层自己抛的 TimeoutError（socket/HTTP 超时等）原样上抛 —— 标成场景超时
                # 会把诊断指向错误的预算（P4-T3 审查 Minor）。
                raise
            raise TimeoutError(
                f"scenario {scenario.id!r} 第 {index}/{len(prompts)} 轮超过 "
                f"timeout_seconds={scenario.timeout_seconds:g}s"
            ) from exc
        turns.append({"task": prompt, "answer": str(outcome.get("final_answer") or "")})
    duration_ms = (time.perf_counter() - start) * 1000
    outcome["tool_calls"] = list(recorder.calls)
    outcome["duration_ms"] = duration_ms
    snapshot = usage.snapshot()
    if snapshot is not None:
        outcome["token_usage"] = snapshot
    return outcome


# ── real 模式的组合根与池生命周期 ──────────────────────────────────────


@dataclass(frozen=True)
class RealDeps:
    """real 模式的三个真实依赖。

    tracer 不在这里：trace 目录/报告属**输出策略**，由 ``run_eval``（和它的 tracer 参数）决定。
    """

    provider: ModelProvider
    executor: AppToolExecutor
    pool: BrowserPool  # 延迟导入（TYPE_CHECKING）：mock 模式不拉 playwright


def build_real_deps(settings: Settings | None = None) -> RealDeps:
    """真实模式的组合根：Settings → (provider, executor, pool)。

    池配置与 app（``app.main._default_app``）同源：headless / max_pages / channel（空串 → None）。
    """
    from browsepilot.app.browser.browser_pool import BrowserPool
    from browsepilot.app.browser.policy import NavigationPolicy
    from browsepilot.app.config import Settings
    from browsepilot.app.executor import AppToolExecutor
    from browsepilot.app.services.fetch import PageFetchService
    from browsepilot.app.services.search import SearchService
    from browsepilot.app.services.weather import WeatherService
    from browsepilot.model.litellm_provider import LiteLLMProvider

    settings = settings if settings is not None else Settings()
    provider = LiteLLMProvider(
        settings.default_model,
        api_key=settings.model_api_key or None,
        base_url=settings.model_base_url or None,
    )
    pool = BrowserPool(
        headless=settings.browser_headless,
        max_pages=settings.browser_max_pages,
        channel=settings.browser_channel or None,
    )
    executor = AppToolExecutor(
        WeatherService(),
        SearchService(),
        PageFetchService(),
        browser=pool,
        # I3：池与策略都从这**同一个** Settings 构造（不显式传的话 executor 自己再读一次
        # 环境，把显式配置静默覆盖掉）。
        policy=NavigationPolicy(settings.navigation_policy, settings.allowed_domains),
    )
    return RealDeps(provider=provider, executor=executor, pool=pool)


@asynccontextmanager
async def pool_lifecycle(pool: BrowserPool, executor: AppToolExecutor) -> AsyncIterator[bool]:
    """起池 → ``yield started`` → 关（**只有起来过才关**）。

    池起不来时打 WARNING 并 ``attach_browser(None)``：不变式是"executor 手里的要么是**已启动**的
    池，要么是 None（6 个浏览器工具统一 ``browser_unavailable``）"——
    与 ``app.main._bind_executor_browser`` 同一条。

    关池失败不得**掩盖** body 的异常（P4-T3 审查 Minor）：诊断优先级是"场景为什么炸"，
    close 自己的问题打 WARNING；body 没炸时 close 失败照常上抛（没有要保留的原异常）。
    """
    started = False
    try:
        try:
            await pool.start()
            started = True
        except Exception as exc:
            print(
                f"[eval] WARNING: BrowserPool.start() failed ({exc}); "
                "Tier3 工具一律 browser_unavailable —— 这些 tier3 失败是环境问题，不是任务失败"
            )
            executor.attach_browser(None)
        yield started
    except BaseException:  # 含取消/KeyboardInterrupt：任何退出路径都要归还池（finally 的替代）
        if started:
            try:
                await pool.close()
            except Exception as close_exc:
                print(f"[eval] WARNING: BrowserPool.close() failed ({close_exc})")
        raise
    else:
        if started:
            await pool.close()
