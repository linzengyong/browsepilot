# V2.0 P3 交付报告（P3 Task 8 验收）

- 分支：`v2.0`（P3 提交范围 `e455a9f..HEAD`，17 个功能/文档提交 + 本报告 1 个；P2 终点 `e455a9f`）
- 验收日期：2026-09-10（本地 23:19–23:25，UTC 15:19–15:25）
- 范围：P3 Tier3 浏览器层重建 —— 观察/动作原语（BrowserPool + AX 快照 refs）、多重定位回退 + 工具级重试、URL 安全策略 D4/D7（WHATWG/UTS46 对齐）、tier3 路由与策略链、HITL 桥接（登录/验证码检测 → P2 interrupt）、真实浏览器冒烟与文章素材
- 验收结论：✅ **全量 616 passed + ruff/mypy 全绿；eval mock 3/3；eval real 2/3（p0-search 为已知 ddgs 结果漂移，与 P2 D1 同因，非回归）；T6 真实浏览器关键场景全 PASS（SKIP 项如实记录）；`backend/`、`browser_mcp/` 零改动（冻结延续）；本任务（T8）仅新增 docs，无 `src/`、测试改动**

---

## 一、全量测试与工程化门禁（Step 1）

工作目录 `D:/AI_Agent_Demo/browsepilot`，`PYTHONIOENCODING=utf-8`。

```text
$ uv run pytest tests/ -q
616 passed, 1 warning in 18.78s

$ uv run ruff check src/ eval/ tests/
All checks passed!

$ uv run mypy src/browsepilot/
Success: no issues found in 32 source files
```

- 增长轨迹：P2 验收 171 → P3 验收 **616**（+445；阶段内 171→176→203→386→432→529→604→616，含各 review 修复轮新增测试）。
- 唯一 warning：`backend/app/config.py:19` PydanticDeprecatedSince20（冻结旧代码，与 P1/P2 相同，不修）。
- 测试套件无真实浏览器依赖（教训 §1 延续）：P3 全部浏览器测试使用 fake page/pool 或纯函数；真浏览器只在 T6 冒烟与加固复验脚本中出现。

## 二、评测回归（Step 2）

### 2.1 mock 模式（确定性，主验收证据）

CLI 注记：当前 `eval/run_eval.py` 只定义 `--mock`（`--mode` 参数不存在；真实模式为无参默认）。派发命令 `--mode mock` 实测报 `unrecognized arguments: --mode mock`，因此按真实 CLI 执行；两条命令均已记录（见偏差 D5）。

```text
$ uv run python -m eval.run_eval --mock
[browsepilot eval v0.2] 3 scenarios loaded
  [PASS] p0-read-page: keywords hit=['Python']
  [PASS] p0-search: keywords hit=['2024']
  [PASS] p0-weather: city='北京' expected='北京'; temperature_c=26.0 (number)
[eval] 3/3 passed
```

### 2.2 real 模式（真模型 + 真实网络，逐场景记录）

前置：shell 进程内导出 `BP_MODEL_API_KEY` / `BP_MODEL_BASE_URL`（值取自 `browsepilot/.env` 的 `OPENAI_API_KEY` / `OPENAI_BASE_URL`，仅环境变量传递，未写文件、未入库；本报告只出现变量名）。命令：`uv run python -m eval.run_eval`。

```text
[browsepilot eval v0.2] 3 scenarios loaded
  [PASS] p0-read-page: keywords hit=['Python']
  [FAIL] p0-search: keywords hit=[]
  [PASS] p0-weather: city='北京' expected='北京'; temperature_c=21.4 (number)
[eval] 2/3 passed
```

`p0-search` 根因已定位（单场景复跑 + trace 取证，非代码回归）：

