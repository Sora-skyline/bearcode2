# BearCode2 Agent 代码对比与架构分析

## 1. 文档目的

本文以当前项目 BearCode2 为分析对象，以同级目录 ../bearcode 为对照，回答三个问题：

1. 两个项目在仓库结构、文档、数据和配置上有什么区别；
2. agents 目录中的 Agent Runtime 如何协作；
3. BearCode2 新增的结构化会话折叠解决了什么问题，又带来了哪些风险。

对比时间为 2026-08-10。对比时两个仓库都位于 main 分支：

| 项目 | HEAD | 最近提交日期 | 定位 |
| --- | --- | --- | --- |
| BearCode2 | 335b293 | 2026-08-08 | 加入结构化 Session Memory folding、评测数据和成体系文档 |
| bearcode | 531ae17 | 2026-08-07 | 基础 Agent Runtime，后续补充了较完整的源码注释 |

两个目录是独立 Git 工作区，因此本文采用文件、AST 和运行链路对比，而不是把它们当成同一仓库的两个提交。

## 2. 结论摘要

BearCode2 的主要代码演进不是重写 Agent，而是在 bearcode 的 Agentic Harness 上新增一条“主动、自动、可审计”的结构化上下文折叠链路。

核心变化可以概括为：

- 新增 agents/session_memory.py，将 OpenAI 和 Anthropic 消息历史转录为统一文本，再折叠为 episode、working、tool 三类 Session Memory；
- 新增 compact_context 工具，模型可以根据上下文占用、连续工具失败和重复调用主动压缩；
- 自动压缩阈值从有效上下文窗口的 85% 提前到 70%；
- 每次折叠除替换当前消息历史外，还写入项目级 .bear/sessions 审计文件，并随完整 Session 一起保存；
- README、Wiki、论文和评测数据显著扩充，项目定位从“最小 Coding Agent”转为“自进化 Harness Agent”；
- 除上述折叠链路外，大部分同名 agents 模块的可执行 AST 与 bearcode 相同，原始差异主要是注释版本不同。

因此，BearCode2 更像一个围绕长程工具任务与自进化评测构建的研究/展示分支，而不是另一套完全不同的 Agent 框架。

## 3. 项目级差异

### 3.1 顶层目录与材料

BearCode2 独有的主要内容：

| 路径 | 规模或内容 | 作用 |
| --- | --- | --- |
| data/ | 约 74 MB | ALFWorld、API-Bank、GAIA、HLE、RestBench、ToolBench、ToolHop、WebShop 数据 |
| paper/ | 约 2.7 MB | AutoSkill、deepAgents 论文材料 |
| wiki/ | 多篇中文文档 | 架构、技术亮点、评测、简历包装和入门说明 |
| 在线Skills评测实现说明.md | 在线评测说明 | 解释 Skill replay、规则评测和候选晋升 |
| .playwright-mcp/ | 本地运行目录 | Playwright MCP 相关运行位置 |

bearcode 独有的主要材料：

| 路径 | 作用 |
| --- | --- |
| PLAN.md | 早期开发计划 |
| RUNTIME_FLOW.md | 基础 Runtime 调用链 |
| eval.md | 早期评测说明 |
| plan-4f88642d.md | 一次 Plan Mode 产物 |
| AutoSkill.pdf | 论文文件位于根目录，而 BearCode2 将论文集中到 paper/ |

BearCode2 的 data 目录包含八类 benchmark，但 agents 源码没有直接导入这些数据集。当前形态更接近“评测资产与结果说明已纳入仓库，评测执行器尚未形成独立源码入口”。

### 3.2 README 定位变化

bearcode 的 README 强调本地 Coding Agent Runtime、权限安全和基础运行方式。

BearCode2 的 README 改名为 Bear Agent，并强化以下叙事：

- Harness 与模型推理解耦；
- Memory、Skills、在线 Skill 自进化；
- MCP、子 Agent、Plan Mode；
- 评测体系和项目展示；
- 从用户反馈抽取并维护可复用 Skill。

这说明 BearCode2 的目标从“解释一个 Coding Agent 怎样运行”扩展为“展示一个可进化、可评测、适合长程任务的 Agent Harness”。

### 3.3 配置差异

| 文件 | BearCode2 | bearcode | 判断 |
| --- | --- | --- | --- |
| requirements.txt | 相同 | 相同 | 依赖层没有变化 |
| Dockerfile | 相同 | 相同 | 容器运行方式没有变化 |
| .mcp.json | 配置 Context7 与 Playwright MCP | 空文件 | BearCode2 默认外部工具能力更强，但依赖 npx 和网络 |
| .env.example | 使用占位 Key 和 MINI_CLAUDE_MODEL | 含疑似真实 Key 的硬编码值，并使用 MODEL | BearCode2 模板更安全；基线中的 Key 若真实有效应立即撤销 |
| .gitignore | 未忽略 Zone.Identifier | 增加该规则 | Windows/WSL 跨文件系统场景下 bearcode 更干净 |

