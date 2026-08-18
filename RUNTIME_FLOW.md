# Bear Code：从命令输入到文本输出的完整调用流程

本文描述 Bear Code 收到一条普通自然语言命令后，如何完成启动、模型调用、工具执行、流式文本输出和会话保存。

文中的“每一个函数”指这条运行链路上由 Bear Code 项目定义的函数。Python、`asyncio`、Rich、Anthropic/OpenAI SDK 等第三方库内部调用不展开。标有“条件”的函数只在对应分支发生。

## 1. 两种输入入口

### 1.1 一次性命令

```bash
python -m agents.main "读取 requirements.txt 并解释依赖"
```

调用主干：

```text
Python 执行 agents.main
└─ main.main()
   ├─ main.parse_args()
   ├─ main._load_env_file()
   ├─ main._resolve_permission_mode()
   ├─ main._resolve_api_config()
   │  ├─ main._clean_env()
   │  └─ main._is_anthropic_compatible_base_url()       [有 API Base URL]
   ├─ agent.Agent.__init__()
   └─ asyncio.run(main.run_one_shot())
      ├─ agent.Agent.chat()
      └─ agent.Agent.drain_background_skill_tasks()
```

命令行中位置参数 `prompt` 非空时，`main()` 将所有片段用空格拼成字符串，然后进入 `run_one_shot()`。模型完成最终回复后进程退出。

### 1.2 REPL 交互输入

```bash
python -m agents.main
```

调用主干：

```text
Python 执行 agents.main
└─ main.main()
   ├─ 与一次性命令相同的参数、配置和 Agent 初始化
   └─ asyncio.run(main.run_repl())
      ├─ agent.Agent.set_confirm_fn()
      ├─ agent.Agent.set_plan_approval_fn()
      ├─ ui.print_welcome()
      └─ while True
         ├─ ui.print_user_prompt()
         ├─ input()
         └─ agent.Agent.chat()                            [普通自然语言输入]
```

`run_repl()` 每轮读取一行。`exit`/`quit` 调用 `ui.print_goodbye()` 后退出；以 `/` 开头的内置命令在 REPL 本地分派，普通文本才进入 `Agent.chat()`。

REPL 和一次性命令的区别只在输入与退出方式。两者最终都调用同一个 `Agent.chat()`，模型及工具运行链路完全相同。

## 2. 启动与配置解析

入口文件是 `agents/main.py`。

### 2.1 参数与 `.env`

`main.main()` 依次调用：

1. `parse_args()`：解析 prompt、模型、权限模式、费用和轮次限制等参数。
2. `_load_env_file()`：用 `find_dotenv(usecwd=True)` 从当前工作目录查找 `.env`，再用 `load_dotenv()` 加载，但不覆盖进程中已经存在的环境变量。
3. `_resolve_permission_mode()`：按 `--yolo`、`--plan`、`--accept-edits`、`--dont-ask` 选择 Agent 内部权限模式。
4. `_resolve_api_config()`：解析 API Base URL、API Key 和后端类型。
   - 每个候选环境变量先经过 `_clean_env()` 去除空白值。
   - 有 Base URL 时调用 `_is_anthropic_compatible_base_url()` 检查 URL path。
   - path 以 `/anthropic` 结尾或包含 `/anthropic/` 时选择 Anthropic SDK；其他非空 URL 选择 OpenAI SDK。
5. 模型名按 `--model`、`MODEL`、`deepseek-chat` 的优先级确定。

### 2.2 创建 Agent

`main()` 调用 `Agent.__init__()`。初始化期间的项目函数调用是：

```text
Agent.__init__()
├─ agent._get_context_windows()
├─ Agent._resolve_thinking_mode()
│  ├─ Agent._model_supports_thinking()                   [开启 --thinking]
│  └─ Agent._model_supports_adaptive_thinking()          [模型支持 thinking]
├─ mcp_client.McpManager.__init__()
├─ prompt.build_system_prompt()
│  ├─ prompt.get_git_context()
│  ├─ prompt.load_claude_md()
│  │  ├─ prompt._resolve_includes()                      [存在 CLAUDE.md]
│  │  │  └─ 内部 _replace()                             [存在 @include]
│  │  └─ prompt._load_rules_dir()
│  │     └─ prompt._resolve_includes()                   [存在规则文件]
│  ├─ memory.build_memory_prompt_section()
│  │  ├─ memory.load_memory_index()
│  │  │  └─ memory._get_index_path()
│  │  │     └─ memory.get_memory_dir()
│  │  │        └─ memory._project_hash()
│  │  └─ memory.get_memory_dir()
│  │     └─ memory._project_hash()
│  ├─ skills.build_skill_descriptions()
│  │  └─ skills.discover_skills()
│  │     ├─ skills._load_skills_from_dir()
│  │     └─ skills._parse_skill_file()
│  │        └─ frontmatter.parse_frontmatter()
│  ├─ subagent.build_agent_descriptions()
│  │  └─ subagent.get_available_agent_types()
│  │     └─ subagent._discover_custom_agents()
│  │        └─ subagent._load_agents_from_dir()
│  │           └─ frontmatter.parse_frontmatter()
│  └─ tools.get_deferred_tool_names()
├─ Agent._generate_plan_file_path()                      [plan 模式]
├─ Agent._build_plan_mode_prompt()                       [plan 模式]
└─ 创建 anthropic.AsyncAnthropic 或 openai.AsyncOpenAI
```