- trace 全链路正常：`classify → decision(intent=search, tier=tier1, confidence=1.0) → tool_call search status=success → answer`。
- ddgs 本次返回的结果与查询「搜索一下 Python 3.13 正式发布时间」无关（Python 3.15.0rc2 发布页、2018 年安装指南、OSCHINA 标签页等）；模型如实回答「根据现有数据，无法找到 Python 3.13 正式发布时间的相关信息。」，最终答案不含检查关键字 `"2024"`。
- 这与 P2 验收报告 §2.2 / 偏差 D1 完全同因（ddgs 结果内容漂移；P2 两次实时运行同场景均 FAIL，本次为连续第三次）。属外部依赖漂移，**不阻塞**（派发指令明确）；mock 3/3 为主证据。

## 三、Tier3 真实浏览器冒烟（T6）摘要

完整记录：`browsepilot/docs/v2-p3-smoke.md`（423 行，commit `a12cc22`，真浏览器一律 `channel="msedge"` + headless）。逐场景：

| # | 场景 | 结果 |
|---|------|------|
| S1 | 池启动 + example.com + 快照 refs（ref_count=5） | PASS |
| S2 | bing 搜索（snapshot→type→click 提交→URL 变化→30 refs） | PASS |
| S3 | 策略门（metadata IP / 127.0.0.1 / ::1 / `file:` / `javascript:` 全拒；executor 路径 `pages_created_delta=0`） | PASS |
| S4 | example.com 点击链接 + 动作后复查放行 | PASS（含一次跨页假失败观测） |
| S5 | 登录页检测（真实 zhihu `/signin` → login/high + executor `needs_human=true`） | PASS |
| S5b | 任务书候选 github/google 登录页 | SKIP（本机网络 15s 超时） |
| S6 | 失败导航后复用池页（异常复现） | PASS（复现成功，见加固 F4） |
| S7 | bing 提交控件两路（click ref vs Enter） | PASS |
| L2 | 真模型 + 真 app `/chat/stream` 走 tier3（SSE `phase→answer→done`，7.5s） | PASS |
| L1.5 | API 层 HITL：zhihu 登录墙 → SSE `human_pending`（3.6s） | PASS |
| — | 真实验证码 / 确认弹窗页 | SKIP（无稳定可复现页面，不构造假页面） |
| — | `/chat/resume` 人工恢复后继续 | SKIP（P3 范围外，P2 已覆盖） |
| — | 前端（Vue）层 HITL | SKIP（仅 API 层断言；前端非本任务范围） |

## 四、Tier3 trace 证据落盘（Step 3）

T6 的真实 trace 原在 `%TEMP%`（不在仓库），文章-5 素材因此引用不可验证路径。本次仍能在盘上定位到两份原始 trace 并落盘入库：

| 来源（采集时，2026-09-10 14:57 UTC） | 内容 | 行数 |
|---|---|---|
| `%TEMP%\t6-e2e-qn_ad4uc\traces\t6-e2e.jsonl` | L2 真模型 tier3 全链路（classify decision tier3 → `browser_snapshot` error → `llm_call` structured → `browser_open` success → answer → llm chat） | 11 |
| `%TEMP%\t6-hitl-sh8bybzr\traces\t6-hitl.jsonl` | L1.5 HITL：`browser_open` success → **hitl node_start/node_end**（同 span，0.38ms） | 10 |

- **落盘文件**：`browsepilot/docs/v2-p3-smoke-trace.jsonl` —— 两份原始 JSONL 逐行合并（不加注释行，保持可 `json.loads` 逐行解析），共 **21 行 / 5,008 字节**；`trace_id` 字段保留 `t6-e2e` / `t6-hitl` 区分来源。
- **内容边界**：仅浏览器/agent 事件（`node_start`/`decision`/`node_end`/`tool_call`/`llm_call`），无密钥字段。
- **密钥扫描**：对落盘文件 `grep -Ei 'sk-|api[_-]?key|bearer|password|token|secret'` → 无命中；文中 `session_id` 为随机 UUID，非凭据。
- **文章素材引用**：`docs/v2-articles/article-5-material.md`「数据在哪」表 + 注释行已补该入库副本路径（仅此一处引用行，见 commit）。

