## 结论

当前项目不是两篇论文的完整复现，而是一个 Bear Code Coding Agent Harness，分别吸收了：

- DeepAgent：长程任务中的结构化会话记忆折叠，以及部分工具发现、评测设计。
- AutoSkill：从用户交互中抽取、维护、版本化、检索和复用 Skills 的闭环。

项目自己的权限系统、Plan Mode、MCP、子 Agent、OpenAI/Anthropic 双协议等，不是这两篇论文的直接实现。

## 与 DeepAgent 的关系

| DeepAgent 机制 | 当前项目实现 | 参考程度 |
| --- | --- | --- |
| Episodic / Working / Tool 三层记忆 | [session_memory.py](/home/hw/project/bearcode2/agents/session_memory.py:23) 使用 `episode_memory`、`working_memory`、`tool_memory` 三段 JSON | 很高，结构基本一一对应 |
| Agent 自主触发 Memory Folding | 把论文中的 `<fold_thought>` 特殊动作改成 `compact_context` 工具，[tools.py](/home/hw/project/bearcode2/agents/tools.py:142) | 高，但改成标准 tool call |
| 折叠后替换原始历史继续执行 | `_compact_anthropic()` / `_compact_openai()` 用结构化记忆替换历史，[agent.py](/home/hw/project/bearcode2/agents/agent.py:934) | 高 |
| 系统兜底折叠 | 上下文达到 70% 自动触发，同时保留模型主动触发，[agent.py](/home/hw/project/bearcode2/agents/agent.py:928) | 项目扩展 |
| 辅助模型压缩历史 | 使用 side query 生成折叠 JSON，[agent.py](/home/hw/project/bearcode2/agents/agent.py:979) | 部分对应；项目没有单独部署专用 auxiliary model |
| 动态工具发现 | 提供 `tool_search` 和 deferred tools，[tools.py](/home/hw/project/bearcode2/agents/tools.py:224) | 只实现了简化版 |
| 下游 benchmark | `data/` 包含 ALFWorld、WebShop、GAIA、HLE、ToolBench、API-Bank、RestBench、ToolHop | 数据集选择高度一致 |

DeepAgent 论文把 Memory Fold 定义为四类 Agent 动作之一，并明确使用 episodic、working、tool 三层结构化记忆；项目的 folding schema 明显直接参考了这一部分。[DeepAgent 官方论文](https://arxiv.org/html/2510.21618)

但以下 DeepAgent 核心内容没有实现：

- ToolPO 强化学习。
- LLM Tool Simulator。
- Tool-call advantage attribution。
- 经过 RL 训练的 DeepAgent 模型。
- 面向数千、上万工具的 embedding + cosine top-k 检索。
- 对工具文档和工具结果进行辅助 LLM 去噪、总结。
- 论文中的端到端连续 reasoning action 协议。

当前 `tool_search` 只是对少量 deferred schema 做关键词包含匹配，目前 deferred 的主要还是 Plan Mode 工具，所以不能等同于 DeepAgent 的 scalable toolsets。

### 一个重要的评测问题

[评测部分.md](/home/hw/project/bearcode2/wiki/评测部分.md:101) 中：

- GAIA All：53.3
- HLE All：20.2
- 关闭 Memory Folding 后 GAIA：44.7

这些数字与 DeepAgent 论文中 `DeepAgent-32B-RL` 和 `w/o Memory Folding` 的结果逐项一致。论文原表也是 GAIA 53.3、HLE 20.2、去掉 folding 后 GAIA 44.7。[DeepAgent 实验与消融](https://arxiv.org/html/2510.21618)

而当前仓库里没有 GAIA/HLE benchmark 执行器，只有数据、Wiki 和 Skill 在线评测代码。因此从仓库证据看，这些数字更像引用了 DeepAgent 的论文结果，不能直接表述成 Bear Code 独立运行得到的结果。文档中的“当前项目测评结果”容易造成误解。

## 与 AutoSkill 的关系

| AutoSkill 机制 | 当前项目实现 | 参考程度 |
| --- | --- | --- |
| Skill-enhanced response loop | 对用户输入检索 Skill，并注入 `<retrieved_skills>`，[skills.py](/home/hw/project/bearcode2/agents/skills.py:278) | 高 |
| Interaction-driven evolution loop | 每轮保存 pending window，下一轮用户反馈补齐后异步演化，[agent.py](/home/hw/project/bearcode2/agents/agent.py:691) | 高，项目化改造 |
| Skill Extractor | `extract_online_skill_candidate()`，[online_skill_evolution.py](/home/hw/project/bearcode2/agents/online_skill_evolution.py:97) | 高 |
| Maintainer 的 add / merge / discard | `maintain_online_skill_candidate()`，[online_skill_evolution.py](/home/hw/project/bearcode2/agents/online_skill_evolution.py:156) | 基本对应 |
| 相似 Skill 辅助维护 | 先进行身份匹配和 BM25-lite 检索，再让 Maintainer 决策 | 对应但简化 |
| SKILL.md 显式能力资产 | 项目级 `.bear/skills/` 和用户级 `~/.bear/skills/` | 高 |
| 版本化合并 | 新 Skill 从 `0.1.0` 开始，merge 提升 patch 版本并保存旧快照，[skill_evolution.py](/home/hw/project/bearcode2/agents/skill_evolution.py:313) | 高 |
| 后台演化 | 主回答完成后异步执行 usage tracking 和 evolution，[agent.py](/home/hw/project/bearcode2/agents/agent.py:490) | 高 |
| 无需训练基础模型 | 全部通过 prompt、文件和检索完成 | 一致 |

AutoSkill 论文的主线正是两套耦合循环：

1. query rewriting → Skill 检索 → 上下文注入 → 回复；
2. 交互证据 → Extractor → 相似 Skill 检索 → add/merge/discard → 版本更新。

当前项目主要参考了第二条闭环，以及第一条中的 Skill 检索和注入。[AutoSkill 官方论文](https://arxiv.org/html/2603.01145)

### 与原论文的差异

当前项目没有完整实现：

- 专门的 query rewriting 模型。
- embedding dense retrieval。
- dense + BM25 混合打分。
- Skill 向量索引和缓存。
- 严格的 per-user SkillBank / Common SkillBank 多用户结构。
- OpenAI reverse proxy、Web UI。
- 离线对话、文档、Agent trajectory 导入。

论文强调 Extractor 只使用用户 query。当前实现会把 assistant 消息也传进去，但在 prompt 中规定“用户消息是证据，assistant 只作为上下文”，同时加入“下一轮用户反馈”来确认上一轮效果。这是 Bear Code 自己的 pending-window 改造。

此外，项目的 [online_skill_eval.py](/home/hw/project/bearcode2/agents/online_skill_eval.py:1485) 包含 replay、规则评测、LLM judge、候选变体和 champion。它们不属于 AutoSkill PDF 的核心方法；更接近 AutoSkill 官方仓库后来单独提供的 `SkillEvo`——replay、evaluation、mutation、promotion 框架。[AutoSkill 官方仓库](https://github.com/ECNU-ICALK/AutoSkill)

一句话概括：

> Bear Code = 基础 Coding Agent Harness + DeepAgent 风格的 Session Memory Folding + AutoSkill 风格的 Skills 自进化闭环，再加上项目自己的权限、MCP、Plan Mode、子 Agent 和审计评测层。