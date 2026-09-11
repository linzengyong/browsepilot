# MCP 工具问题分析（v1 期，历史存档）

> **历史声明（2026-09-11，V2.0 交付前复核添加）**：本文写于 **v1 阶段**，分析对象是 v1 的浏览器 MCP 工具
> （`get_page_structure` / `click` / `type_text` 等）。这些工具及其服务端代码已在 P5-T1 随 v1 删除
> （`backend/`、`browser_mcp/`，commit `07bc3bd`），**与 V2.0 的 `src/browsepilot/` 实现无关**；保留全文
> 仅为问题诊断思路的**文章素材**价值，不作为当前系统现状的依据。v2 的浏览器层（`browser_*` 动作 + 快照）
> 见 `docs/v2-architecture.md` §4.6；V2.0 已交付内容见 `docs/v2-release.md`。

## 一、整体诊断：你已经有了解决方案的雏形，只是它没有被串联起来
你工具集里最大的矛盾在于：

`get_page_structure`已经给出了解决“LLM 瞎猜选择器”的方案，但其他工具（`click`、`type_text`）完全没有利用它。
- `get_page_structure`能提取所有可交互元素，并生成最优选择器
- 但它的返回值是一个结构体，LLM 拿到后仍然需要从 `buttons[3].selector` 这个字段里复制粘贴
- `click(selector)` 工具签名本身没有与 `get_page_structure` 的输出建立任何约束关系

这就是问题根源：你有“眼睛”（获取页面结构），也有“手”（click/type_text），但它们之间的神经连接是断裂的。LLM 需要手动把“眼睛看到的东西”翻译成“手能用的参数”，这个翻译过程最容易出错。

## 二、按工具逐个分析
### 1. get_page_structure — 最有价值的工具，但用了就浪费了
做得好的地方：
- JS 脚本里的 `getBestSelector` 函数考虑周全（id > name > class > placeholder > aria-label），这是生产级的设计
- 过滤了不可见元素
- 限制了返回数量

问题：
- 返回的 `selector` 仍然是 CSS 选择器，LLM 复制粘贴时容易出错（比如漏掉 `.` 或 `#`）
- 返回的 `screenshot` 在非视觉模式下是多余的，在视觉模式下又和独立 `screenshot` 工具重复
- 没有为每个元素生成一个简短、唯一的引用标签，导致 LLM 在后续 `click`/`type_text` 中仍需使用冗长的选择器

**改造方向**：让这个工具成为“预观测节点”的核心，并输出一个元素映射表，供后续所有交互工具使用。

### 2. click — 问题最集中，之前已经详细分析过
2.1 核心问题：selector 参数没有约束来源。LLM 可能从 get_page_structure 复制，也可能自己编。
工具签名是：

```python
async def click(selector: str, ctx: Context) -> dict:
    """Click an element identified by a CSS selector."""
```
这里有一个核心矛盾：LLM 只知道需要一个 selector，但不知道这个 selector 应该从哪里获取。你的系统之前已经拿到了页面 DOM 并做了预先观测，但在工具描述里完全没有体现。LLM 为了完成任务，只能：
- 凭空编造一个（大概率错误）
- 或者从之前观测结果里截取一段（但观测结果如果是长文本，它很难精确复制）

2.2 等待策略过于刚性，制造虚假失败

```python
await page.wait_for_selector(selector, timeout=5000)
```
5 秒固定超时，如果页面较慢，就会抛出异常，而这个异常会被直接捕获为 “selector_not_found”，但实际上选择器本身可能是对的，只是渲染慢。LLM 会误以为选错了元素，转而尝试另一个错误选择器，导致越改越糟。

### 3. type_text — 与 click 同样的问题，还多一个隐患
与 click 相同的问题：

- `selector` 参数没有约束
- 错误信息写死 `"selector_not_found"`，不准确
- 等待 5 秒固定超时

**额外问题**：
- page.fill() 会先清空再输入，但如果 input 元素有复杂的 JS 事件绑定（比如 React 的受控组件），fill 可能失效，需要降级为 type（逐字符输入）或配合 dispatchEvent
- 工具没有说明 text 参数应该用什么格式（是否需要加 Enter？是否需要特殊字符转义？）

**改造方向**：与 click 统一使用元素引用标签，增加输入失败后的降级策略。

### 4. screenshot — 设计合理，但职责被污染
做得好的地方：

- 独立成工具，按需调用
- full_page 参数给了灵活性
- 有单独的超时处理

问题：

其他 5 个工具（navigate、get_content、click、type_text、get_page_structure、scroll）都在内部调用了 browser.screenshot() 并返回截图。截图工具的能力被分散到了所有工具中，导致：
- Token 浪费：即使 LLM 不需要看图，每个操作都返回截图
- LLM_VISION_ENABLED 的判断逻辑需要写在 6 个地方，维护成本高
- screenshot 这个独立工具的价值被稀释了

**改造方向**：截图统一收归 screenshot 工具，其他工具只在必要时返回文本描述。