## 五、P3 交付检查单（计划 §P3 交付检查单，逐项）

| 检查单条目 | 结果 | 证据（commit / 文档 / 测试） |
|---|---|---|
| msgpack 注册 + degradation 去重（P2 MUST-FIX 2 项） | ✅ PASS | `31936c6` + `8d60d3f`；`tests/test_app/test_main.py::test_lifespan_checkpointer_registers_msgpack_serde`；`tests/test_agent/test_graph.py::test_merge_degradation_dedupes_same_strategy_and_error` / `test_degradation_log_not_duplicated_when_execute_replays`（resume 重放不翻倍） |
| BrowserPool + AX snapshot refs（边界截断 ≤30） | ✅ PASS | `8e48287`（并发上限/启动泄漏/幻影 ref 修复 `f002bc4`）；`tests/test_app/test_browser_snapshot.py`（31 个 test 函数）；`snapshot_to_refs(max_refs=30)` + `page_snapshot_refs()`；冒烟 S1 refs=5、S2 30 refs |
| 定位多重回退（ref→role/name→CSS→text）+ 动作重试/JS 回退 | ✅ PASS | `c1b098a`（真实候选序列 + 参数强转 `bd4f7b2`）；`tests/test_app/test_browser_actions.py`（51）；冒烟 S2/S4/S7 实测 `resolution`/`attempts`/`js_fallback` |
| navigate 条件等待（D3）；URL 安全策略（D4 默认 https） | ✅ PASS | `70dc8ca` → `9842957` → `f294a9c` → `ba2ece9`；`tests/test_app/test_browser_policy.py`（39）；executor 每次导航前把关 + 重定向后复查（`test_browser_open_gates_url_before_navigating`、`test_browser_open_revalidates_the_post_redirect_url`、`test_browser_click_navigation_is_revalidated`）；冒烟 S3 |
| 6 浏览器工具进 executor + tier3 策略链 | ✅ PASS（有已知限制） | `f907680` + `66cc03f` + `8dbbb7b`；`tests/test_app/test_executor_browser.py`（57，含 `test_browser_tools_listed_with_parameters`）、`tests/test_agent/test_graph.py`（`test_browser_task_snapshots_first_then_opens`、`test_browser_task_without_browser_degrades_honestly`、`test_read_page_escalates_to_browser_when_fetch_fails`）、`tests/test_agent/test_router.py`（16）、`test_main.py` lifespan 接线测试。**限制**：策略链 `["browser_snapshot","browser_open"]` 只到「打开页面」，交互步骤未入链（遗留 2） |
| 登录/验证码检测 → HITL 自动接管（复用 P2 interrupt） | ✅ PASS（有已知限制） | `089733a` + `5ae1a9c`（可见文本采样 + confidence 门控）；`tests/test_app/test_browser_detect.py`（25）+ `test_executor_browser.py::test_high_confidence_intervention_still_pauses` / `test_browser_open_on_login_page_carries_hitl_payload`；冒烟 S5 + L1.5。**限制**：`/chat/resume` 人工恢复后继续未在 P3 实测（遗留 6） |
| 真实浏览器演示场景冒烟记录 | ✅ PASS | `a12cc22` → `docs/v2-p3-smoke.md`（本文 §三 摘要；L2/L1.5 均真模型 + 真浏览器） |
| 全量 pytest + ruff/mypy 绿；backend/browser_mcp 零改动（冻结延续） | ✅ PASS | 本文 §一、§九 |
| 文章素材补充 | ✅ PASS | `6c41509`（article-5 +142 行、article-3 +123 行、README 状态更新）；T8 追加落盘 trace 样本（§四），补齐 T7 报告标注的「证据链在 `%TEMP%`」缺口 |
| Tier3 失败时「问题暴露」原则：不静默、可重试、可 HITL | ✅ PASS | `5ae1a9c`（失败导航丢页重试、`_quarantine_page` 隔离、confidence=medium 只记录不暂停）；`test_browser_task_without_browser_degrades_honestly`（无池时结构化降级 + 明确文案）；冒烟 §5 逐条异常记录 |

