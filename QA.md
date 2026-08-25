# BearCode Harness Agent 面试 QA

本文整理 BearCode 项目在实习、秋招提前批等面试中出现过的高频问题，覆盖 Harness、Agent Loop、Skills 自进化、长期记忆、工具安全、子 Agent、上下文折叠和效果评测。

回答时建议先给结论，再解释设计原因和实现细节。不要只罗列功能，要说明：解决了什么问题、为什么这样设计、当前边界是什么。

## 一、Harness 与 Agent Loop

### 1. 项目里哪些地方体现了 Harness Agent 的设计？

主要体现在三个层面。

第一，模型只负责推理和提出工具调用意图，不直接操作环境。真正的权限检查、工具执行和异常处理都由 Harness 完成。

第二，工具结果会按照模型协议封装成 `tool_result` 或 `role=tool` 消息，再回写给模型。模型基于真实环境反馈继续推理，而不是靠一次调用猜测结果。

第三，Harness 还负责模型协议适配、会话保存、上下文折叠、Memory、Skills、MCP、子 Agent、成本限制和在线评测。因此 BearCode 的核心不是一个 Prompt，而是一套持续驱动 Agent 工作的 Runtime。

### 2. 为什么不是只调用一次模型？

Agent 任务通常包含多轮“推理—行动—观察”。例如模型先读取文件，看到真实代码后才能决定如何修改；修改后还要运行测试，再根据测试结果继续修复。

BearCode 的执行循环是：

```text
用户任务
  -> 模型推理
  -> 产生文本或 tool call
  -> Harness 检查权限并执行工具
  -> 将 tool result 回写模型
  -> 模型继续推理
  -> 不再调用工具时结束本轮对话
```

一次模型调用只能完成静态问答，Agent Loop 才能让模型根据环境反馈完成多步任务。

### 3. Skills 和 Memory 有什么区别？

Memory 保存“知道什么”，Skills 保存“怎么做”。

- “项目部署在某台机器上”属于事实，是 Memory。
- “部署前先备份，部署后检查健康状态，失败时执行回滚”属于可复用流程，是 Skill。

两者分开以后，事实不会污染方法库，流程也不会和某个项目的临时信息绑定。

## 二、Skills 自进化

### 4. Skills 自进化是怎么实现的？

BearCode 使用“延迟判断”的反馈窗口。

一轮对话结束后，系统先保存本轮用户请求、助手回答和命中的相关 Skills，不立即写入新 Skill。下一轮用户输入到来时，系统把它作为上一轮结果的反馈证据，形成 `U1 -> A1 -> U2` 的窗口。

随后分成两个阶段：

1. **Extractor**：从窗口中抽取最多一个稳定、可复用的 Skill 候选；如果只是新任务、临时参数、项目事实或弱反馈，就返回空结果。
2. **Maintainer**：将候选与已有 Skills 做相似检索，决定 `add`、`merge` 或 `discard`，并记录来源、决策和版本历史。

默认权限模式下，系统可以完成抽取和维护判断，但不会静默写文件；使用 `--accept-edits` 或 `--yolo` 时，通过写权限检查的候选可以自动写入项目或用户级 `SKILL.md`。

### 5. 怎么避免沉淀垃圾 Skill？

主要有四层控制。

1. **证据控制**：用户反馈是主要证据，助手自己的总结只能作为上下文。
2. **内容控制**：不沉淀密钥、账号、URL、精确日期、临时路径、一次性参数和项目事实。
3. **集合维护**：候选先和已有 Skills 比较，优先合并重复能力；非法或低价值决策默认丢弃。
4. **使用治理**：记录 `retrieved`、`relevant` 和 `used`。长期被检索但没有实际使用的 Skill 可以进入归档流程。

系统的目标不是尽可能多地产生 Skills，而是提高能力库的复用价值和信噪比。

### 6. 为什么使用下一轮用户反馈，而不是当前轮结束后立刻沉淀？

当前轮结束时，Agent 不知道用户是否认可结果。如果立刻沉淀，很容易把助手自己的猜测或当前任务的临时细节写成长期规则。

下一轮用户反馈提供了更强的结果证据。例如“以后都按这个格式”“刚才的步骤太多，下次先给结论”明确表达了稳定偏好，更适合沉淀为 Skill。

这个设计本质上是在提高自动学习的精确率：少学一点，但尽量学对。

### 7. 项目最大的技术难点是什么？

最大的难点不是调用模型 API，而是把模型、工具、权限、上下文、长期记忆、自进化和评测串成一个稳定闭环。

具体有三个难点：

1. **执行安全**：模型可以提出文件和 Shell 操作，但副作用必须经过 Runtime 控制。
2. **长期状态管理**：Memory、Skills 和 Session Memory 的职责必须清晰，否则事实、方法和任务状态会互相污染。
3. **自进化治理**：自动学习不能只解决“能写入”，还要解决证据、去重、冲突、版本、审计和效果评测。

### 8. 如果继续优化，你会优先做什么？

我会优先做四件事。

1. 抽象更独立的模型适配层，统一 OpenAI-compatible 和 Anthropic-compatible 的消息、流式输出及工具协议。
2. 为 Skills 检索增加向量召回和 rerank，形成 BM25-lite 与语义检索结合的 hybrid search。
3. 将在线评测和 active Skill 升级真正串起来，形成“候选版本—回放评测—人工审批—正式晋级”的流程。
4. 增加权限模式、Plan Mode、工具异常、MCP 路由、Skill 新建/合并/丢弃以及评测门控的自动化测试。

### 9. 这个项目最能体现你能力的地方是什么？

最能体现的是系统设计和工程边界。

我没有把 Agent 做成一次模型调用，也没有让模型直接修改自己的 Prompt，而是实现了一套 Runtime：模型提出意图，Harness 控制执行；任务过长时进行结构化状态折叠；用户反馈经过抽取、维护、版本和评测后再沉淀为 Skills。

这说明我不仅能调用模型，还能围绕模型的不确定性设计协议、状态机、安全边界和可治理的反馈闭环。

### 10. Feedback window 是每轮都会产生吗？

主 Agent 每完成一轮有效对话，都会生成一个候选 pending window；子 Agent、中断轮次以及用户输入或助手回复为空的情况不会生成新窗口。

它的时序是：

```text
U1 -> A1
      保存 pending window

U2 到来
  -> 取出 U1/A1
  -> 追加 U2 作为反馈证据
  -> 执行一次抽取判断

U2 -> A2
      保存新的 pending window，等待 U3
```

当前只有一个滚动窗口。保存时提取最近 8 条有效 `user/assistant` 消息，下一轮再追加一条用户反馈。窗口每轮都会被判断，但只有明确、稳定、可复用的反馈才可能产生 Skill。

该机制更擅长捕捉紧邻下一轮的反馈。如果用户隔了很多轮才评价早期回答，后续可以通过多窗口队列和反馈归因模型增强。

### 11. 每轮都判断是否自进化，Token 成本不会很高吗？

这个问题要分原型阶段和生产阶段回答。