### 5. navigate — 基本可用，但副作用不透明
问题：

- 返回值只有 `title` 和 `screenshot`，没有页面简要内容
- 如果导航后出现了弹窗、登录页、验证码，LLM 只能通过截图发现（视觉模式）或完全不知道（文本模式）
- `wait_until="domcontentloaded"` 可能太早，SPA 页面此时内容还没渲染

**改造方向**：返回时增加页面状态摘要（如“页面已加载，检测到 1 个弹窗”），可选等待 networkidle。

### 6. get_content — 职责混乱
问题：
- 名字是“获取内容”，但强制返回截图，职责不清
- `content[:10000]` 硬截断，文本模式下的致命隐患
- `inner_text("body")` 只拿可见文本，可能丢失一些关键的结构化信息

**改造方向**：纯文本模式下返回完整或极度宽松截断的文本；视觉模式下可配合 `screenshot` 使用，不重复返回截图。

### 7. scroll — 能用但不够聪明
问题：
- LLM 不知道滚多少像素合适，只能试
- 没有“滚到某个元素可见”的能力

**改造方向**：增加 `scroll_to_text(text: str)` 变体，直接滚动到包含特定文字的位置。

### 8. execute_script — 风险高、收益低
问题：

- filter_js_script 能做基本安全检查，但防不了 LLM 写出低效或错误的脚本
- 对于浏览器 Agent 的核心任务（点击、输入、滚动、读取），其他 7 个工具已经覆盖
- 开放此能力增加了风险面

**建议**：直接移除，或在工具描述中标注“仅在其它工具无法完成任务时使用”，并严格限制 token 消耗。

## 三、完整改造方案（按优先级排序）
### 第一优先级：建立“元素引用标签”体系（解决 80% 的问题）
改 `get_page_structure`，让它为每个可交互元素生成简短标签：

```python
# 返回格式示例
{
  "status": "success",
  "elements": {
    "E1": {"type": "input", "label": "搜索框", "placeholder": "请输入关键词", "text": ""},
    "E2": {"type": "button", "label": "百度一下"},
    "E3": {"type": "link", "label": "新闻", "href": "https://news.baidu.com"},
    "E4": {"type": "input", "label": "用户名", "placeholder": "手机号"},
    "E5": {"type": "button", "label": "登录"}
  },
  "summary": "页面包含 2 个输入框、2 个按钮、1 个链接"
}
```

改 `click` 和 `type_text`，使用元素引用而非原生选择器：
```python
async def click(element_ref: str, ctx: Context) -> dict:
    """Click an element using its reference label from get_page_structure (e.g., 'E2')."""

async def type_text(element_ref: str, text: str, ctx: Context) -> dict:
    """Type text into an input element using its reference label (e.g., 'E1')."""
```

这个改动会让你之前遇到的“选错工具、填错参数”问题大幅减少，因为 LLM 不再需要面对选择器这个黑盒。

### 第二优先级：统一截图策略，精简返回值
原则：
- 截图只由 `screenshot` 工具返回
- 其他工具不再返回 `screenshot_base64`
- `get_page_structure` 返回 `elements` 和 `summary`，不返回截图
- `navigate`、`click`、`type_text`、`scroll` 成功时只返回状态和简要文本描述

**好处**：

- Token 消耗直接降低 60% 以上
- LLM 不会被无用截图淹没
- LLM_VISION_ENABLED 的逻辑集中在一处

### 第三优先级：统一错误处理格式
所有工具的错误返回统一为：

```python
{
  "status": "error",
  "error_type": "element_not_found",  # 或 timeout / invalid_input / blocked
  "message": "元素引用 'E2'（百度一下按钮）未在页面中找到，当前可用元素为：E1, E3, E4, E5",
  "suggestion": "请从当前可用元素中重新选择",
  "available_refs": ["E1", "E3", "E4", "E5"]  # 仅在元素未找到时返回
}
```
这样 LLM 拿到错误后不需要“猜”，可以直接从 `available_refs` 里选一个新的。

### 第四优先级：工具职责清理
| 工具	| 改造后职责	| 视觉模式	| 文本模式
|------|---------|------|------|
| get_page_structure	| 获取可交互元素映射表	| 不返回截图	| 不返回截图
| screenshot	| 获取视觉快照	| 按需调用	| 不调用（或返回占位）
| click	| 点击指定元素引用	| 返回操作结果 + 可选调用 screenshot	| 返回操作结果 + 页面变化文本摘要
| type_text	| 向指定元素引用输入文本	| 同上	| 同上
| navigate	| 导航到 URL	| 返回 title + 页面摘要	| 返回 title + 页面摘要
| get_content	| 获取页面完整文本内容	| 可返回结构化内容	| 返回完整文本（不截断）
| scroll	| 滚动页面或滚动到指定文本	| 返回滚动后状态	| 返回滚动后可见文本摘要
| execute_script	| 建议移除	| —	| —