**计划「明确不做」项确认未做**：站点专用适配器（京东/淘宝全流程）、多页/多标签管理、可视化回放 UI、旧 `browser_mcp/` 删除 —— 均留 P4/P5。

## 六、SSRF 加固故事（C1 → C2 → UTS46 全类对齐）

P3 安全模块（`src/browsepilot/app/browser/policy.py`）经历了三轮 review 驱动的加固，每轮都由「策略放行、浏览器却打内网」的解析器差分漏洞触发：

1. **C1 反斜杠权威混淆（commit `9842957`）**：`urlsplit` 不把 `\` 当 authority 终止符，而 WHATWG/Chromium 对 special scheme 会。评审给出实弹：`https://169.254.169.254\latest/meta-data/`（云元数据 SSRF）与 `https://127.0.0.1\@example.com/`（allowlist 绕过）当时 `validate=(True, '')`。修复：host 判定前按 WHATWG special-scheme 语义把 `\` 归一化为 `/`（**交给导航的原始 URL 不动**）；补反斜杠在真实 authority 之后不误伤的用例。
2. **C2 Unicode 点变体（commit `f294a9c`）**：WHATWG 会把 `。`(U+3002)/`．`(U+FF0E)/`｡`(U+FF61) 映射为 `.`，policy 只按 ASCII `.` 切分 → `https://169。254.169.254/latest/meta-data/`、`https://0x7f。0.0.1/`、`https://１６９。２５４.169.254/` 均被放行，且原 docstring 的「不会误放」是假保证。实施时主动扩大探测（41 个类私有 payload 实测 25 个可绕过，包括软连字符 U+00AD、零宽空格 U+200B、全角字符、U+A7F1），于是从「补 3 个点变体」升级为**系统性对齐 UTS46 17.0.0**。
3. **UTS46 全类对齐 + 不变量固化（commit `ba2ece9`）**：主机归一化 = 点变体映射 + NFKC 兼容字形 + UTS46 `ignored` 全 294 码点删除 + `\ua7f1`→`s`；并新增不变量测试（逐码点表 vs 现装 `idna` 库、294 码点全量校验，删除 VS 范围可触发 RED）、IPv4 混淆字面值检测收紧到最后一个 label、拒因回显有界化。

**最终策略保证（WHATWG 对齐 + fail-closed）**

- `allow_https`（默认）：只放行 http/https；`file:`/`javascript:`/`data:`/`ftp:` 等全拒；私有/回环/链路本地/保留 IPv4+IPv6、`localhost`、`*.local`/`*.internal`/`*.home.arpa`、单标签主机名、整数/八进制/十六进制 IPv4 字面值全拒；非法 URL fail-closed。
- `allowlist`：scheme 门禁之上仅放行白名单（精确或子域后缀；`example.com` 放行 `www.example.com`、不放行 `notexample.com`）；IP 字面值/混淆 IPv4/裸标签条目**只做精确匹配**（防止 `["1.1.1"]` 后缀匹配放行 `127.1.1.1`）；空/缺失白名单 → 全拒（`allowlist_empty`，构造不抛错）；不支持通配符（写错即永不匹配）。
- 未知 `mode` 构造即 `ValueError`，绝不静默降级为更宽松策略；拒因是 `code: detail` 稳定契约且回显截断有界。
- **反向保证**（逐条核对 UTS46 17.0.0 `IdnaMappingTable.txt`）：凡 Chromium 会解析成字面 IP/本机名的形态（点变体、全角/兼容数字、`ignored` 码点、`\ua7f1`）都会按 IP/本机名拒绝；`mapped`/`disallowed_STD3_mapped` 6377 码点中映射结果含 ASCII 的 1673 个，NFKC 复现 1669，其余 3 个相关码点显式映射。

**已知限制（诚实边界）**

