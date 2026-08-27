# Bear Agent

Bear Agent 是一个基于 Python 实现的 **自进化 Harness Agent**。它不是简单的命令行聊天工具，而是一个可运行、可阅读、可扩展的本地 Coding Agent Runtime：统一编排大模型推理、工具调用、文件编辑、Shell 执行、权限控制、长期记忆、Skills、自进化、MCP 外部工具、子 Agent 和会话恢复。

项目重点是 **Harness**：模型只负责推理和提出工具调用意图，真正的环境操作由 Bear Code Runtime 统一做权限判断、工具执行、结果回写、上下文压缩和经验沉淀。它适合学习 Claude Code 类工具的底层机制，也适合作为个人 Coding Agent、项目分析助手或领域 Agent 的二次开发基础。

当前项目运行流程以 [`RUNTIME_FLOW.md`](RUNTIME_FLOW.md) 为唯一真相源；仓库采用 doc-first 约定，行为修改必须先更新流程文档和变更账本，再修改实现。

## 核心亮点

- **可归因的 Skills 自进化**：先把跨轮反馈归因为 `skill_gap / capability_limit / evaluation_noise`，只有可修复缺口才能形成隔离 proposal；通过评测和显式发布后才新增或合并 active `SKILL.md`。
- **双锚点回归治理**：候选在 dev 样本上选优后，在隔离 promotion-test 上逐样本对比当前 active，并继续通过历史 champion gate；平均分上涨不能掩盖已有能力回退。
- **完整 Agent Loop**：模型请求、tool call 解析、权限检查、工具执行、tool result 回写、继续推理、会话保存形成闭环。
- **轻量 Docker Sandbox**：Shell、测试构建和受信任 MCP 在会话级容器内运行；Approval 与隔离分层，`--yolo` 也不能访问 workspace 外的宿主资源。
- **OpenAI / Anthropic 双协议**：支持 OpenAI-compatible 和 Anthropic-compatible 接口，便于接入不同模型服务或代理网关。
- **工具系统与权限控制**：支持读写文件、精确编辑、代码搜索、Shell 命令、Skill 调用、子 Agent 和 MCP 工具；Plan Mode 下阻断写操作和 Shell。
- **Skills 体系**：通过项目级和用户级 `SKILL.md` 保存可复用任务方法，支持检索、调用、inline / fork 执行和版本化演化。
- **长期 Memory**：按项目路径 hash 隔离记忆，保存用户偏好、项目背景、历史决策和参考资料。
- **MCP 外部工具扩展**：自研 stdio JSON-RPC MCP Client，把外部 MCP Server 工具包装为 `mcp__server__tool`。
- **子 Agent**：支持 `explore`、`plan`、`general` 以及自定义子 Agent，用隔离上下文完成探索、规划或局部任务。
- **会话恢复和上下文压缩**：自动保存 session，支持 `--resume`、`/compact`，并对大工具结果做截断或持久化。

## 项目架构

![Bear Agent 总体架构](wiki/assets/architecture/01-overall-architecture.svg)

核心运行链路：

```text
用户输入
  -> agents/main.py
  -> Agent.chat()
  -> 构建 Prompt / 检索 Skills / 预取 Memory / 初始化 MCP
  -> 调用 OpenAI-compatible 或 Anthropic-compatible 模型
  -> 模型返回文本或 tool call
  -> Harness 做权限检查
  -> 执行工具 / Skill / MCP / 子 Agent
  -> tool result 回写模型
  -> 保存 Session
  -> 后台执行 Skill usage tracking 和 online skill evolution
```

## 目录结构

```text
BearAgent/
├── agents/
│   ├── main.py                    # CLI 入口、REPL、参数解析
│   ├── agent.py                   # Agent Runtime、模型调用、工具调度、上下文压缩
│   ├── tools.py                   # 内置工具和权限系统
│   ├── sandbox.py                 # Docker Sandbox Session、资源限制和执行事件
│   ├── prompt.py                  # System prompt 动态构建
│   ├── skills.py                  # Skills 加载、检索、执行、创建和演化封装
│   ├── online_skill_evolution.py  # 在线反馈归因、Skill 抽取和 add/merge/discard 决策
│   ├── online_skill_eval.py       # replay、候选试跑、回归治理和 champion 记录
│   ├── skill_evolution.py         # Skill 落盘、版本快照、审计统计
│   ├── memory.py                  # 长期记忆系统
│   ├── mcp_client.py              # MCP stdio JSON-RPC 客户端
│   ├── subagent.py                # 子 Agent 配置
│   ├── session.py                 # 会话保存与恢复
│   └── ui.py                      # 终端 UI 输出
├── .bear/
│   ├── skills/                    # 项目级 Skills
│   └── skill-evolution/           # Skills 自进化审计产物
├── wiki/                          # 项目文档中心
├── Dockerfile                     # 完整 Harness 镜像
├── Dockerfile.sandbox             # 独立执行 Sandbox 镜像
├── requirements.txt
└── README.md
```