当前实现面向本地原型验证，优先验证“用户反馈能否稳定转化为 Skill”。已有控制包括：只在主 Agent 执行、Plan Mode 关闭在线演化、限制窗口长度、每次最多抽取一个候选、后台异步运行，并支持通过环境变量关闭。

如果上线，我会把轮次触发改成事件触发：

1. 先用规则或小模型识别“以后、不要、应该、下次、这类任务”等反馈信号。
2. 普通新任务、闲聊和一次性参数直接跳过。
3. 对候选进行采样、批处理和频率限制。
4. 前置分类使用低成本模型，只有高置信反馈才调用强模型完成抽取和合并判断。

后台执行只能降低主对话延迟，不能降低 Token 消耗；真正的成本优化必须依靠前置 gating、分级模型和批处理。

## 三、工具调用、权限与框架选择

### 12. Tool calling 失败时，Harness 怎么处理？

我把失败处理分成执行前约束和执行后恢复。

执行前，模型只提交工具调用意图。Harness 会解析参数、检查权限模式和项目规则，再决定允许、拒绝还是请求确认。文件工具在覆盖已有文件前要求先读取，并通过 `mtime` 检查文件是否被外部修改。

执行后，权限拒绝、命令超时、参数缺失和工具异常都会被包装成 tool result 回写模型。模型可以修正参数、切换工具、请求用户补充信息或停止执行，而不是让整个循环因为一次工具失败直接退出。

Runtime 还会记录连续工具错误和同名工具重复次数，并把这些状态注入下一轮提示。失败持续累积时，模型可以调用 `compact_context`，把失败路径整理进 tool memory，避免反复走同一条错误路线。

### 13. 项目有没有使用 LangGraph、Spring AI 等框架？为什么？

没有。这个项目的目标就是实现 Agent Harness 本身，所以我直接实现了模型循环、工具协议、权限层、会话状态、上下文折叠、Memory、Skills、MCP 和子 Agent 调度。

LangGraph 的优势是用图和状态节点快速编排复杂工作流，Spring AI 的优势是和 Java/Spring 生态集成。但如果直接使用这些框架，模型循环、消息协议和状态迁移会被框架封装，我很难深入展示 Harness 的底层设计。

不使用框架的收益是控制粒度更细、可以理解并修改每个运行环节；代价是需要自己处理协议兼容、异常恢复、状态持久化和测试。这正是这个项目想验证的工程能力。

### 14. 不依赖现成框架，怎么验证 Agent 效果？

是否使用框架和 Agent 效果没有直接关系。效果应该通过任务完成率、基线对比和消融实验验证。

BearCode 当前使用 GAIA 和 HLE 共 665 道任务，以 `Pass@1` 作为主指标：每道题只运行一条 Agent 轨迹，不通过多次采样挑选最好答案。

| 数据集 | HiRA 基线 | BearCode | 提升 |
| --- | ---: | ---: | ---: |
| GAIA | 42.1% | 53.3% | +11.2 个百分点 |
| HLE | 13.6% | 20.2% | +6.6 个百分点 |

项目还做了会话折叠消融。在相同 GAIA 任务集上，开启结构化折叠时 `Pass@1` 为 53.3%，关闭后为 44.7%，下降 8.6 个百分点。这说明折叠的价值不仅是减少 Token，还包括保留任务状态、工具经验和恢复路径。

此外，Skills 自进化使用另一套在线评测：从 provenance 构造 replay pool，通过程序规则、LLM judge、candidate variants 和 usage gate 评价 Skill。这两类评测分别验证“整个 Agent 能否完成任务”和“沉淀的 Skill 是否有效”。

### 15. 为什么使用 Markdown 存储长期记忆？

因为当前长期记忆主要是用户偏好、项目事实、历史决策和参考资料，这类信息天然适合文本存储。

Markdown 有四个优势：

1. **人可读**：用户可以直接检查、修改和删除错误记忆。
2. **易审计**：适合 Git diff、备份和按项目隔离。
3. **模型友好**：正文可以直接注入上下文，frontmatter 可以保存 `name`、`description`、`type` 等元数据。
4. **实现轻量**：不需要先引入数据库和向量服务，适合本地 Agent 原型。

它的边界也很明确：当记忆规模变大、需要多人并发、复杂权限或高性能检索时，应增加 SQLite/Postgres 和向量索引，而不是继续只靠文件扫描。