- **不做 DNS 解析**（零网络、纯标准库）：`allow_https` 下「公网外观但解析到内网」的域名（如 `db-01.corp.example.com`）会放行；要挡住请用 `allowlist`。这是设计取舍，非缺陷。
- **不做完整 IDNA/UTS46**：不做 punycode 编解码、不对齐 IDNA2003↔UTS46 其余差异（`ß`/词尾 `ς` 等）；归一化后仍含非 ASCII 的主机按字面判定——UTS46 `valid`/`mapped` 的字形在 Chromium 里是 DNS 名（归入「不做 DNS 解析」取舍）；`disallowed` 字形会让 Chromium 直接拒绝该 URL，本层可能放行但方向安全（浏览器根本不会导航）。
- **idna 映射集钉在 UTS46 17.0.0**：上游表更新后需重新核对（测试用 `idna` 库现装版本做 oracle，已固化不变量）。
- 不做端口范围限制；校验当次调用 URL，不跟踪单次调用内的重定向/子资源后续跳转（T4 在 navigate 后与 click 后复查；redirect-then-navigate-failure 路径不复查 → 遗留）。
- `allowlist` 模式的信任边界就是运维显式写下的域名：显式写 `localhost`/`127.0.0.1` 即有意放行本机（本机调试逃逸口）。

## 七、与计划的偏差（SDD ledger 摘要）

| # | 计划 vs 实际 | 说明 / 证据 |
|---|---|---|
| D1 | **T4-wire 为新增任务**（计划 T4 brief 漏写 app 装配） | T6 冒烟暴露「运行中的 app 仍无 BrowserPool，tier3 全部 `browser_unavailable`」；控制器追加 T4-wire：`Settings` 加 browser_headless/browser_max_pages + lifespan 构造/启动/注入/关闭池（start 失败 → `browser=None` 不崩）→ commit `66cc03f`；测试 `test_lifespan_starts_browser_pool_and_injects_it_into_executor` / `test_lifespan_browser_start_failure_degrades_to_browser_none` / `test_default_app_wires_browser_pool_into_executor` |
| D2 | **T5-fix / T6-fix 合并为收尾加固轮**（计划无此任务） | `5ae1a9c` 一次闭 4 项：F1 检测改采可见文本（`inner_text`，消除隐藏 DOM/script 误报 HITL）、F2 confidence 门控（high 才暂停，medium 仅记录）、F3 `Settings.browser_channel`（`BP_` 前缀）+ `_default_app` 透传、F4 失败页丢弃 + 池 `discard()` + `new_page` 错误页过滤。9 文件 +544/−118；msedge 真浏览器复验 F1/F3/F4 全 PASS |
| D3 | **T7 brief 缺失**（`task-7-brief.md` 未生成；T8 同样无 `task-8-brief.md`） | 按派发 prompt + `progress.md` 执行；T7 报告已显式记录 |
| D4 | 计划 TDD 步数 vs 实际修复轮 | T1 1 轮修复（`f002bc4`）、T2 1 轮（`bd4f7b2`）、T3 4 轮（`70dc8ca`→`9842957`→`f294a9c`→`ba2ece9`，含 C1/C2 两个 Critical）、T4 1 轮（`8dbbb7b`）；T4 按控制器提速决议跳过独立 re-review（记账，由 final review 覆盖） |
| D5 | 评测 CLI：派发写 `--mode mock|real`，实际 `--mock`（real = 无参） | `--mode mock` 实测 `unrecognized arguments`；已按真实 CLI 执行并双记录（本文 §2.1） |
| D6 | 计划 T6 示例任务措辞不可用 | `_URL_RE` + read_page 关键词先行 → 「打开 example.com 看看标题」实走 tier2；tier3 E2E 改用「在 www.bing.com 的搜索框里搜索 …」（冒烟 §5.4），已作为 P4 路由调优输入 |
| D7 | 冒烟文档部分章节记录于加固之前 | `docs/v2-p3-smoke.md` §5.1（错误页竞态）/§5.6（BROWSER_CHANNEL 未接线）是 `a12cc22` 时点记录；HEAD 上两者已由 `5ae1a9c` F3/F4 修复并有测试钉住。smoke 文档本身未回改（保持当时证据原样），以本报告 + 测试为准 |