## 快速启动

### 1. 准备环境

推荐环境：

- Python 3.11+
- macOS / Linux
- Git
- ripgrep，可选但推荐
- 一个 OpenAI-compatible 或 Anthropic-compatible 模型接口

安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置 `.env`

项目会自动读取当前目录或父目录中的 `.env`。Sandbox 会挂载整个项目目录，因此项目内
`.env` 也能被容器读取；如果需要让模型密钥只留在控制面，优先使用宿主环境变量，或把
`.env` 放在 workspace 外的父目录。`.dockerignore` 不会过滤运行时 bind mount。

Anthropic-compatible 示例：

```env
APIKEY=sk-your-api-key
API=https://your-host/anthropic
MODEL=claude-sonnet-4-6
```

OpenAI-compatible 示例：

```env
OPENAI_API_KEY=sk-your-api-key
OPENAI_BASE_URL=https://your-host/v1
MODEL=gpt-4o
```

也可以使用通用变量：

```env
APIKEY=sk-your-api-key
API=https://your-host/v1
MODEL=deepseek-chat
```

协议判断规则：

- `API` 或 `--api-base` 路径包含 `/anthropic` 时，按 Anthropic-compatible 调用。
- 否则有 OpenAI base URL 时，按 OpenAI-compatible 调用。
- `--model` 会覆盖 `.env` 中的 `MODEL`。

### 3. 启动 REPL

先构建独立执行镜像（Harness 和宿主环境中的模型密钥不注入容器；项目内 `.env` 会随 workspace 挂载，建议将 Key 放在宿主环境或 workspace 外）：

```bash
docker build -f Dockerfile.sandbox -t bear-code-sandbox:latest .
```

```bash
python3 -m agents.main
```

启动后直接输入任务，例如：

```text
阅读这个项目，告诉我 Agent Loop 是怎么跑起来的
```

### 4. 执行一次性任务

```bash
python3 -m agents.main "总结这个项目的目录结构和核心模块"
```

### 5. 使用 Plan Mode

Plan Mode 适合重构、复杂修复和多文件修改。它会先只读分析和写计划，用户审批后再执行。

```bash
python3 -m agents.main --plan "分析 Skills 检索逻辑应该如何优化"
```

REPL 中也可以输入：

```text
/plan
```

### 6. 恢复最近会话

```bash
python3 -m agents.main --resume
```

### 7. 启动本地 Web Developer Console

先构建一次前端，再启动只监听本机的控制台：

```bash
npm --prefix web install
npm --prefix web run build
python3 -m agents.main --web
```

浏览器访问 `http://127.0.0.1:8000`。前端开发时可另开终端运行
`npm --prefix web run dev`；Vite 会把 `/api` 请求代理到本地 FastAPI。

## Sandbox 安全模型

默认 Shell 与已信任的 stdio MCP Server 运行在每个主 Session 懒创建的持久 Docker
容器中。容器只读挂载系统层，只把当前 workspace 读写挂载到 `/workspace`，并启用默认
断网、非 root UID、`cap-drop=ALL`、`no-new-privileges`、1 CPU、1 GiB 内存和 128 PID
上限。Docker 不可用时 Shell 会 fail closed，不会偷偷退回宿主机。

Web 顶栏会直接显示 Sandbox `未启动/运行中/已停止/已关闭/本地不隔离` 状态；没有
`sandbox.created` 的会话并未创建 Container，收尾时也不会产生虚假的 `sandbox.destroyed`。
独立镜像会预装项目 `requirements.txt` 和 pytest，源码则通过 `/workspace` bind mount 复用。

Approval 和 Sandbox 是两层：`default/acceptEdits/plan/bypassPermissions` 决定是否询问，
Docker 决定命令实际上能访问什么。因此 `--yolo` 只跳过询问；只有启动时显式传入
`--unsafe-local` 才会关闭 Docker 隔离。文件工具仍在宿主侧执行，但 canonical path
只能位于 workspace 或明确的 Runtime 状态目录，拒绝绝对路径越界、`..` 和符号链接逃逸。