可参考 [Claude Code Memory 文档](https://code.claude.com/docs/en/memory)。

## 四、长期记忆与检索

### 16. 如果不用 Markdown，你会怎么存储长期记忆？

我会将长期记忆设计成一个 Memory Service，而不是简单把完整聊天记录放进数据库。

它包含四层：

1. **原始事件层**：保存用户输入、助手回复、工具调用、反馈和时间戳，用于审计与回放。
2. **结构化记忆层**：抽取用户偏好、项目事实、历史决策和工作流经验，并记录 `type`、`scope`、`source`、`confidence`、`expire_at` 和 `provenance`。
3. **索引检索层**：使用 SQLite/Postgres 做结构化过滤和全文检索，向量索引负责语义召回，必要时增加 rerank。
4. **生命周期层**：负责去重、冲突合并、过期降权、删除和租户权限隔离。

本地优先方案可以使用“SQLite + 向量索引 + 原始事件归档”，兼顾可读性、检索效率和实现复杂度。

扩展资料：[Mem0](https://docs.mem0.ai/introduction)、[TencentDB Agent Memory](https://github.com/TencentCloudADP/tencentdb-agent-memory)、[PolarDB Agent Memory](https://help.aliyun.com/zh/polardb/polardb-for-ai/agentmemory/)。

### 17. 向量化的目的是什么？

向量化主要解决语义召回问题。

用户当前问题和历史记忆可能表达不同，但语义相同。例如“接口文档少堆函数名”和“文档重点讲业务流程”字面不同，向量检索更容易找到它们的关联。

它还有两个作用：

- 从大量记忆中只召回少量相关内容，降低上下文成本。
- 帮助发现重复或冲突记忆，支持聚类、合并和更新。

向量不是权威存储。原文、权限和生命周期仍应保存在结构化存储中，向量只负责语义索引。

### 18. 向量检索会不会召回其他用户的画像？

不会把所有用户数据放在一个不受约束的全局向量空间里直接搜索。正确流程是先做身份和权限过滤，再做向量召回。

```text
确认当前 user / tenant / project
  -> 根据 user_id、tenant_id、project_id、visibility 做硬过滤
  -> 在合法候选集合内执行向量检索
  -> rerank
  -> 回表读取原文和权限字段
  -> 注入上下文
```

用户画像、权限和开关类偏好更适合存在结构化 profile 表里；非结构化历史偏好、项目经验和文档片段才适合增加向量索引。

因此不是“记忆应不应该向量化”，而是“向量不能作为唯一存储，也不能代替权限隔离”。

### 19. 使用下一轮反馈自进化，和 Session 结束后统一总结相比，哪个更好？

两者优化的目标不同。

- Session 总结的召回率更高，可以看到完整任务中的工具路径、失败过程和最终决策。
- 下一轮用户反馈的精确率更高，可以明确判断哪些内容是用户认可的、未来仍要复用的规则。

完整 Session 中包含大量当前任务 payload，例如临时路径、报错、参数和实现细节。直接从完整 Session 自动生成 Skill，容易污染长期能力库。因此 BearCode 把 pending feedback 作为 Skill 自动写入的主要证据，把 Session Memory 用于当前长任务的状态恢复。

更完整的方案是两者结合：Session summary 生成候选经验池，pending feedback 提供证据强度，最后由 Maintainer 决定 `add`、`merge` 或 `discard`。

### 20. 新进化内容和原 Skill 冲突时怎么处理？

新反馈不会直接以追加文本的方式覆盖旧 Skill，而是先进入 Maintainer。

Maintainer 会看到候选内容、相似 Skills、原有说明和触发条件，再做三类决策：

- 新能力：创建新 Skill。
- 同类能力：合并到已有 Skill，并生成完整的新版本。
- 重复、低价值或无法形成稳定规则：丢弃。

如果新旧规则适用于不同场景，会把适用条件拆清楚；如果无法判断优先级，就不强行覆盖。每次 merge 前都会保存版本快照，并记录反馈来源、维护决策和演化历史，出现问题时可以回溯。

当前在线评测会基于 replay、规则检查和 LLM judge 生成 candidate/champion 记录；后续可以进一步升级为“评测通过后再人工批准覆盖 active Skill”。

## 五、权限、子 Agent 与上下文折叠

### 21. Shell 操作怎么判断是否安全？如何防止覆写？

安全判断由 Harness 完成，不交给模型自己决定。

当前权限链路包含三层：

1. **权限规则**：用户级和项目级配置可以显式允许或拒绝某类工具及命令。
2. **权限模式**：默认模式需要确认高风险操作；`dontAsk` 自动拒绝；`acceptEdits` 自动允许文件编辑；`bypassPermissions` 跳过确认。
3. **危险模式识别**：对删除、Git 强制操作、提权、格式化磁盘、杀进程、关机重启等明显高风险 Shell 命令进行拦截或确认。

文件工具还有独立的一致性保护：覆盖已有文件前必须先读取，并记录读取时的 `mtime`；如果文件之后被外部修改，写入会被拒绝并要求重新读取。

当前 Shell 判断属于显式规则和危险模式检测，不是操作系统级沙箱。生产环境还应增加工作区路径隔离、容器沙箱、命令 AST 分析和统一审计。

### 22. 主 Agent 和子 Agent 共享信息吗？信息不足怎么办？

采用的是 fork-return 模式：上下文默认隔离，但通过任务输入和最终结果通信。

主 Agent 创建子 Agent 时，会传入明确的任务描述、必要背景和目标。子 Agent 拥有独立上下文，根据类型获得不同工具：`explore` 和 `plan` 只有只读工具，`general` 可以执行更完整的任务。完成后，它只把结论和 Token 统计返回主 Agent，不把整个探索过程塞回主上下文。

如果信息不足，子 Agent 可以先用自己的工具补充上下文；仍无法完成时，应明确返回缺少的文件、约束或决策点。主 Agent 再补充上下文重新派发，或者自己继续处理。

这样牺牲了完整历史共享，但减少了上下文污染，并使任务边界和工具权限更清晰。

### 23. 工具执行被阻塞时，对 Agent 有什么影响？

Agent Loop 必须等工具返回 observation 才能继续推理，因此工具阻塞会卡住当前执行链。

当前项目已经覆盖两个关键位置：Shell 默认 30 秒超时，超时结果会回写模型；MCP Server 初始化和工具发现默认 15 秒超时，单个 Server 失败时会被隔离，不影响其他 Server 和主 Agent 启动。

上下文层还会记录失败和重复调用信号，必要时通过结构化折叠清理失败路径。

当前普通 MCP `tools/call` 还没有统一的单次调用超时。后续应给所有工具增加 per-call timeout、取消传播、熔断和子进程清理，形成统一的 Tool Executor。

### 24. 上下文会话折叠解决了什么问题？

它主要解决三个问题：上下文持续增长、错误路径不断累积、关键执行状态容易丢失。

BearCode 将折叠结果组织成三类结构化记忆：

- `episode_memory`：任务中发生了什么、已经完成了什么。
- `working_memory`：当前目标、阻塞点和下一步计划。
- `tool_memory`：使用过哪些工具、哪些参数失败、后续应避免什么路径。

模型可以主动调用 `compact_context`；当输入 Token 接近有效窗口阈值时，Runtime 也会自动触发。折叠后，原始长历史会被结构化状态替换，Agent 从更干净的上下文继续执行。

所以它不是普通摘要功能，而是长程 Agent 的状态恢复机制。

### 25. 它和普通会话压缩有什么不同？为什么要结构化？

普通会话压缩主要追求“字更少”，结构化折叠追求“任务还能正确继续”。

自然语言摘要容易遗漏当前阻塞点、下一步动作和工具失败参数。结构化以后，各类状态进入固定字段，模型不需要从长段落里重新猜重点，Runtime 也可以解析、保存、展示和审计。

结构化折叠的核心价值包括：

1. 固定字段降低关键状态遗漏概率。
2. 明确区分历史进展、当前工作状态和工具经验。
3. 原始消息被移除后仍可恢复任务。
4. 将失败路径转化为经验，而不是继续保留大量错误日志。

### 26. 工具调用错误后，模型会自动触发记忆折叠吗？

不是每次错误都强制折叠，而是引导式触发。

Runtime 会检测工具结果中的 `error`、`denied`、`timeout` 等信号，同时统计连续工具错误和同名工具重复次数。下一次模型推理前，这些状态会注入 system prompt。

一次简单参数错误可以直接重试；当失败开始累积、上下文变脏或当前策略需要重启时，模型再调用 `compact_context`。同时，输入 Token 达到阈值后还有 Runtime 自动折叠作为兜底。

这种设计避免了“每次小错误都额外调用模型做折叠”的无效成本。

### 27. 模型输出 JSON 时 Schema 错了，会阻塞还是报错？

需要区分最终答案和工具参数。

如果用户要求最终答案是 JSON，它仍然属于普通文本。当前 Runtime 不会自动校验业务 Schema，因此不会阻塞，但用户可能收到格式错误的结果。需要在任务层增加 JSON Schema 校验和自动重试。

如果是 tool call 的 JSON 参数格式错误，Runtime 在解析失败后会降级为空参数，随后权限检查和工具执行通常会因为缺少必填参数返回错误，再将错误作为 tool result 交给模型修正。

当前机制能避免解析异常直接让整个进程崩溃，但更理想的实现是解析失败后不执行工具，直接返回明确的 `invalid_tool_arguments`，并保留原始错误信息供模型重试。

## 六、项目对比

### 28. BearCode Harness 和 Superpowers 相比有什么优势？

两者不在同一层，应该先说明定位差异。

[Superpowers](https://github.com/obra/superpowers) 是运行在现有 Coding Agent 之上的 Skills 和软件工程方法论，强调需求澄清、设计、计划、Git worktree、TDD、子 Agent 开发和代码审查。它的优势是工程流程成熟、生态适配广、开箱即用。

BearCode 是更底层的 Agent Runtime，自己控制模型调用、工具协议、权限判断、执行结果回写、会话状态、上下文折叠、Memory、Skills、MCP 和子 Agent 调度。

BearCode 的差异化主要有四点：

1. **Runtime 控制粒度更细**：不是只通过 Prompt 约束行为，而是在本地执行层控制工具和权限。
2. **长程状态管理**：支持结构化 Session Memory folding，在上下文变长或失败累积时重组任务状态。
3. **能力沉淀闭环**：可以从用户后续反馈中抽取 Skill，并完成相似检索、版本、来源追踪和在线评测。
4. **工具边界更通用**：除了编码工具，还可以通过 MCP 接入外部系统和领域工具。

一句话总结：Superpowers 更像成熟的上层软件工程方法论和 Skills 包；BearCode 更像可控制、可扩展、可审计并支持自进化的底层 Agent Harness。两者可以组合，而不是互相替代。

## 七、主流 Harness 工程与对比

> 本节资料核对日期：2026-08-24。Harness 项目迭代很快，面试前应再次检查官方文档、架构说明和 Changelog。

### 29. 刚开放的 Codex Harness 是什么？哪些部分真正开源？

Codex Harness 是 Codex 的 Agent 执行层，不只是命令行界面。它负责会话状态、流式事件、工具调用、沙箱与审批策略，以及跨轮次继续任务。其核心价值是把可复用的 Agent Loop 从具体 UI 中拆出来，让其他产品也能嵌入同一套执行能力。

根据 OpenAI 在 2026 年 8 月 19 日发布的[官方说明](https://learn.chatgpt.com/blog/codex-as-a-platform)，目前有三种主要集成层：

- `codex exec`：适合脚本、CI 和一次性后台任务。
- Codex SDK：适合在程序中启动、恢复和流式接收任务。
- Codex App Server：适合将 Agent 嵌入产品，管理 thread、turn、事件、审批和生命周期。

“Codex 已开源”也需要讲清边界。官方[开源组件清单](https://learn.chatgpt.com/docs/open-source)显示，Codex CLI、SDK 和 App Server 已开放源码；IDE 扩展和 Codex Cloud 没有开源，模型服务也不属于开源 Harness。

因此更准确的说法是：**Codex 开放了本地 Agent Harness 和集成接口，并非把整个 Codex 产品与云服务全部开源。**

### 30. BearCode 和 Codex Harness 有什么区别？

两者都实现了“模型提出工具调用，Runtime 执行并回写结果”的完整闭环，也都关注权限、上下文、Skills、MCP 和子 Agent，但工程目标不同。

Codex 的重点是生产级运行时与产品嵌入。它已经把 CLI、SDK 和 App Server 分层，并提供事件流、审批协议、沙箱和多种宿主集成方式。BearCode 当前更像一个可读、可改、用于验证 Harness 机制的 Python Runtime，重点研究结构化上下文折叠和用户反馈驱动的 Skills 自进化。

BearCode 相对 Codex 的差异化不是“功能更多”，而是下面这条可审计闭环：

```text
用户反馈
  -> Skill 候选抽取
  -> add / merge / discard
  -> provenance 与版本记录
  -> replay 和 usage gate 评测
  -> active Skill 升级
```

反过来，Codex 明显领先的部分是操作系统级安全边界、稳定的客户端协议、事件流、可嵌入 SDK、产品化体验和工程测试规模。BearCode 应优先学习 App Server 的 Runtime/UI 分离、统一事件模型和沙箱设计，而不是照搬 Codex 的界面。

### 31. Claude Code 是怎样的 Harness？它是完整开源的吗？

Claude Code 是成熟的 Coding Agent 产品。它的底层仍是标准 Agent Loop：读取项目、搜索代码、执行命令、修改文件、运行测试，再根据每次工具结果决定下一步。其[官方工作原理](https://code.claude.com/docs/en/how-claude-code-works)还展示了几项有代表性的 Harness 机制：

1. 会话以 JSONL 保存，支持恢复、分叉和回退。
2. 上下文接近上限时，先清理旧工具输出，再压缩会话。
3. Skills 按需加载，MCP 工具定义可以延迟发现，子 Agent 使用独立上下文。
4. 文件修改前创建 checkpoint，危险操作由权限模式和规则控制。
5. 通过 `CLAUDE.md`、Skills、Hooks、MCP 和自定义 Agent 扩展行为。

需要直接纠正一个常见说法：**Claude Code 的 GitHub 仓库公开，不等于核心 Runtime 完整开源。**截至本节核对日期，[官方仓库](https://github.com/anthropics/claude-code)主要公开插件、示例和配套文件，其[许可证](https://github.com/anthropics/claude-code/blob/main/LICENSE.md)仍是 Anthropic 保留所有权利并受商业条款约束。因此可以研究它公开的产品机制和扩展接口，但不能把它描述成 MIT、Apache 等意义上的完整开源 Harness。

### 32. BearCode 和 Claude Code 应该怎么比较？

Claude Code 的优势是产品成熟度：终端、IDE、桌面端、Web 和 CI 使用同一套 Agent 能力；checkpoint、权限、自动压缩、Skills、Hooks、MCP、子 Agent 和会话恢复已经形成统一体验。

BearCode 的优势是 Runtime 可完全检查和修改，而且把“反馈如何成为长期能力”做成显式流程。Claude Code 已有 auto memory 和 Skills，但 BearCode 更强调候选抽取、冲突合并、来源追踪、版本和在线评测，而不是把新经验直接追加到一个记忆文件。

面试时不能说 BearCode 整体优于 Claude Code。更准确的结论是：

- Claude Code 是成熟的 Coding Agent 产品基线。
- BearCode 是可控 Harness 与 Skills 自进化机制的工程原型。
- BearCode 在生态、交互、跨平台、安全隔离和稳定性上还有明显差距。
- BearCode 值得保留的研究方向是“带证据、可回滚、可评测的能力演化”。

BearCode 最值得向 Claude Code 学习的是文件 checkpoint、按需加载工具 Schema、可视化上下文占用，以及更完整的 Hooks 和扩展生命周期。

### 33. DeepSeek Harness 的核心设计是什么？

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 是 DeepSeek AI 发布的 MIT 开源 Agent Harness，目前处于 Developer Preview。它最鲜明的设计原则是 **Everything is a Plugin**。

它基于 Cordis 组织插件。模型适配器、工具注册表、Session Log、Agent Loop、沙箱和审批策略都作为插件挂载到共享 Context，理论上都可以通过配置替换，而不需要修改一个特权核心。插件注册产生的副作用还可以在卸载时反向撤销。

其[架构文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)还体现了几个重要设计：

- 用 profile 和 bundle 组合 Web、Headless 等不同运行形态。
- Session 使用 append-only 事件日志保存持久事实。
- Agent 事件负责观察或拦截正在执行的工作。
- capability 事件把文件、工具、遥测等策略接到运行时，而不直接依赖 Agent Loop。
- 默认 Agent Loop 只是实现 `Agent` 接口的一个驱动，可以被替换。

它代表的是“插件化 Agent 操作系统”路线，扩展能力和运行时本身使用同一种组合机制。

### 34. BearCode 和 DeepSeek Harness 有什么区别？

DeepSeek Harness 优先追求可组合性，BearCode 优先追求执行链路清晰和研究机制可验证。

| 维度 | DeepSeek Harness | BearCode |
| --- | --- | --- |
| 组织方式 | Cordis 插件树，所有组件都可替换 | Python 模块直接组装，调用链更直观 |
| 扩展方式 | service、typed event、profile、bundle | Tool、MCP、Skill、子 Agent 和配置 |
| 会话状态 | append-only SessionEvent 与投影 | 对话历史加结构化 Session Memory |
| Agent Loop | 作为插件实现，可交换 | Runtime 的核心执行循环 |
| 主要目标 | 通用、可组合的 Agent 平台 | 可控 Harness 与 Skills 自进化验证 |

DeepSeek Harness 的优势是组件边界、插件生命周期和配置组合能力；代价是概念数量多，而且官方明确说明预览期可能发生破坏性变化。BearCode 更容易让面试官沿着一条执行链理解模型、工具、权限、Memory 和 Skill，但扩展组件时更容易触碰核心模块。

BearCode 最应该借鉴三点：定义稳定的 `Agent`、`ToolExecutor`、`SessionStore` 接口；用事件解耦日志、评测和 Skill 演化；让 Agent Loop 和存储实现可以替换。没有必要直接复制 Cordis 的全部复杂度。

### 35. Pi Agent Harness 的核心设计是什么？

[Pi Agent Harness](https://github.com/earendil-works/pi) 是 MIT 开源的 TypeScript Agent 工具集。它包含统一的多模型 API、带状态和工具调用的 Agent Core、Coding Agent CLI、TUI，以及用于嵌入应用的 SDK。

Pi 的关键词是 **minimal and extensible**。它默认保持核心较小，通过 TypeScript Extensions、Skills、Prompt Templates、Themes 和可分享的 Pi Packages 增加能力。官方甚至明确选择不把 subagent 和 plan mode 做成内置默认功能，而是交给扩展或第三方包。

Pi 还提供 interactive、print/JSON、RPC 和 SDK 四种运行方式。其[SDK 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)中的 `AgentSession` 统一管理 Agent 生命周期、消息历史、模型状态、上下文压缩和事件流；JSONL Session 采用树结构，可以在同一会话文件中分叉和导航。

它代表的是“极简内核 + 强扩展 API + 多模型适配”路线。

### 36. BearCode 和 Pi Agent Harness 有什么区别？

两者都适合阅读源码并理解 Agent Loop，也都支持模型适配、工具、会话、压缩和 Skills，但默认取舍几乎相反。

Pi 尽量把功能留在扩展层，核心提供稳定的 Agent、Session、事件和 UI/SDK 基础。BearCode 则把权限模式、Plan Mode、MCP、子 Agent、结构化折叠和 Skills 演化放进一套完整 Runtime，用于展示这些机制如何协作。

因此：

- Pi 更适合做轻量、可嵌入、可按团队工作流定制的通用 Coding Harness。
- BearCode 更适合研究长程状态和反馈驱动的能力沉淀。
- Pi 的多模型 API、SDK/RPC、Session Tree 和扩展 API 更成熟。
- BearCode 的 `pending feedback -> Skill -> eval` 是更明确的研究主线。

BearCode 应学习 Pi 的包边界和可嵌入接口，同时避免把所有新能力继续堆进 Runtime。一个合理拆分是 `bearcode-core`、`bearcode-tools`、`bearcode-memory`、`bearcode-skills` 和 `bearcode-sdk`。

### 37. 这几个典型 Harness 的设计路线可以怎样概括？

| 项目 | 核心定位 | 架构中心 | 最值得学习的机制 | 开放边界 |
| --- | --- | --- | --- | --- |
| Codex | 可嵌入产品的生产级 Agent Runtime | Agent Loop、App Server、SDK、沙箱与审批 | Runtime/UI 分离、事件协议、产品嵌入 | CLI、SDK、App Server 开源；IDE 和 Cloud 不开源 |
| Claude Code | 成熟的全场景 Coding Agent 产品 | 工具循环、上下文治理、权限和用户体验 | checkpoint、按需加载、Hooks、会话恢复 | 仓库公开，但核心 Runtime 不是标准开源软件 |
| DeepSeek Harness | 高度可组合的通用 Agent 平台 | Cordis 插件、service、typed event、profile | 所有组件可替换、可撤销插件副作用、事件化状态 | MIT 开源，当前是 Developer Preview |
| Pi | 极简且可扩展的 Coding Harness | Agent Core、多模型 API、Extensions、SDK/RPC | 小内核、Session Tree、嵌入接口 | MIT 开源 |
| BearCode | 可控 Harness 与自进化研究原型 | Python Runtime、结构化折叠、Skills 演化 | 反馈证据、Skill 维护、版本和在线评测 | 项目内实现，成熟度与生态仍需建设 |

这张表说明行业并不存在唯一正确的 Harness。Codex 强调平台化，Claude Code 强调产品体验，DeepSeek Harness 强调可组合性，Pi 强调极简扩展，BearCode 应把“可治理的能力演化”做深，而不是追求功能数量最多。

### 38. 怎样公平比较不同 Harness，而不是只比较产品体验？

不能直接用“哪个回答看起来更聪明”下结论，因为模型、系统提示、工具、网络、Token 预算和执行环境都可能不同。

更公平的对比方法是：

1. 固定任务集、仓库快照、容器环境、超时和网络条件。
2. 能使用相同模型时统一模型、采样参数和最大 Token；不能统一时明确把结果称为“产品系统对比”。
3. 对齐基础工具集合，例如只开放 read、search、edit、shell 和 test。
4. 同时记录任务成功率、Token、耗时、工具调用数、重复失败数、人工审批次数和危险操作率。
5. 对 compaction、subagent、Skill、MCP、plan 等机制做消融，而不是只看完整系统总分。
6. 保存完整 trajectory 和环境结果，失败案例按规划、检索、工具、权限、上下文和模型错误分类。

BearCode 已有 GAIA/HLE 和折叠消融，但若要与 Coding Harness 正面比较，还需要增加 SWE-bench 类代码任务、真实仓库任务和安全任务集。

### 39. 已经有这些成熟 Harness，为什么还要做 BearCode？

如果目标只是提高日常编码效率，直接使用 Codex、Claude Code 或 Pi 更现实。BearCode 的价值不在于重新做一个终端 UI，而在于掌握并验证三个尚未被产品界面解释清楚的问题：

1. 模型意图如何经过权限和工具协议变成可控执行。
2. 长任务如何在压缩后保留可恢复的结构化状态。
3. 用户反馈如何经过证据、合并、版本和评测，成为长期可复用能力。

这也是面试时最有说服力的回答：我研究主流 Harness，不是为了声称自己做得比成熟产品全面，而是用它们校准工程基线，再把 BearCode 的差异化收敛到“可审计、可回滚、可评测的 Skill 演化”。

### 40. 参考最新 Harness，BearCode 下一步应该怎样演进？

可以按三个优先级推进。

**P0：先补 Runtime 可靠性与安全。**

- 统一所有工具的超时、取消、熔断和错误类型。
- 增加工作区隔离、进程树清理和操作系统级沙箱。
- 工具参数使用 JSON Schema 严格校验，非法参数不进入执行层。
- 为权限、上下文折叠和文件修改增加 checkpoint 与可恢复测试。

**P1：再做架构解耦与嵌入能力。**

- 参考 Codex App Server 定义 thread、turn、event、approval 协议。
- 参考 DeepSeek Harness 用事件解耦 Session、Telemetry、Eval 和 Skill Evolution。
- 参考 Pi 提供小型 SDK/RPC，并拆分核心包与可选能力。
- 增加稳定的 Hook 和 Plugin 生命周期，减少修改 Runtime 核心的需要。

**P2：最后做 BearCode 真正的差异化。**

- 将 Skill candidate、champion、rollback 串成正式状态机。
- 建立离线 replay、在线 usage、人工反馈和安全审查的联合门控。
- 支持 Skill 的作用域、依赖、冲突、过期和灰度发布。
- 证明 Skill 演化能提高任务成功率，而不只是增加 Skill 文件数量。

优先顺序不能反过来：没有稳定执行和安全边界，自进化只会更快地放大错误。

### 41. 如何持续跟进最新的 Harness 工程，而不是做一次性对比？

应该把 Harness 追踪变成项目维护流程。

1. **建立官方来源表**：只记录官方仓库、架构文档、Release 和 Changelog，保存最后核对日期与版本或 commit。
2. **每两周做变更扫描**：关注 Agent Loop、Session、Compaction、Permission、Sandbox、Skill、Plugin、MCP、Subagent、SDK 和 Eval 的变化。
3. **每月更新对比矩阵**：每个新机制必须对应源码位置、解决的问题、代价和 BearCode 是否需要。
4. **先实验再引入**：在 BearCode 建最小原型并做消融，只有改善成功率、安全性、成本或可维护性的机制才进入主线。
5. **保留观察池**：除本节四个项目外，还可以持续跟踪 [Gemini CLI](https://github.com/google-gemini/gemini-cli)、[OpenHands](https://github.com/All-Hands-AI/OpenHands)、[goose](https://github.com/block/goose)、[SWE-agent](https://github.com/SWE-agent/SWE-agent) 等官方仓库。

最终产物不应只是“竞品功能列表”，而应是 Harness ADR：它说明一个机制解决了什么问题、为什么适合或不适合 BearCode、如何验证，以及采用后怎样回滚。

## 八、公开面经高频追问

> 本节根据 2026-08-24 前公开的[小红书 Agentic 全栈研发面经](https://www.nowcoder.com/feed/main/detail/e5e9311a623940eead6ec98c65e7f9e8)、[小红书校招 Agent 场景题](https://www.nowcoder.com/enterprise/715/interview)、[美团/字节/蚂蚁等 Agent 岗面经](https://www.nowcoder.com/discuss/919673484509184000)整理。回答严格区分“当前已实现”和“下一步计划”，避免把设计方案说成项目成果。

### 42. BearCode 中的 Agent 真正用在哪里？哪些地方不应该交给模型？

BearCode 把 Agent 用在“下一步依赖环境反馈、无法提前写死”的位置。例如面对一个代码修改任务，模型需要先判断读哪些文件、调用什么工具、如何根据报错调整方案，以及是否需要检索 Memory、启用 Skill、折叠上下文或委派子 Agent。

但以下内容不交给模型决定：

- 工具是否实际执行，由 Runtime 控制。
- 文件和 Shell 权限，由权限规则和权限模式控制。
- 工具参数解析、调用 ID 关联和结果回写，由协议代码控制。
- 文件覆盖前必须读取、`mtime` 是否变化，由确定性代码检查。
- Token、费用和最大轮次是否超限，由预算逻辑判断。
- 最终是否成功，应该由测试、文件状态或业务验收条件判断，而不是模型自己说“完成了”。

一句话概括：模型负责不确定的语义决策，Harness 负责确定性的执行、安全、状态和验证。

### 43. 能否按照代码调用链讲一遍一条用户请求？

可以按下面这条主链回答：

```text
用户输入
  -> agents/main.py 接收命令
  -> Agent.chat()
  -> 首轮懒加载 MCP 工具
  -> 处理上一轮 pending feedback
  -> 检索相关 Skills，预取长期 Memory
  -> 进入 OpenAI 或 Anthropic 协议循环
  -> 模型输出文本或 tool call
  -> Runtime 解析参数并检查权限
  -> 执行内置工具 / MCP / Skill / 子 Agent
  -> 用 tool_call_id 或 tool_use_id 回写结果
  -> 模型根据 Observation 继续推理
  -> 没有新工具调用时结束
  -> 保存 Session
  -> 后台统计 Skill 使用并创建下一轮反馈窗口
```

Anthropic 和 OpenAI 的消息格式不同，但语义相同：模型只提出 Action，Runtime 执行后返回 Observation。主循环在 [agent.py](/home/hw/project/bearcode2/agents/agent.py)，工具和权限在 [tools.py](/home/hw/project/bearcode2/agents/tools.py)，MCP 路由在 [mcp_client.py](/home/hw/project/bearcode2/agents/mcp_client.py)。

### 44. BearCode 为什么选择 Agent Loop？Workflow、ReAct 和 Planner-Executor 应该怎么选？

步骤固定、规则明确、错误分支有限的任务优先使用 Workflow，因为状态迁移可预测、容易测试，也不必让模型每一步重新决策。

任务路径取决于外部观察时适合 ReAct。例如修改代码时，要先读文件，再根据真实内容决定搜索、编辑或测试。BearCode 当前的主循环属于 Tool Calling 驱动的 ReAct 风格：模型产生 Action，Runtime 执行，Observation 回写，模型继续判断下一步。

长任务存在明确依赖时，可以使用 Planner-Executor：Planner 先产生阶段计划，Executor 执行当前步骤，失败后局部重规划。BearCode 已有 Plan Mode，但还不是完整的图式 Planner-Executor Runtime，也没有独立的 Critic 状态机。

选择标准不是哪个名词更新，而是任务的路径是否固定、执行风险、失败恢复成本和验收条件是否明确。

### 45. ReAct 为什么容易死循环？BearCode 当前如何处理？

ReAct 容易陷入局部贪心：模型只根据最近一次 Observation 选择下一步，可能重复同一个错误参数、在两个工具之间来回切换，或者把“继续尝试”误当成“正在取得进展”。

BearCode 当前有三类控制：

1. 支持 `max_turns` 和费用预算，超限后停止继续执行工具。
2. 统计连续工具错误和同名工具重复次数，并注入下一轮提示。
3. 失败和上下文噪声累积时，可以主动或自动执行结构化折叠。

当前边界是 `max_turns` 和费用限制可以不配置，而且重复检测主要看工具名，不能识别“同一工具 + 同一参数 + 同一结果”的无进展循环。

下一步应增加默认墙钟、Token、步骤和重试上限，并根据“工具名 + 规范化参数 + 结果摘要 + 任务状态版本”生成 Action 指纹。相同指纹重复且状态不变化时，必须进入 `revise`、`replan`、`ask_user` 或 `stop`。

### 46. Planner、Executor 和 Critic 分别负责什么？BearCode 现在都有吗？

Planner 负责把目标拆成带依赖关系的步骤，并定义每一步的输入、产物和完成条件。Executor 只执行当前可运行的步骤，可以在步骤内部使用 ReAct 调工具。Critic 负责根据测试、Schema、业务状态或 rubric 判断结果应该通过、修改、重规划还是升级人工。

三者是职责，不一定要使用三个模型或三个 Agent。能用编译、测试和数据库状态验证的内容，应优先使用确定性 Critic；只有主观质量或长尾语义才使用 LLM Judge。

BearCode 当前有 Plan Mode、执行循环、子 Agent 和在线 Skill Eval，但还没有统一的 `Planner -> Executor -> Critic` 状态机。更准确的说法是已经具备部分组件，还没有完成正式编排。

### 47. Skill、Tool 和 MCP 有什么区别？

Tool 是原子执行能力，例如读取文件、搜索代码、运行 Shell。它有明确参数和返回值，真正执行发生在 Runtime。

MCP 是外部能力接入协议。MCP Server 可以向 Agent 暴露 Tool、Resource 或其他能力；BearCode 当前通过 stdio JSON-RPC 发现并调用 MCP Tool。MCP 解决“工具怎么标准化连接”，不负责规定完成一个业务任务的全部步骤。

Skill 是可复用的方法和流程。例如“修复 Bug 时先复现、再定位、最小修改、最后回归”属于 Skill。一个 Skill 可以调用多个内置 Tool，也可以编排多个 MCP Tool。

简化表达是：Tool 是动作，MCP 是连接动作的协议，Skill 是组织动作的方法。

### 48. Skill 在什么阶段检索和加载？为什么不把所有 Skill 全量放进 Prompt？

BearCode 启动时只把 Skill 的名称和描述等轻量信息放入系统能力说明。每条用户请求到来后，再根据请求检索最多三个相关 Skill，将相关上下文注入本轮输入。模型也可以显式调用 `skill` 工具，按需展开完整流程；fork 类型 Skill 会在独立上下文执行。

不全量加载有三个原因：

1. Skill 数量增长后会持续占用上下文。
2. 无关规则可能互相干扰，降低模型选择能力。
3. 按需加载才能记录哪些 Skill 被检索、展开和实际使用，为后续评测和归档提供证据。

当前检索还是轻量词法方案，后续可以增加向量召回和 rerank，但必须保留作用域、权限和来源过滤。

### 49. 怎么判断一个 Query 是明确还是模糊？BearCode 有独立意图路由吗？

当前 BearCode 没有独立意图分类器，主要由模型根据上下文直接判断并选择工具。因此不能声称已经实现稳定的意图分流。

工程上可以从四个维度判断任务是否需要澄清：

- 必要输入是否缺失，例如目标文件、数据范围或输出格式。
- 是否存在多个合理解释，而且不同解释会产生明显不同结果。
- 是否包含不可逆、高成本或外部副作用。
- 是否有可验证的完成条件。

低风险且容易回滚的任务可以做合理假设后继续；高风险写操作、目标冲突或成功条件不清晰时必须询问用户。后续可以增加轻量 Router，但鉴权、预算、权限和副作用判断仍应由确定性代码完成。

### 50. 模型连续调用多个工具时，如何保证结果不混乱？

BearCode 使用模型协议中的调用 ID 做关联。

- OpenAI-compatible：先保存带 `tool_calls` 的 assistant 消息，再为每个结果追加 `role=tool` 消息，并使用对应的 `tool_call_id`。
- Anthropic-compatible：每个 `tool_use` 有独立 ID，返回的 `tool_result` 使用相同 `tool_use_id`。

并发方面，只把 `read_file`、`list_files` 和 `grep_search` 视为并发安全工具。写文件、Shell、Skill 演化和其他可能有副作用的工具顺序执行。并行结果即使完成顺序不同，也通过调用 ID 回到对应 Action。

当前仍缺少副作用幂等协议。工具超时不能简单自动重试，因为第一次调用可能已经成功，只是结果丢失。生产版本应增加 `executionId`、副作用状态和幂等键，将结果区分为 `not_started`、`committed` 和 `unknown`。

### 51. 多 Agent 并发一定能提高性能吗？BearCode 当前怎么做？

不一定。并发会同时消耗模型 RPM/TPM、Token、数据库连接、文件句柄和第三方 API 配额；最终聚合通常还要等待最慢分支，因此 P95/P99 可能反而上升。

BearCode 当前的子 Agent 采用隔离上下文和结果返回模式，但没有完整的并行 Scheduler；`agent` 工具也没有被标记为并发安全。因此当前更准确的定位是支持任务委派，而不是成熟的并行多 Agent 编排。

如果升级为并发执行，需要增加：

- 最大 fan-out、最大深度和全局 Semaphore。
- 每个分支的 deadline、取消传播和 partial result 策略。
- 模型、工具和下游服务的分层并发预算。
- 相同探索任务的去重。
- 结构化产物契约，避免把所有子 Agent 对话塞回主上下文。

只有可独立、无共享写状态、聚合成本低的任务才适合并行。

### 52. 主 Agent 和子 Agent 的权限应该怎样继承？当前实现安全吗？

正确原则是子 Agent 只能缩小权限，不能放大权限：

```text
子 Agent 有效权限
  = 父 Agent 有效权限
  ∩ Agent 类型白名单
  ∩ 当前任务临时能力
```

BearCode 已经让 `explore` 和 `plan` 子 Agent 只获得只读工具，并禁止子 Agent 再递归创建 Agent。但当前 `general` 子 Agent 和 fork Skill 在父 Agent 非 Plan Mode 时会使用 `bypassPermissions`，这可能使子 Agent 的有效权限高于父 Agent，是现有实现中需要优先修复的问题。

后续还要解决共享写状态：不同子 Agent 同时修改相同文件时，应使用独立工作区、单写者或版本检查；父 Agent 中止后，也必须取消所有子 Agent、工具调用和子进程。

### 53. BearCode 支持不同模型时，如何保证行为一致？

当前实现维护 OpenAI-compatible 和 Anthropic-compatible 两条协议循环，使用同一套内部 Tool Schema，再转换成各自接口格式。两条链路都完成工具调用解析、权限判断、结果回写、Token 统计和上下文压缩。

但协议兼容不等于模型行为一致。不同模型在工具选择、并行调用、JSON 参数、Thinking、上下文窗口和停止原因上都可能不同，所以不能保证输出完全一致。

下一步应该统一内部 `ModelRequest`、`ModelEvent`、`ToolCall`、`Usage` 和 `StopReason`，Provider Adapter 只处理字段转换。然后使用同一组 Fake Stream 和 replay 对两种协议做契约测试，指标比较任务成功率、参数正确率、工具次数、Token 和延迟，而不是要求文本逐字相同。

### 54. Context Builder 和上下文折叠有什么区别？

Context Builder 解决“本次模型调用应该看到什么”，上下文折叠解决“历史太长时如何保留可继续执行的状态”。折叠只是 Context Builder 的一个输入来源，不应该代替整个上下文管理。

一个完整 Context Builder 应区分：

1. `Policy`：系统规则、权限和输出契约。
2. `TaskState`：目标、计划、完成项、阻塞点和待审批项。
3. `RecentContext`：最近交互和当前工具结果。
4. `FoldedHistory`：结构化 Session Memory。
5. `RetrievedContext`：Memory、Skill、代码和资料。
6. `ToolContext`：当前候选工具的最小 Schema。

BearCode 已经分别实现了这些能力中的大部分，包括动态 System Prompt、Memory 预取、Skill 检索、Tool Schema 延迟加载、工具结果裁剪和结构化折叠。但组装逻辑仍分散在多个函数中，缺少统一 Token 预算和 `/context` 可解释视图。

### 55. 如何防止工具结果或 MCP Server 对 Agent 发起 Prompt Injection？

当前 System Prompt 会提醒模型把工具结果视为可能不可信内容，权限层也能阻止部分危险操作。但这主要依赖模型识别，不能作为完整安全边界。

更可靠的方案分四层：

1. 给 System Policy、用户指令、项目规则、检索数据和工具结果标记不同信任级别。
2. 工具、网页和文件中的“指令”只能作为数据，不能覆盖权限、预算和完成条件。
3. 高风险写操作必须能追溯到用户目标或已批准计划，不能只由工具结果触发。
4. MCP 子进程只获得显式允许的环境变量和凭据，并记录 Server、调用 ID 与结果来源。

测试时应主动构造恶意 README、网页、Memory 和 MCP 返回值，检查它们能否诱导 Agent 读取密钥、越权写文件或调用危险命令。

### 56. 如果要承接几千用户、每天十几万次请求，BearCode 怎么扩容？

当前 BearCode 是本地 CLI Runtime，不能直接声称支持这个规模。扩容时应把无状态接入层和有状态任务执行层分开：

```text
API Gateway
  -> 鉴权、配额、幂等和限流
  -> 有界任务队列
  -> Agent Worker
  -> Model / Tool / Sandbox
  -> SessionStore + EventStore + ArtifactStore
```

并发预算应按 `tenant -> user -> session -> task -> model/tool` 分层设置。Session 内默认单写，多个 Session 可以并行；模型受 RPM/TPM 约束，Shell 和浏览器受沙箱池约束，MCP 和第三方 API 受连接池及对方限额约束。

本地 Markdown/JSONL 可以继续作为开发模式，服务模式需要可替换的 Store 接口，再接 SQLite、Postgres、Redis 或对象存储。扩容能力必须通过压测报告证明，而不是只画架构图。

### 57. BearCode 当前做了哪些延迟和成本优化？还可以怎样优化？

当前已经实现：

- 模型响应流式输出，降低首段内容等待时间。
- 长期 Memory 异步预取，不阻塞首次模型请求。
- Anthropic 流中完整收到并发安全 Tool Call 后可以提前执行。
- OpenAI 分支可以批量并行执行只读工具。
- Tool Schema 支持延迟发现，减少无关 Schema Token。
- 大工具结果截断或持久化，旧结果会进一步压缩。
- Skill 统计和在线演化在主回复后后台运行。

后续可以增加请求缓存、相同只读调用去重、小模型处理分类和抽取、统一 deadline、连接复用以及动态并发控制。优化指标应同时看首 Token、端到端 P95、Token、工具调用数和单位成功成本，不能只看平均延迟。

### 58. 如何定义 Agent 的任务成功？BearCode 应监控哪些指标？

任务成功不能以模型输出“已完成”为准。代码任务至少要满足目标文件发生预期变化、编译或测试通过、没有越权副作用，并符合用户验收条件。

BearCode 当前用 GAIA/HLE 的 `Pass@1` 评估整体任务能力，用上下文折叠消融验证长程状态管理，并用 replay、规则和 LLM Judge 评估 Skill。这些属于离线效果指标。

如果作为服务运行，还应监控：

- 任务完成率和人工接管率。
- 工具选择、参数校验和执行成功率。
- 超时、重复 Action、无进展终止和恢复率。
- 危险操作提出率、拦截率和错误副作用率。
- P50/P95/P99、Token、模型费用和单位成功成本。
- Context Folding 后任务恢复率。
- Skill 检索、显式使用、增益、回退和污染率。

最终需要将指标关联到模型、Prompt、Skill、工具版本和完整 Trace，才能解释一次回归来自哪里。

### 59. 如果面试官要求讲一个真实 Bad Case，应该讲什么？

可以讲 Skill `used` 证据不可靠的问题。

早期设计在回答结束后，让 side-query LLM 根据用户问题、最终答案和 Skill 摘要判断某个 Skill 是否被使用。这能判断回答“看起来是否符合”，但不能证明生成时真的展开或执行了该 Skill。Side query 不可用时，如果再退化成名称匹配，误判会更严重。

这个问题会污染三类下游逻辑：使用统计不可信、无效 Skill 归档可能误杀、在线评测可能把相关性当成因果增益。

改进方案是把证据拆开：Runtime 在 Skill 被展开、显式调用或 fork 执行时记录 `used`；事后 Judge 只记录 `inferred_used`。同时，只有被确认 `relevant` 或显式使用的 Skill 才能作为新候选 merge 的参考锚点。

这个案例能说明项目难点不只是“生成 Skill”，而是如何保证自进化证据可靠、可审计并且不会自我强化错误。

### 60. 面试官说项目太 Toy，应该怎么回应？

先承认当前边界，再给出已经完成的非 Demo 机制和可验证证据。

BearCode 不只是单次 API 调用：它实现了两种模型协议的多轮工具循环、权限检查、文件一致性保护、MCP、子 Agent、Session 恢复、结构化上下文折叠，以及带 provenance、版本和在线评测的 Skill 演化。项目还有 GAIA/HLE 基线与折叠消融，说明部分设计经过了效果验证。

但它目前仍是本地工程原型，不具备完整的操作系统级沙箱、高并发服务、统一 Tool Executor、端到端 Trace、完整自动化测试和生产级多租户隔离。这些不应该包装成已经完成。

最好的回应不是争论“是不是 Toy”，而是展示清楚的工程路线：先补 Schema、超时、取消、幂等和子 Agent 权限；再统一 Provider、Context 和 Event；然后做 Trace/Replay、Coding Task、安全评测和并发压测。这样能说明自己知道原型和生产系统之间差在哪里，也知道如何用测试与指标逐步跨过去。