## 八、遗留清单（carry-over to P4）

以下来自 SDD ledger / T6 冒烟 / T5-T7 报告，均**未在 P3 修复**：

1. **click 跨页假失败**：`browser_click` 报 error/timeout（attempts=3 + JS 兜底）但 URL 已变化 → 动作层把「点击成功但页面在导航中」过报失败（smoke §5.2）。
2. **tier3 链只到 open**：`_STRATEGY_CHAIN["browser_task"] = ["browser_snapshot","browser_open"]`，交互任务（type/click）未入链，`stop_reason=completed` 但只完成第一步（smoke §5.3）。
3. **`_quarantine_page` release 回 idle**：隔离页归还池后仍可被复用（T4 记账；F4 已部分缓解错误页复用）。
4. **`aclose()` 未接 lifespan**：池的 `aclose()` 未被 lifespan 调用（T4/T5 记账）。
5. **answer 节点不读 `human_response`**：resume 后最终答案可能仍说「请手动登录」（T5 review 记录；T6 应断言，未做）。
6. **`/chat/resume` 在 P3 未测**：T6 SKIP；resume 后继续链路留 P4 验证。
7. **tier3 `all_strategies_failed` 无真实样本**：L2 成功、L1.5 转 HITL，失败链渲染仅有代码 + P2 对照（T7 报告 §不可得 2）。
8. **detect 的 `role="dialog"` 结构规则已删**：F1 改可见文本后标签级判定不可用；仅保留具体短语的 medium 分支（`detect.py` 注释 F1）。
9. **运维注记 `BP_BROWSER_CHANNEL`**：v2 `Settings` 只读 `BP_*` 前缀；仓库 `.env` 只有 v1 名 `BROWSER_CHANNEL`（被 `extra="ignore"` 静默忽略）→ 部署需写 `BP_BROWSER_CHANNEL=msedge`（或在镜像内 `playwright install chromium` 补齐 rev 1217）。`.env.example` 未改（工作树有用户自己的编辑）。
10. 其余 ledger Minor（不逐条展开，详见 `progress.md`）：redirect-then-navigate-failure 不复查；强关键词被问题式短语触发（「为什么京东比淘宝便宜」→ tier3）；无 `press`/`submit` 键盘原语；单 current page 共享（跨会话无隔离）；`scroll(page,"down",inf)` OverflowError；`waited` 信号恒 False；JS 回退成功语义（已派发≠已生效）。

## 九、范围与冻结确认（out-of-scope）

```text
$ git diff --stat 92ed2f1..HEAD -- browsepilot/backend browsepilot/browser_mcp     # 派发指定检查（覆盖 P2+P3）
（空输出）

$ git diff --stat e455a9f..HEAD -- browsepilot/backend browsepilot/browser_mcp     # P3 段单独检查
（空输出）
```