项目配置示例见 [`.bear/settings.example.json`](.bear/settings.example.json)。可重复演示：

```bash
PYTHONPATH=. .venv/bin/python scripts/sandbox_demo.py
```

报告写入 `.bear/sandbox-evaluation.json`，包含攻击用例通过率、冷启动、warm command P95
和超时进程树清理结果。设计与面试讲法见 [`wiki/Sandbox架构与面试演示.md`](wiki/Sandbox架构与面试演示.md)。

## 如何让项目自动沉淀并进化 Skills

Bear Code 的核心特色是 **自进化 Skills**。它可以从用户明确反馈中抽取未来可复用的规则，并自动新增或合并到项目级或用户级 `SKILL.md`。

### 1. 开启自动自进化

默认 `BEAR_AUTO_SKILL_EVOLUTION` 是开启的。为了明确配置，建议在 `.env` 中写：

```env
BEAR_AUTO_SKILL_EVOLUTION=1
BEAR_AUTO_SKILL_TARGET=project
# 用户级 Skill 默认 surfaced 40 次且 invoked 为 0 时归档
BEAR_SKILL_USAGE_PRUNE_MIN_SURFACED=40
BEAR_SKILL_USAGE_PRUNE_MAX_INVOKED=0
```

含义：

- `BEAR_AUTO_SKILL_EVOLUTION=1`：启用在线 Skill 自进化。
- `BEAR_AUTO_SKILL_TARGET=project`：proposal 记录未来发布到项目级 Skill。
- `BEAR_SKILL_USAGE_PRUNE_MIN_SURFACED`：自动归档前要求的摘要注入样本数。
- `BEAR_SKILL_USAGE_PRUNE_MAX_INVOKED`：允许自动归档的最大真实调用数。

如果希望通过评测后的新 Skill 最终发布为所有项目共享的个人 Skill：

```env
BEAR_AUTO_SKILL_TARGET=user
```

proposal 一律先保存在项目隔离区；显式发布后的 active 路径为：

```text
project: <project>/.bear/skills/<skill_name>/SKILL.md
user:    ~/.bear/skills/<skill_name>/SKILL.md
```

### 2. 正常启动在线候选抽取

后台只生成 proposal，不需要为了在线演化开启绕过权限的模式：

```bash
BEAR_AUTO_SKILL_EVOLUTION=1 \
BEAR_AUTO_SKILL_TARGET=project \
python3 -m agents.main
```

评测通过后，再在 REPL 中执行 `/skill-promote <skill-name>`。这个显式命令才会创建或演化 active `SKILL.md`；`--accept-edits` 和 `--yolo` 都不会让后台 proposal 自动发布。

### 3. 给出可复用反馈

自进化不是把每一句话都写成 Skill。它只适合沉淀稳定、明确、未来同类任务仍适用的规则。

不太适合沉淀：

```text
帮我写一篇 500 字政府报告。
```

适合沉淀：

```text
以后写政府报告、工作汇报、调研材料时，默认先给可直接使用的初稿，不要连续追问；结构按“标题、背景、主要情况、问题分析、工作举措、下一步计划”组织，语言要正式克制。
```

适合演化已有 Skill：

```text
以后这类政府报告还要加入关键数据指标，不能只写概念性表述。
```

### 4. 自进化链路如何工作

```text
第 N 轮用户任务
  -> Agent 输出结果
  -> 保存 pending extraction window
第 N+1 轮用户反馈
  -> 合并进上一轮 window
  -> online_ingest()
  -> Attributor 判断 skill_gap / capability_limit / evaluation_noise
  -> 仅对 skill_gap 抽取候选 Skill
  -> Maintainer 结合 skill_trace 判断 add / merge / discard
  -> 写入隔离 proposal（active SKILL.md 不变）
  -> /skill-eval：dev 增益 + promotion-test 质量 + 零回归 + champion 比较
  -> /skill-promote <skill>：显式发布 champion 到 active SKILL.md
  -> 记录 provenance、proposal 状态、评测产物和版本快照
```

每轮 `skill_trace` 分开记录 `retrieved`（检索命中）、`surfaced`（摘要已注入）和
`invoked`（完整 Skill 已真实展开）。只有 `invoked` 是确定性使用证据；检索 Top 1
只用于提示和审计，不能直接成为 merge 目标。裁判模型输出的 `inferred_used` 仅表示
最终回答看起来采用了某个流程，不参与自动归档或 champion 晋级。