`build_system_prompt()` 将当前目录、日期、平台、Shell、Git 状态、`CLAUDE.md`、`.bear/rules`、记忆索引、Skill 描述、子 Agent 描述和延迟工具名称组装为最终系统提示词。

### 2.3 恢复会话（条件）

传入 `--resume` 时，`main()` 额外调用：

```text
session.get_latest_session_id()
└─ session.list_sessions()
   └─ session._ensure_dir()

session.load_session()
Agent.restore_session()
└─ Agent._normalize_anthropic_messages()                 [Anthropic 历史]
   ├─ Agent._anthropic_tool_use_ids()
   └─ Agent._anthropic_tool_result_ids()
```

## 3. 一条普通输入进入 Agent

一次性模式由 `run_one_shot()` 调用 `Agent.chat()`；REPL 则直接调用 `Agent.chat()`。

```text
Agent.chat(user_message)
├─ McpManager.load_and_connect()                         [主 Agent 第一次 chat]
├─ McpManager.get_tool_definitions()                     [MCP 初始化后]
├─ agent._safe_utf8_text()
├─ Agent._pop_pending_skill_extraction_window()
├─ Agent._augment_user_message_with_skill_context()
│  └─ skills.format_retrieved_skill_context()
│     └─ skills.retrieve_relevant_skills()
│        ├─ skills.discover_skills()
│        └─ skills._token_list()                          [查询和每个 Skill]
├─ Agent._chat_anthropic() 或 Agent._chat_openai()
├─ Agent._schedule_background_skill_task()               [非 plan，且满足条件]
├─ Agent._set_pending_skill_extraction_window()
│  ├─ Agent._recent_dialog_messages()
│  │  ├─ Agent._message_text()
│  │  └─ Agent._strip_runtime_injections()
│  └─ Agent._compact_retrieved_reference()
├─ ui.print_divider()
└─ Agent._auto_save()
   ├─ Agent._get_message_count()
   └─ session.save_session()
      └─ session._ensure_dir()
```

### 3.1 第一次聊天时加载 MCP

`Agent.chat()` 只在主 Agent 的第一次聊天中调用 `McpManager.load_and_connect()`：

```text
McpManager.load_and_connect()
├─ McpManager._load_configs()
│  └─ McpManager._merge_config_file()                    [每个候选配置文件]
└─ 对每个 MCP Server
   ├─ McpConnection.__init__()
   ├─ McpConnection.connect()
   │  └─ McpConnection._read_loop()                      [后台任务]
   ├─ McpConnection.initialize()
   │  ├─ McpConnection._send_request("initialize")
   │  └─ McpConnection._send_notification(...)
   ├─ McpConnection.list_tools()
   │  └─ McpConnection._send_request("tools/list")
   └─ McpConnection.close()                              [连接失败]
```

单个 MCP Server 失败只打印错误，不会阻止主模型继续运行。成功发现的工具由 `get_tool_definitions()` 加上 `mcp__<server>__<tool>` 前缀，再追加到 Agent 工具列表。

### 3.2 Skill 自动检索

`_augment_user_message_with_skill_context()` 调用 `format_retrieved_skill_context()`，后者根据用户文本检索相关 Skill。命中时，Skill 内容以 `<retrieved_skills>` 运行时片段追加到用户输入中；原始输入仍单独保留，供后续使用统计和在线演化使用。

## 4. Anthropic-compatible 文本输出链路

当前配置的 API URL 包含 `/anthropic` 时走这条链路。

### 4.1 每轮模型调用