MCP 配置中的 latest 或未固定版本依赖会降低可复现性，生产环境建议固定包版本并校验来源。

### 3.4 运行产物差异

BearCode2 的 .bear/skill-evolution 下存在更多在线评测、usage、provenance 和 champion 相关产物，.bear/sessions 也保存了结构化折叠记录。

这些内容反映 BearCode2 已实际运行过在线演化和折叠流程，但它们属于运行状态，不应与源码能力本身混为一谈。若需要干净复现实验，应把源码、固定输入数据、配置和运行产物分开管理。

## 4. agents 模块地图

agents 目录当前共有 15 个 Python 模块，补充注释后约 8,500 行。

| 模块 | 核心职责 | 主要调用方或下游 |
| --- | --- | --- |
| main.py | CLI 参数、环境变量、REPL、斜杠命令、Session 恢复 | 创建 Agent，调用 chat、compact、extract_now |
| agent.py | Agent 状态机、双协议循环、工具路由、权限、压缩、预算、后台任务 | 整个 Runtime 的中枢 |
| tools.py | 内置工具 schema、文件/Shell 执行、权限规则、deferred tool | agent.py |
| prompt.py | 动态 system prompt，合并 Git、规则、Memory、Skills、子 Agent | Agent 初始化和能力刷新 |
| memory.py | 长期事实记忆的索引、检索、预取和注入 | agent.py、prompt.py、tools.py |
| skills.py | Skill 发现、解析、检索、展开和维护入口 | main.py、agent.py、prompt.py |
| online_skill_evolution.py | 从真实对话抽取候选，并做 add、merge、discard 决策 | agent.py |
| skill_evolution.py | Skill 落盘、版本快照、provenance、usage 和 stale pruning | skills.py、在线演化与评测 |
| online_skill_eval.py | replay 冻结、规则评测、候选试跑、champion 记录 | REPL 的 skill eval 命令 |
| mcp_client.py | stdio JSON-RPC MCP 连接、工具发现与调用 | agent.py |
| subagent.py | 内置/自定义子 Agent 配置发现与工具隔离 | agent.py、prompt.py |
| session.py | 完整会话快照与折叠记忆审计文件持久化 | agent.py、main.py |
| session_memory.py | 协议转录、结构化折叠解析、回退和注入格式 | agent.py |
| frontmatter.py | Memory、Skill、自定义 Agent 共用的轻量元数据解析 | memory.py、skills.py、subagent.py |
| ui.py | 终端输出、Spinner、工具摘要、审批界面 | main.py、agent.py |

其中 mcp_client.py、memory.py、subagent.py 在两个项目中逐字节相同。frontmatter.py、main.py、online_skill_eval.py、online_skill_evolution.py、skill_evolution.py、skills.py、ui.py 的原始可执行 AST 相同，区别主要是 bearcode 后来补充了中文注释。

真正具有功能差异的模块是：

- agents/agent.py；
- agents/tools.py；
- agents/prompt.py；
- agents/session.py；
- BearCode2 独有的 agents/session_memory.py。

## 5. Agent Runtime 主调用链

一次普通 REPL 对话的主链路如下：

    用户输入
      -> main.run_repl
      -> Agent.chat
      -> 首轮懒加载 MCP
      -> 拼接上一轮 Skill 反馈窗口
      -> 检索相关 Skills 并注入用户消息
      -> 选择 Anthropic 或 OpenAI Agent Loop
      -> 启动长期 Memory 异步预取
      -> 调用模型流式接口
      -> 收集文本与工具调用
      -> 权限检查
      -> 执行内置工具 / MCP / Skill / 子 Agent / Plan / compact_context
      -> 工具结果写回消息历史
      -> 重复调用模型，直到没有工具调用
      -> 保存 Session
      -> 后台记录 Skill 使用效果并执行在线演化

### 5.1 CLI 和协议选择

main.py 从 CLI、通用环境变量和厂商专用环境变量合并配置。若 API Base 路径包含 /anthropic，则使用 Anthropic-compatible 客户端；其他自定义地址默认使用 OpenAI-compatible 客户端。

Agent 内部维护两套消息：

- _anthropic_messages：assistant content 中保存 tool_use，下一条 user content 回传 tool_result；
- _openai_messages：assistant 保存 tool_calls，工具结果使用 role=tool 和 tool_call_id。