归因与候选抽取由一次隔离的辅助模型请求联合完成：缺工具、权限、网络、沙箱或运行时
能力的问题会停在 `capability_limit`，证据不足或只是切换话题会停在
`evaluation_noise`，不会通过修改提示词“伪修复”。`/skill-eval` 生成候选后，先在
`mutate_dev` 上与 current active 的同批样本比较，再在 `promotion_test` 上逐
`sample_id + rule_id` 计算 regression；默认要求零回归和零硬规则回归，最后才与历史
champion 比较。评测通过只会更新隔离 champion，只有显式 `/skill-promote` 才改变 active。
所有归因、proposal 生命周期和回归明细都会进入 provenance / run artifacts。

核心文件：

```text
agents/agent.py
agents/online_skill_evolution.py
agents/skills.py
agents/skill_evolution.py
```

审计产物：

```text
.bear/skill-evolution/usage.jsonl
.bear/skill-evolution/online_provenance.jsonl
.bear/skill-evolution/online_skill_provenance.json
.bear/skill-evolution/skill_usage_stats.json
.bear/skill-evolution/proposals.json
.bear/skill-evolution/proposals/<proposal-id>/
.bear/skill-evolution/online-eval/champions/
.bear/skill-evolution/history/
.bear/skill-evolution/pruned/
```

### 5. 手动触发当前窗口抽取

如果你想立即把当前对话窗口送入自进化链路，可以在 REPL 中执行：

```text
/extract_now 这是一个可复用的写作规则
```

显式抽取和后台抽取都只暂存 proposal，不会直接改变 Agent 当前加载的 Skill。

### 6. 手动创建 Skill

```text
/skill-create government-report-writing | 政府报告写作规范 | 用户要求写政府报告、工作汇报、调研报告、公文材料时使用 | 默认按标题、背景、问题、举措、成效、下一步计划组织内容；语言正式克制；信息不足时先生成可用初稿，并在末尾列出待补充信息。
```

### 7. 手动演化 Skill

```text
/skill-evolve government-report-writing 以后政府报告类任务不要使用口语化表达，优先使用正式、稳健、可汇报的句式。
```

### 8. 评测并发布在线候选

```text
/skill-proposals
/skill-eval
/skill-promote government-report-writing
```

### 9. 查看 Skills 和统计

```text
/skills
/skill-stats
```

## 常用命令

### CLI 参数

| 参数 | 功能 |
|------|------|
| `--model`, `-m` | 指定模型，覆盖 `.env` 中的 `MODEL` |
| `--api-base` | 覆盖 API base URL |
| `--plan` | 只读规划模式 |
| `--accept-edits` | 自动允许普通编辑类操作；在线 Skill 仍只生成 proposal |
| `--yolo`, `-y` | 跳过确认 |
| `--dont-ask` | 自动拒绝需要确认的操作，适合 CI |
| `--resume` | 恢复最近会话 |
| `--max-cost` | 费用上限 |
| `--max-turns` | 最大 agentic turns |

### REPL 命令

| 命令 | 功能 |
|------|------|
| `/clear` | 清空对话历史 |
| `/plan` | 切换 Plan Mode |
| `/cost` | 显示 token 和费用估算 |
| `/compact` | 手动压缩上下文 |
| `/memory` | 列出长期记忆 |
| `/skills` | 列出可用 Skills |
| `/skill-stats` | 查看 Skill 使用和演化统计 |
| `/skill-proposals` | 查看待评测、已评测和 champion proposals |
| `/skill-eval` | 隔离试跑候选并执行晋级门禁 |
| `/skill-promote <skill>` | 显式把已过门禁的 champion 发布为 active Skill |
| `/extract_now [hint]` | 抽取当前 pending window |
| `/skill-feedback <skill> <rating> [note]` | 记录 Skill 反馈 |
| `/skill-evolve <skill> <lesson>` | 手动演化 Skill |
| `/skill-create <name> \| <description> \| <when-to-use> \| <instructions>` | 手动创建 Skill |

## Skills 是什么

Skill 是一个可复用能力说明文件，通常是一个带 frontmatter 的 `SKILL.md`。它保存的不是某次任务的具体内容，而是未来同类任务可复用的方法、规范、偏好或流程。

Skill 路径：

```text
用户级：~/.bear/skills/<skill_name>/SKILL.md
项目级：<project>/.bear/skills/<skill_name>/SKILL.md
```

示例：