- `backend/`、`browser_mcp/` **零改动**，冻结延续至 P5（`tests/test_architecture.py` 守卫 agent 层不 import `browsepilot.app`/`browser_mcp`/**playwright**，并守卫 `app.browser` 包不 import `browsepilot.agent`）。
- P3 全部改动限于 `browsepilot/src/`、`browsepilot/tests/`、`browsepilot/docs/`（26 文件，+7608/−44）；`browsepilot/eval/` 零改动（场景与运行器在 P3 未动）。
- 本任务（T8）**仅新增 docs**：`docs/v2-p3-report.md`、`docs/v2-p3-smoke-trace.jsonl`，以及 `docs/v2-articles/article-5-material.md` 的 1 处引用行。
- 工作树另携带会话前既有的无关未提交修改（`CLAUDE.md`、`README.md`、`browsepilot/.env.example`、`browsepilot/backend/app/config.py`）与未跟踪文件——非 P3 产出，未触碰、未提交。

## 十、无密钥声明

- 本报告与落盘 trace 不含任何密钥/token；环境变量只出现名称（`BP_MODEL_API_KEY`、`BP_MODEL_BASE_URL`、`BP_BROWSER_CHANNEL`）。
- 真实评测/冒烟所用的 key 仅在 shell 进程内导出（值取自 `.env` 的 v1 变量映射），未写入任何文件。

## 十一、提交

本任务提交仅含：

- `browsepilot/docs/v2-p3-report.md`（本文件）
- `browsepilot/docs/v2-p3-smoke-trace.jsonl`（T6 两份真实 trace 合并落盘，21 行 / 5,008 B）
- `browsepilot/docs/v2-articles/article-5-material.md`（仅「数据在哪」引用行）

提交信息：`docs: P3 delivery report`

## 十二、P3 收口修复（本报告之后、P4 之前）

提交信息：`fix(agent+app): P3 final-review must-fixes (HITL resume, tier3 completion, error attribution)`
（P3 whole-branch final review 的 4 个 MUST-FIX-BEFORE-P4 + 便宜的 Important/Minor）。

- **§八 遗留清单的变化**：第 1（click 跨页假失败）、3（`_quarantine_page` release 回 idle）、
  5（answer 不读 `human_response`）条已关闭；第 2 条**部分关闭**（链改为先导航后观察，只导航到页面时
  不再假报 `completed`，改报 `partial`；但 `type`/`click` 仍未入链——多步交互留 P4）；
  第 6 条补了 HTTP 层 `/chat/resume` 测试（真实浏览器里的 resume 仍留 P4）；
  第 4（`aclose()` 未接 lifespan）与 7-10 仍留 P4。
- **新增/变更的语义**（P4 读报告时要知道）：tier3 链改为**先导航后观察**（`browser_open` → `browser_snapshot`），
  未与任务目标匹配的观察结果显式标 `partial`（`stop_reason="partial"`，不再报 `completed`）；
  `chrome-error://` 按**导航失败**归因（不再当策略违规）；`browser_type`/`browser_snapshot` 也复查 URL；
  违规页 `discard()`（不回 idle）。
- **本次提交后已过时的引用**（P3 交付时的事实，原文保留不删）：§五 检查单第 6 行与 §八 第 2 条里的
  `_STRATEGY_CHAIN["browser_task"] = ["browser_snapshot","browser_open"]` 已是
  `["browser_open","browser_snapshot"]`；测试 `test_browser_task_snapshots_first_then_opens` 改名为
  `test_browser_task_navigates_then_observes_without_fake_degradation`。`docs/v2-p3-smoke.md` 与
  `docs/v2-articles/article-3/5-material.md` 里同样按旧链记录（不在本次改动范围，待统一勘误）。

### P4 评测前置（运维，I5）

1. **真实 HITL 必须有头浏览器**：`BP_BROWSER_HEADLESS=false` —— headless 下没有人能登录/过验证码，
   HITL 场景只能拿到假结果（页面停在登录墙，人工反馈无从产生）。
2. **浏览器通道**：`BP_BROWSER_CHANNEL=msedge`（或在镜像内 `playwright install chromium` 补齐 rev 1217）——
   本机 Playwright 1.59 默认要的 chromium rev 不在缓存里。
3. **harness 必须注入浏览器池 + 逐场景隔离**：`eval/run_eval.py` real 模式现在自己构造 `BrowserPool`
   （与 app 同一个组合根来源），并在每个场景后 `executor.aclose()`。不注入 → 每个 tier3 场景都 `browser_unavailable`
   （接线问题会被读成任务失败）；不隔离 → 下一个场景可能拍到上一个场景留下的页面（报 `completed` 却答非所问）。
   池起不来时 harness 打 `WARNING` 并降级（tier3 一律 `browser_unavailable`，Tier1/2 照常评测）。