两条协议循环共享工具定义、权限语义、Session、Skill、Memory 和压缩策略。

### 5.2 动态 System Prompt

prompt.build_system_prompt 会拼接：

- 当前工作目录、日期、平台和 Shell；
- Git 分支、最近提交和工作区状态；
- 从当前目录向上收集的 CLAUDE.md；
- 当前项目 .bear/rules 下的规则；
- Memory manifest；
- Skill 描述；
- 可用子 Agent 描述；
- 尚未激活的 deferred tools。

BearCode2 还在 Agent._refresh_runtime_system_prompt 中追加 Runtime Fold Guidance，包括：

- 当前上下文利用率；
- 连续工具失败次数；
- 同名工具连续调用次数；
- 距离上次折叠的时间；
- 是否建议调用 compact_context。

这使 system prompt 从“能力快照”进一步变为“能力快照 + 运行状态提示”。

### 5.3 工具与权限

tools.py 将工具分为：

- 读取类：read_file、list_files、grep_search，以及 BearCode2 新增的 compact_context；
- 编辑类：write_file、edit_file、skill_create、skill_evolve；
- Shell：run_shell；
- Runtime 特殊工具：agent、skill、enter_plan_mode、exit_plan_mode、compact_context；
- MCP 工具；
- deferred tools。

权限检查的优先级是：

1. bypassPermissions 直接允许；
2. 用户级和项目级 deny/allow 规则；
3. 读取类默认允许；
4. Plan Mode 限制；
5. acceptEdits 自动允许编辑；
6. 危险 Shell、新文件和 Skill 变更进入确认；
7. 其余调用允许。

文件编辑还有“先读后写”和 mtime 一致性保护，避免模型基于过期内容覆盖用户改动。

## 6. BearCode2 的核心新增：结构化会话折叠

### 6.1 与 bearcode 旧压缩方式的区别

bearcode 在上下文达到 85% 时让模型生成一段自然语言摘要，然后用“摘要 + 确认回复 + 最后一条用户消息”替换历史。

BearCode2 的变化：

| 维度 | bearcode | BearCode2 |
| --- | --- | --- |
| 自动阈值 | 有效窗口 85% | 有效窗口 70% |
| 输出结构 | 单段自然语言摘要 | episode、working、tool 三段 JSON |
| 触发方式 | 自动和 REPL 手动 | 自动、REPL 手动、模型工具主动 |
| 协议统一 | 两个后端各写一套摘要代码 | 先分别转 transcript，再共享解析和格式 |
| 失败回退 | 固定空摘要文本 | 保留最多 6,000 字符的原始 transcript |
| 持久化 | 只保存在会话消息 | 会话内记录 + 项目 JSONL + latest JSON |
| 工具经验 | 未单列 | 保存工具参数、常见错误、返回模式和派生规则 |

### 6.2 结构化 Memory Schema

session_memory.py 要求折叠模型返回三类信息：

1. episode_memory
   - task_description：总任务和用户意图；
   - key_events：已经发生的关键步骤和结果；
   - current_progress：已完成和待完成内容。
2. working_memory
   - immediate_goal：当前子目标；
   - current_challenges：阻塞、风险和不确定性；
   - next_actions：下一步工具、规划或决策动作。
3. tool_memory
   - tools_used：工具名、关键参数、错误、返回模式和经验；
   - derived_rules：避免重复失败的工具使用规则。

这个结构比单段摘要更适合长程 Agent，因为“任务进度”“下一步动作”和“工具经验”不会挤在同一段自然语言中。

### 6.3 折叠执行链路

    触发 compact
      -> _compact_conversation(trigger)
      -> build_openai_transcript 或 build_anthropic_transcript
      -> 限制单块 12,000 字符、总转录 80,000 字符
      -> _build_side_query(max_tokens=6000)
      -> FOLD_SESSION_MEMORY_SYSTEM 约束模型只返回 JSON
      -> parse_folded_memory 收窄为固定顶层结构
      -> 失败时 fallback_folded_memory
      -> _record_folded_session_memory
      -> 替换原始协议消息
      -> _record_fold_event 重置失败与重复工具信号
      -> 刷新 system prompt

OpenAI 路径保留第一条 system 消息，再追加一条折叠后的 user 消息；Anthropic 路径只保留一条折叠后的 user 消息，因为 system prompt 是单独传给 API 的。

### 6.4 三种触发方式

1. 自动触发

   每轮模型工具循环结束后，如果 last_input_token_count 超过 effective_window 的 70%，执行 trigger=auto。