```markdown
---
name: code_review
description: Review code changes with a bug-risk-first mindset.
when-to-use: When the user asks for code review.
user-invocable: true
context: inline
---

# Workflow

1. Read the relevant code first.
2. Lead with bugs, regressions, security risks, and missing tests.
3. Cite file paths and line numbers when possible.
4. Keep summary secondary to findings.
```

Skill 和 Memory 的区别：

| 类型 | 保存内容 |
|------|----------|
| Memory | 用户偏好、项目事实、历史决策、参考资料 |
| Skill | 可复用任务流程、输出规范、领域方法 |

## MCP 支持

Bear Code 支持 MCP 外部工具扩展。MCP Server 可以通过 stdio JSON-RPC 暴露工具，Bear Code 会将其包装为 Agent 可调用工具。

配置来源：

```text
~/.bear/settings.json
<project>/.bear/settings.json
<project>/.mcp.json
```

配置示例：

```json
{
  "mcpServers": {
    "example": {
      "command": "python",
      "args": ["server.py"],
      "env": {}
    }
  }
}
```

工具命名规则：

```text
mcp__<serverName>__<toolName>
```

## Docker 运行

这是把完整 Harness 放进一个外层容器的旧兼容方式，不是推荐的独立执行面。由于容器内
不会挂 Docker Socket（也不应该挂），启动参数需显式使用 `--unsafe-local`；此时外层容器
本身承担隔离。推荐开发方式仍是宿主 Harness + 前文的 `Dockerfile.sandbox`。

构建镜像：

```bash
docker build -t bear-code .
```

启动交互式会话：

```bash
docker run --rm -it \
  --env-file .env \
  -v "$PWD:/workspace" \
  -v bear-code-sessions:/root/.bear-code \
  -v bear-code-memory:/root/.BearCode \
  bear-code --unsafe-local
```

允许自动抽取 Skill proposals（不会自动发布 active）：

```bash
docker run --rm -it \
  --env-file .env \
  -e BEAR_AUTO_SKILL_EVOLUTION=1 \
  -e BEAR_AUTO_SKILL_TARGET=project \
  -v "$PWD:/workspace" \
  -v bear-code-sessions:/root/.bear-code \
  -v bear-code-memory:/root/.BearCode \
  bear-code --unsafe-local
```

## 重要数据路径

| 数据 | 路径 |
|------|------|
| 项目级 Skills | `.bear/skills/<skill_name>/SKILL.md` |
| 用户级 Skills | `~/.bear/skills/<skill_name>/SKILL.md` |
| Skills 自进化审计 | `.bear/skill-evolution/` |
| 长期记忆 | `~/.BearCode/projects/<project_hash>/memory/` |
| 会话历史 | `~/.bear-code/sessions/` |
| 大工具结果 | `~/.bear-code/tool-results/` |
| Plan Mode 计划 | `~/.bear/plans/` |

## 文档入口

更完整的学习和展示材料在 `wiki/`：

| 文档 | 内容 |
|------|------|
| [学习说明](wiki/从0到1学习了解项目.md) | 面向学习者的完整项目说明 |
| [架构设计](wiki/架构设计.md) | 系统分层、主链路、模块边界和数据流 |
| [核心源码阅读指南](wiki/核心源码阅读指南.md) | 按源码顺序学习 Agent Loop、工具、Skills、Memory、MCP 和自进化 |
| [技术亮点](wiki/技术亮点.md) | 技术亮点和核心代码讲解 |
| [Sandbox 架构与演示](wiki/Sandbox架构与面试演示.md) | 隔离边界、威胁模型、演示脚本和面试回答 |
| [Sandbox 评测报告](wiki/Sandbox评测报告.md) | 自动化攻击用例、性能指标状态与当前限制 |
| [Skills 自进化逻辑](wiki/Skills自进化逻辑与实现思路.md) | 自进化设计和实现取舍 |
| [简历包装](wiki/简历包装.md) | 简历 bullet、面试表达和项目包装 |

## 适合如何使用

Bear Code 适合：

- 学习 Claude Code 类工具的 Agent Loop 和工具调用机制。
- 学习如何做本地文件编辑型 Agent 的权限控制。
- 学习 Skills、Memory、MCP、子 Agent 如何接入同一个 Runtime。
- 扩展成个人 Coding Agent。
- 扩展成带长期记忆和可复用经验沉淀的领域 Agent。

如果只看一个核心点：Bear Code 是一个会把稳定用户反馈沉淀成 Skills 的 **自进化 Harness Agent**。