```text
Agent._chat_anthropic(user_message)
├─ Agent._normalize_anthropic_messages()
├─ agent._sanitize_for_utf8()
├─ agent._safe_utf8_text()
├─ Agent._build_side_query()                             [主 Agent]
├─ memory.start_memory_prefetch()                        [主 Agent]
│  ├─ memory.get_memory_dir()
│  │  └─ memory._project_hash()
│  └─ memory.select_relevant_memories()                  [异步任务]
│     ├─ memory.scan_memory_headers()
│     │  ├─ memory.get_memory_dir()
│     │  │  └─ memory._project_hash()
│     │  └─ frontmatter.parse_frontmatter()
│     ├─ memory.format_memory_manifest()
│     ├─ side query 闭包（由 Agent._build_side_query() 创建）
│     ├─ memory.memory_freshness_warning()
│     └─ memory.memory_age()                              [没有过期警告]
├─ while True
│  ├─ Agent._run_compression_pipeline()
│  │  ├─ Agent._budget_tool_results_anthropic()
│  │  ├─ Agent._snip_stale_results_anthropic()
│  │  │  └─ Agent._find_tool_use_by_id()
│  │  └─ Agent._microcompact_anthropic()
│  ├─ memory.format_memories_for_injection()             [预取完成且有命中]
│  ├─ ui.start_spinner()                                 [主 Agent]
│  ├─ Agent._call_anthropic_stream()
│  ├─ ui.stop_spinner()                                  [主 Agent]
│  ├─ Agent._block_to_dict()                             [每个响应 block]
│  ├─ 无 tool_use：ui.print_cost() → break
│  └─ 有 tool_use：进入第 6 节的工具循环，然后继续 while
└─ 返回 Agent.chat()
```

`_run_compression_pipeline()` 每轮请求前整理旧工具结果。只有上下文达到阈值时才真正裁剪内容。

### 4.2 SDK 流与终端文本

```text
Agent._call_anthropic_stream()
└─ 内部异步函数 _do()
   ├─ agent._get_max_output_tokens()
   ├─ tools.get_active_tool_definitions()
   ├─ agent._sanitize_for_utf8()
   ├─ anthropic.AsyncMessages.stream()
   └─ async for event
      ├─ ui.stop_spinner()                               [第一个文本增量]
      └─ Agent._emit_text(delta.text)                    [每个文本增量]
         ├─ agent._safe_utf8_text()
         └─ ui.print_assistant_text()                    [普通 CLI/REPL]
            └─ ui._safe_stdout_write()
               └─ ui._safe_text()
```

`_call_anthropic_stream()` 本身由 `agent._with_retry(_do)` 包裹。请求异常时：

```text
agent._with_retry()
├─ agent._is_retryable()
└─ ui.print_retry()                                     [可重试错误]
```

因此，模型文本真正出现在终端的最短调用链是：

```text
Anthropic SSE event
→ Agent._call_anthropic_stream()
→ Agent._emit_text()
→ ui.print_assistant_text()
→ ui._safe_stdout_write()
→ sys.stdout.write()
```

## 5. OpenAI-compatible 文本输出链路

非 `/anthropic` 的非空 API Base URL 走 OpenAI 分支。

### 5.1 每轮模型调用

```text
Agent._chat_openai(user_message)
├─ agent._safe_utf8_text()
├─ Agent._build_side_query()                             [主 Agent]
├─ memory.start_memory_prefetch()                        [主 Agent]
│  ├─ memory.get_memory_dir()
│  │  └─ memory._project_hash()
│  └─ memory.select_relevant_memories()                  [异步任务；子调用同 4.1]
├─ while True
│  ├─ Agent._run_compression_pipeline()
│  │  ├─ Agent._budget_tool_results_openai()
│  │  ├─ Agent._snip_stale_results_openai()
│  │  └─ Agent._microcompact_openai()
│  ├─ memory.format_memories_for_injection()             [预取完成且有命中]
│  ├─ ui.start_spinner()                                 [主 Agent]
│  ├─ Agent._call_openai_stream()
│  ├─ ui.stop_spinner()                                  [主 Agent]
│  ├─ 无 tool_calls：ui.print_cost() → break
│  └─ 有 tool_calls：进入第 6 节的工具循环，然后继续 while
└─ 返回 Agent.chat()
```

### 5.2 SDK 流与终端文本

```text
Agent._call_openai_stream()
└─ 内部异步函数 _do()
   ├─ tools.get_active_tool_definitions()
   ├─ agent._to_openai_tools()
   ├─ agent._sanitize_for_utf8()
   ├─ openai.AsyncChatCompletions.create(stream=True)
   └─ async for chunk
      ├─ ui.stop_spinner()                               [第一个文本增量]
      └─ Agent._emit_text(delta.content)                 [每个文本增量]
         ├─ agent._safe_utf8_text()
         └─ ui.print_assistant_text()
            └─ ui._safe_stdout_write()
               └─ ui._safe_text()
```