2. 用户手动触发

   REPL 输入 /compact，调用 Agent.compact；历史消息不足四条时不压缩。

3. 模型主动触发

   模型调用 compact_context，并可填写 reason。Agent 执行 trigger=tool 后设置 _context_cleared，当前协议循环停止继续使用已经被替换的 tool_use/tool_result 历史，下一轮从结构化记忆继续。

### 6.5 工具失败和重复调用信号

BearCode2 在两套协议循环中统一记录：

- 最近是否连续出现工具失败；
- 当前工具是否与上一工具同名；
- 同名工具连续重复次数；
- 最近一次折叠时间。

这些信号不直接强制折叠，而是注入 system prompt，让模型决定是否调用 compact_context。它属于“Harness 提供状态，策略由模型决策”的实现。

### 6.6 持久化布局

完整 Session 仍保存到：

    ~/.bear-code/sessions/{session_id}.json

BearCode2 在该 JSON 中新增 foldedSessionMemories 数组。

每次折叠还写入项目目录：

    .bear/sessions/{session_id}.folded-memory.jsonl
    .bear/sessions/{session_id}.folded-memory.latest.json

JSONL 保留整个折叠时间线，latest 文件方便人工查看最近一次状态。

## 7. Memory、Session Memory 与 Skill 的边界

这三类机制名字相近，但生命周期不同：

| 机制 | 保存内容 | 生命周期 | 触发方式 | 主要文件 |
| --- | --- | --- | --- | --- |
| Long-term Memory | 用户偏好、项目事实、长期约定 | 跨 Session | 检索和显式写入 | memory.py |
| Folded Session Memory | 当前任务进度、下一步、工具经验 | 当前 Session | 上下文折叠 | session_memory.py、session.py |
| Skill | 可复用的方法、流程、输出约束 | 跨任务、可版本演化 | 显式调用、检索、在线抽取 | skills.py、online_skill_evolution.py、skill_evolution.py |

Session Memory 不应自动升级为长期 Memory；工具经验只有在真实用户反馈支持其跨任务复用时，才适合进入 Skill。session_memory.py 的 system prompt 已明确限制不要保存与当前任务无关的长期偏好和项目事实。

## 8. Skill 自进化链路

BearCode2 与 bearcode 的这部分可执行逻辑基本一致。

### 8.1 在线抽取

第 N 轮结束后，Agent 保存任务、回答和检索到的 Skill 引用。第 N+1 轮用户输入到来时，该输入被视为对上一轮结果的反馈，形成完整抽取窗口。

online_skill_evolution.py 的 Extractor 最多抽取一个候选，只允许稳定、可复用的流程或纠正，不接受秘密、一次性参数、账号、URL、临时事实和仅由 Assistant 猜测出的规则。

### 8.2 维护决策

Maintainer 在 add、merge、discard 中选择：

- add：没有合适 Skill，创建新文件；
- merge：与现有 Skill 身份匹配，演化已有文件；
- discard：证据弱、过于具体、重复或价值不足。

### 8.3 持久化与审计

skill_evolution.py 负责：

- 创建或更新 SKILL.md；
- 更新版本号并保存历史快照；
- 写 usage.jsonl 和 online_provenance.jsonl；
- 维护 online_skill_provenance.json；
- 汇总 relevant、used、retrieved 统计；
- 对长期未命中的 Skill 做 stale pruning。

### 8.4 在线评测

online_skill_eval.py 从真实 provenance 冻结 replay 数据，编译启发式规则和可选 LLM judge，生成候选变体并试跑。champion 只记录当前 lineage 的最佳候选，不直接覆盖 active SKILL.md，这个边界能避免评测阶段未经审批修改生产 Skill。

## 9. 风险与待改进项

### 9.1 BearCode2 新增折叠链路的风险

高优先级：

1. compact_context 被归入 READ_TOOLS，但它会调用模型、改写消息历史并向项目 .bear/sessions 写文件。权限语义与实际副作用不一致，Plan Mode 中也会默认允许。
2. 缺少自动化测试。当前没有 test_*.py、pytest.ini 或 pyproject.toml，折叠后的协议合法性、恢复兼容性和中途 context clear 只能靠人工验证。
3. 自动阈值从 85% 提前到 70%，会增加一次额外模型调用的成本，也更早丢弃原始对话。需要用 token 成本、任务成功率和折叠次数共同评估。

中优先级：

