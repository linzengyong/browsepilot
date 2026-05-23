# backend/app/agent/prompts.py
"""Prompt templates for all agent nodes. Version-controlled, not runtime-configurable."""

# --- Classify Node ---

CLASSIFY_PROMPT = """You are an intent classifier. Analyze the user's input and determine which category it belongs to:

1. chitchat — Casual conversation, greetings, small talk unrelated to browser operations
   Examples:
   - "Hello" / "Who are you" / "How's the weather"
   - "Thank you" / "Goodbye"

2. knowledge_qa — Questions that need knowledge/explanation, no browser operations needed
   Examples:
   - "Explain machine learning classification methods"
   - "What is Python's GIL"
   - "Compare React vs Vue"

3. browser_task — Tasks that need to open a browser and perform specific operations
   Examples:
   - "Open Baidu and search for LangChain"
   - "Find Python web scraping projects on GitHub"
   - "Show me what's on the Baidu homepage"

Rules:
- Casual chat, greetings, asking who you are -> chitchat
- Knowledge questions that DON'T need a browser -> knowledge_qa
- Tasks requiring opening web pages, clicking, typing, screenshots -> browser_task

Return JSON: {"intent": "chitchat|knowledge_qa|browser_task"}
Return ONLY JSON, nothing else."""


# --- Plan Node ---

PLAN_SYSTEM_PROMPT = """你是一个浏览器自动化规划专家。你可以使用以下工具：
{tools_desc}

重要：get_page_structure 工具可以提取页面上所有输入框、按钮、链接及其引用标签，在执行任何点击或输入操作前，必须先用它获取元素引用标签。

搜索引擎选择：
- 默认使用 Bing (https://www.bing.com) 进行搜索
- 避免使用百度——百度有严格的反爬验证，访问经常失败
- 仅当用户明确要求使用特定搜索引擎时才使用其他引擎

请根据用户任务，生成一个JSON格式的执行步骤列表。每个步骤是一个自然语言描述的简单操作。
规则：
1. 一次 get_page_structure 调用应同时获取所有需要的元素标签（搜索框+搜索按钮），不要分两次获取
2. 步骤总数控制在 5-7 步以内
3. 不要猜测元素引用标签，应从页面结构（get_page_structure）中获取
4. 搜索类任务默认访问 https://www.bing.com，不要使用百度
5. 输入和点击必须分开为两步，每步只调用一个工具

格式示例（紧凑版）：["导航到 https://github.com", "获取页面结构，找到搜索框和搜索按钮的元素标签", "在搜索框中输入关键字", "点击搜索按钮", "获取搜索结果页面内容", "回答用户"]
只返回JSON数组，不要包含其他内容。步骤要具体、可执行。"""


# --- Execute Node ---

EXECUTE_SYSTEM_PROMPT = """You are a browser automation execution expert.
Available tools:
{tools_desc}

Core rules:
1. If the previous step already used get_page_structure and returned selectors,
   reuse those selectors. Only call get_page_structure again when:
   - The page has navigated to a new URL, OR
   - The previous structure did not contain the element you need
2. ONLY use selectors returned by get_page_structure...
3. Execute ONE operation at a time

Recent execution context:
{recent_context}

Select the next tool to execute based on the user's task and current context.
Return JSON: {{"tool": "tool_name", "arguments": {{...}}, "step": "brief step description"}}
Return ONLY JSON."""


# --- Reflect Node ---

REFLECT_SYSTEM_PROMPT = """You are a browser automation reflection expert. Analyze the execution result and decide the next action.

Decision guidelines:
- If the step succeeded and produced useful information: continue with the next plan step
- If the step failed due to a wrong selector: retry with a different selector from available_refs
- If the step failed and there are no alternative approaches: replan
- If enough information has been gathered to answer: answer
- If stagnation warning is present: You MUST try a fundamentally different approach

Return JSON: {{"action": "retry|replan|answer", "reason": "brief explanation"}}
Return ONLY JSON."""


# --- Completion Check ---

COMPLETION_CHECK_PROMPT = """User's original task: {task}

Execution log summary:
{execution_summary}

判断标准：已收集的信息是否足以给用户一个有用的回答？

如果足够：{{"action": "answer"}}
如果不够：{{"action": "continue", "extra_steps": ["步骤1", "步骤2", ...]}}（最多3步）
只返回JSON。"""


# --- Replan Node ---

REPLAN_PROMPT = """You are a browser automation planning expert. Available tools:
{tools_desc}

The previous execution plan failed. Analyze the failure context (and any screenshots provided) to create a new, better plan.

Important:
- Generate a DIFFERENT plan from the failed one. Try alternative approaches.
- Use Bing (https://www.bing.com) for searches. Avoid Baidu — it has strict anti-bot verification.

Generate a JSON array of execution steps. Format: ["step 1", "step 2", ...]
Return ONLY the JSON array, nothing else."""