`_call_openai_stream()` 同样由 `_with_retry(_do)` 包裹。模型分片中的工具参数会按 `tool_call.index` 拼接，流结束后重组为完整 `tool_calls`。

## 6. 模型调用工具后的完整回环

模型第一次回复不一定包含最终文本。例如它可能先要求读取 `requirements.txt`：

```text
用户输入
→ 模型返回 read_file tool call
→ Bear Code 读取文件
→ 把文件内容作为 tool result 发回模型
→ 模型基于文件内容返回最终文本
→ 终端流式输出
```

### 6.1 共用工具分派

Anthropic 和 OpenAI 两条循环都会对每个工具调用执行：

```text
ui.print_tool_call()
├─ ui._get_tool_icon()
├─ ui._get_tool_summary()
└─ ui._safe_text()

tools.check_permission()
├─ tools._check_permission_rules()
│  ├─ tools.load_permission_rules()
│  │  ├─ tools._load_settings()
│  │  └─ tools._parse_rule()
│  └─ tools._matches_rule()
├─ tools.is_dangerous()                                 [Shell]
└─ tools._resolve_tool_path()                            [文件写入检查]

Agent._confirm_dangerous()                              [需要确认]
├─ ui.print_confirmation()
└─ REPL 注入的 confirm_fn() 或 input()

Agent._execute_tool_call()
├─ Agent._execute_plan_mode_tool()                       [计划模式工具]
├─ Agent._execute_agent_tool()                           [子 Agent 工具]
├─ Agent._execute_skill_tool()                           [Skill 工具]
├─ McpManager.is_mcp_tool()
│  └─ McpManager.call_tool()                             [MCP 工具]
│     └─ McpConnection.call_tool()
│        └─ McpConnection._send_request("tools/call")
└─ tools.execute_tool()                                  [内置工具]

Agent._persist_large_result()
ui.print_tool_result()
└─ ui._print_file_change_result()                        [文件修改结果]
```

权限拒绝或用户拒绝时不会调用实际工具，但仍会构造一个失败的工具结果返回模型，使消息协议保持完整。

### 6.2 内置工具内部调用

`tools.execute_tool()` 根据工具名继续分派：

| 工具 | 后续项目函数调用 |
| --- | --- |
| `read_file` | `_read_file()` → `_resolve_tool_path()` → `_truncate_result()` |
| `write_file` | `_resolve_tool_path()` 做读后写校验 → `_write_file()` → `_resolve_tool_path()` → `_auto_update_memory_index()`（记忆文件）→ `_truncate_result()` |
| `edit_file` | `_resolve_tool_path()` 做读后写校验 → `_edit_file()` → `_resolve_tool_path()` → `_find_actual_string()` → `_normalize_quotes()`（直接匹配失败时）→ `_generate_diff()` → `_truncate_result()` |
| `list_files` | `_list_files()` → `_resolve_tool_path()` → `_truncate_result()` |
| `grep_search` | `_grep_search()` → `_resolve_tool_path()` → `_grep_python()`（系统 grep 不可用时）→ 其内部 `walk()` → `_truncate_result()` |
| `run_shell` | `_run_shell()` → `_truncate_result()` |
| `tool_search` | 激活命中的延迟工具 → `_truncate_result()` 不参与该分支 |
| `skill_create` | `skills.create_skill()` → `_truncate_result()`；成功后 `Agent._refresh_runtime_system_prompt()` |
| `skill_evolve` | `skills.evolve_skill()` → `_truncate_result()`；成功后 `Agent._refresh_runtime_system_prompt()` |

`read_file_state` 保存最近一次成功读取文件时的修改时间。写入已有文件之前，`execute_tool()` 要求该文件已经读取且未被外部修改。

### 6.3 Anthropic 工具结果回传

```text
Agent._chat_anthropic()
├─ Agent._block_to_dict()                                [保存 assistant/tool_use]
├─ tools.check_permission()
├─ Agent._execute_tool_call()
├─ Agent._persist_large_result()
├─ ui.print_tool_result()
├─ 将结果写成 user/tool_result，使用 tool_use_id 关联
├─ Agent._check_and_compact()
│  └─ Agent._compact_conversation()                      [超过 85% 窗口]
│     ├─ Agent._compact_anthropic()
│     └─ ui.print_info()
└─ 回到 while 顶部，再次调用模型
```