4. _looks_like_tool_failure 依赖 error、denied、timeout 等字符串，可能把“no errors”误判为失败，也可能漏掉不含关键词的结构化错误。
5. parse_folded_memory 只校验顶层对象和列表类型，不校验 key_events、next_actions、tools_used 的内部字段，异常或过大的嵌套对象仍可进入 Session。
6. fallback 只保留最多 6,000 字符的转录。折叠模型失败时，最需要保真的场景反而可能丢失较多信息。
7. 折叠记录写入失败会被静默吞掉；Session JSON 和 latest JSON 也不是临时文件替换式的原子写入，进程中断可能留下损坏文件。
8. 每轮工具循环刷新动态 system prompt 会重复扫描 Git、规则、Memory、Skills 和子 Agent 配置。大型仓库中应测量这部分 I/O 开销。

### 9.2 两个项目共同继承的问题

以下问题在 bearcode 中也存在，不是 BearCode2 新增：

1. Thinking 模式调用了不存在的 _mode_supports_adaptive_thinking，而实际方法名是 _model_supports_adaptive_thinking。对支持 thinking 的模型启用该开关会在 Agent 初始化时触发 AttributeError。
2. Plan 审批函数在 main.py 中定义为 async，但 _execute_plan_mode_tool 调用后没有 await，随后直接使用 result.get，会把 coroutine 当字典使用。
3. Plan 审批通过的分支计算了 target_mode，却没有把它赋给 self.permission_mode；代码先写入 _pre_plan_mode 随即清空，运行时可能仍停留在 plan。
4. 子 Agent 构造时没有透传 api_key 和 Anthropic base URL。主 Agent 仅通过通用 APIKEY 配置时，子 Agent 可能无法复用相同连接。
5. 多处异常使用宽泛 except 并静默忽略，提升了交互容错性，但降低了可诊断性。

建议先为这些既有状态机问题补单元测试，再继续扩展折叠策略。

## 10. 推荐测试矩阵

### 10.1 Session Memory 单元测试

- OpenAI transcript 能保留 tool_calls、arguments 和 tool_call_id；
- Anthropic transcript 能保留 tool_use 和 tool_result 对应关系；
- 纯 JSON、Markdown JSON 代码块、前后带文本的输出都能解析；
- 非对象、缺字段、字段类型错误进入可预期回退；
- 超过块级和总级字符上限时保留头尾且标注裁剪；
- format 后的标签边界稳定，不把工具结果当系统指令。

### 10.2 Agent 压缩集成测试

- 少于四条消息时 manual/tool compact 不改写上下文；
- OpenAI 折叠后只保留 system + folded user；
- Anthropic 折叠后保留单条 folded user；
- 自动阈值在 70% 前后行为正确；
- compact_context 位于多工具调用中间时，剩余工具不会基于旧历史继续执行；
- 折叠记录可以随 Session 保存和恢复；
- side query 失败时主对话仍能从 fallback 继续。

### 10.3 权限和状态机测试

- default、plan、acceptEdits、dontAsk、bypassPermissions 的工具矩阵；
- compact_context 是否应在 Plan Mode 中写审计文件；
- Plan 审批四个选择的权限模式切换；
- thinking 开关对支持和不支持的模型；
- OpenAI 和 Anthropic 子 Agent 的连接参数继承。

## 11. 本次注释整理

本次仅增加文档字符串和行内说明，没有修改 agents 目录的可执行 AST。

处理原则：

- 对两个项目逻辑相同的模块，复用 bearcode 中与实现严格对应的中文注释；
- 对 BearCode2 新增的 session_memory.py、结构化折叠状态和 compact_context 链路单独补充说明；
- 所有 15 个 Python 模块现在都有模块级文档；
- 复杂公共入口和关键私有流水线增加职责、输入输出和边界说明；
- 不给显而易见的赋值逐行复述，重点解释协议约束、状态变化、权限和持久化副作用。

验证结果：

- agents 下所有文件通过 Python AST 解析；
- 与本次修改前的 Git HEAD 对比，去除 docstring 后的可执行 AST 全部不变；
- git diff --check 通过；
- compileall 使用项目外临时字节码缓存目录执行通过。

## 12. 最终判断

BearCode2 最值得保留的差异是结构化 Session Memory folding。它把传统的“上下文快满时写一段摘要”升级为：

- 可由模型根据运行信号主动触发；
- 面向任务进度、工作状态和工具经验的结构化记忆；
- 双协议统一；
- 可恢复、可审计；
- 能与长程 benchmark 和消融评测形成对应关系。

目前它仍处于“功能实现和说明材料较完整，但工程验证不足”的阶段。下一步最高收益不是继续增加折叠字段，而是补齐协议、权限、Plan 状态机和恢复链路的自动化测试，并修复共同继承的 thinking 与 Plan 审批问题。