`read_file`、`list_files`、`grep_search` 属于 `CONCURRENCY_SAFE_TOOLS`。Anthropic 流式返回完整工具 block 时会先调用 `check_permission()`；若直接允许，便提前创建 `_execute_tool_call()` 异步任务，等完整模型响应结束后再收集结果。

### 6.4 OpenAI 工具结果回传

```text
Agent._chat_openai()
├─ tools.check_permission()
├─ Agent._execute_tool_call()
├─ Agent._persist_large_result()
├─ ui.print_tool_result()
├─ 将结果写成 role=tool，并使用 tool_call_id 关联
├─ Agent._check_and_compact()
│  └─ Agent._compact_conversation()                      [超过 85% 窗口]
│     ├─ Agent._compact_openai()
│     └─ ui.print_info()
└─ 回到 while 顶部，再次调用模型
```

每次发现工具调用后，两个后端都会增加 `current_turns`，然后调用 `Agent._check_budget()`。该函数通过 `_get_current_cost_usd()` 检查 `--max-cost`，并检查 `--max-turns`。

## 7. 最终文本输出后的收尾

当模型响应不再包含工具调用时，后端循环调用 `ui.print_cost()` 并返回 `Agent.chat()`。随后：

```text
Agent.chat()
├─ Agent._schedule_background_skill_task()
│  ├─ Agent._run_skill_usage_tracking()                  [检索过 Skill]
│  │  ├─ Agent._online_evolution_enabled()
│  │  ├─ Agent._build_side_query()
│  │  ├─ online_skill_evolution.judge_retrieved_skill_usage()
│  │  └─ skills.record_usage_judgments()
│  └─ Agent._run_online_skill_evolution()                [存在上一轮反馈窗口]
│     ├─ Agent._online_evolution_enabled()
│     ├─ Agent._build_side_query()
│     ├─ online_skill_evolution.online_ingest()
│     └─ Agent._refresh_runtime_system_prompt()           [Skill 有变化]
├─ Agent._set_pending_skill_extraction_window()
├─ ui.print_divider()
└─ Agent._auto_save()
   └─ session.save_session()
```

一次性模式还会调用 `Agent.drain_background_skill_tasks()`，等待本轮创建的后台 Skill 任务结束，然后进程退出。REPL 则回到 `while True`，重新调用 `ui.print_user_prompt()` 等待下一条输入；退出整个 REPL 前也会调用 `drain_background_skill_tasks()`。

## 8. 最短成功路径

如果忽略初始化细节、没有 MCP、没有 Skill 命中、没有记忆命中、模型不调用工具，最短项目调用链如下。

### 一次性命令

```text
main.main()
→ main.parse_args()
→ main._load_env_file()
→ main._resolve_permission_mode()
→ main._resolve_api_config()
→ Agent.__init__()
→ main.run_one_shot()
→ Agent.chat()
→ Agent._chat_anthropic() / Agent._chat_openai()
→ Agent._call_anthropic_stream() / Agent._call_openai_stream()
→ Agent._emit_text()
→ ui.print_assistant_text()
→ ui._safe_stdout_write()
→ Agent._auto_save()
→ session.save_session()
→ Agent.drain_background_skill_tasks()
```

### REPL

```text
main.main()
→ main.run_repl()
→ ui.print_welcome()
→ ui.print_user_prompt()
→ input()
→ Agent.chat()
→ 与一次性命令相同的模型及输出链路
→ ui.print_user_prompt()
→ 等待下一条输入
```

## 9. 相关源码入口

- `agents/main.py`：命令行参数、配置解析、一次性入口和 REPL。
- `agents/agent.py`：Agent 生命周期、双后端模型循环、工具回环、输出、预算和会话收尾。
- `agents/ui.py`：欢迎页、提示符、流式文本、工具信息和费用的终端渲染。
- `agents/tools.py`：内置工具 schema、权限判断和实际执行。
- `agents/prompt.py`：动态系统提示词组装。
- `agents/mcp_client.py`：MCP 配置、stdio JSON-RPC、工具发现和路由。
- `agents/skills.py`：Skill 发现、检索、调用和变更。
- `agents/memory.py`：长期记忆索引、检索预取和注入。
- `agents/session.py`：会话持久化和恢复。

```
第一轮：
用户提出任务
    ↓
Agent 回答
    ↓
暂存“用户问题 + Agent 回答”

第二轮：
用户给出反馈/修正
    ↓
将反馈补到上一轮窗口
    ↓
Agent 完成第二轮回答
    ↓
后台分析上一轮任务、回答和当前反馈
    ↓
决定是否创建或演化 Skill
```