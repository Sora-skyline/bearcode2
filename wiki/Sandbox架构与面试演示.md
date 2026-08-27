# BearCode 轻量 Sandbox：架构、边界与面试演示

## 一句话结论

BearCode 把“用户是否同意”与“进程技术上能访问什么”拆成两层：宿主机保留 Agent
Loop、模型密钥、Approval 和 Trace；获准的 Shell、测试构建及可信 MCP stdio 进程进入
一个会话级持久 Docker Sandbox。它是面向单用户 Demo 的爆炸半径控制，不宣称多租户
强隔离。

## 先补 Docker 的最小知识

如果完全没用过 Docker，先只记住三个词。

### 1. Image：装好工具的只读模板

Image 可以理解成“环境安装包”或“系统模板”。BearCode 使用
`Dockerfile.sandbox` 构建 `bear-code-sandbox:latest`，里面预装 Python、Bash、Git、
ripgrep、Node.js 等命令行工具。

```text
Dockerfile.sandbox  --docker build-->  bear-code-sandbox:latest
     构建说明                              Image
```

Image 本身不是正在运行的进程。它更像一个模具，同一份 Image 可以创建多个相互独立的
Container。

### 2. Container：Image 的一次运行实例

Container 是从 Image 创建出来的运行环境。它有自己的进程、文件系统视图和网络配置。
关闭或删除 Container 不会删除 Image。

它也不是完整虚拟机：虚拟机通常带一套独立内核，Container 共享宿主机内核，所以启动更
快、资源更轻，但隔离强度通常弱于 microVM。BearCode 选择 Docker，是因为本项目是本地
Demo，需要一个容易理解、容易演示的真实边界。

### 3. Bind mount：给 Container 开一扇指定的门

Container 默认看不到宿主项目。BearCode 显式建立如下映射：

```text
宿主机 /Users/.../BearCode2  <---- bind mount ---->  Container /workspace
```

因此容器里的 `pytest` 能读取项目代码，测试生成的文件也会反映回宿主 workspace。但没有
映射的宿主 Home、`.ssh` 和 Docker Socket，容器看不到。

这里有个容易误解的重要边界：如果 `.env` 就放在项目 workspace 中，它也会随整个目录被
挂进去，容器可以读取。`.dockerignore` 只影响 `docker build` 的构建上下文，不会过滤
运行时 bind mount。BearCode 不会把宿主进程的 API Key 环境变量自动注入容器，但 V1 也
没有逐文件排除 workspace 内 Secret。因此建议把模型 Key 放在宿主环境变量，或放在
workspace 外、仍能被 `find_dotenv()` 找到的父目录 `.env` 中。

这也是 Sandbox 的核心思路：不是试图识别每一条坏命令，而是先把命令能看到的世界缩小。

## 用一个具体任务理解完整过程

假设用户让 BearCode“运行 pytest”。完整过程如下：

```text
1. Agent Loop 在宿主机调用模型
2. 模型提出 run_shell({"command": "pytest"})
3. Approval 层判断 allow / deny / confirm
4. 第一次 Shell 调用时，SandboxSession 检查本地 Image
5. 用 docker create 创建当前 Session 的 Container
6. 用 docker start 启动 Container
7. 用 docker exec 在 Container 的 /workspace 执行 pytest
8. 收集 stdout、stderr、退出码、耗时和截断状态
9. 把结果作为 tool result 交回宿主机上的模型
10. Session 退出时用 docker rm -f 删除 Container
```

模型客户端和宿主环境中的 API Key 留在宿主机，BearCode 不会把它们作为容器环境变量传入。
但 workspace 中的所有文件都会被挂载，所以项目内 `.env` 不属于这个保证；这是 V1 需要
通过使用约定或未来 secret masking 处理的边界。

对应关系是：

| BearCode 对象 | 初学者理解 | Docker 动作 |
| --- | --- | --- |
| `SandboxConfig` | 安全设置清单 | 决定 Image、网络和资源上限 |
| `SandboxSession.start()` | 创建并打开临时工作间 | `docker image inspect/create/start` |
| `ExecRequest` | 要在工作间执行的任务单 | command、cwd、timeout、显式 env |
| `SandboxSession.exec()` | 派人进去执行任务 | `docker exec ... sh -lc` |
| `ExecResult` | 标准化执行回执 | stdout、stderr、exit code、耗时等 |
| `SandboxSession.close()` | 清空并关闭临时工作间 | `docker rm -f` |

## 为什么一个 Session 复用一个 Container

BearCode 没有为每条命令新建容器，而是主 Agent Session 第一次需要 Shell/MCP 时懒创建
一个，后续命令继续用 `docker exec` 进入它。

这样做有两个直观好处：

- 避免每条命令都付一次 Docker 冷启动成本；
- `/tmp` 中的临时文件和容器内仍在运行的服务可以被后续命令看到。

但每次 `docker exec` 仍会启动一个新的 Shell，所以第一条命令里的 `export FOO=bar` 不会
自动传给下一条命令。需要长期保存的内容应该写进 workspace，依赖则应预装进 Image。

workspace 是宿主 bind mount，所以删除 Container 后代码修改仍保留；容器自己的临时层和
`/tmp` 会被清理。这正好符合 Coding Agent 的需求：保留代码成果，清理不受控进程。

## 逐项看懂 BearCode 的安全设置

| 设置 | 白话解释 | 主要防什么 |
| --- | --- | --- |
| `--network none` | 容器没有外网网卡 | 恶意下载、数据外传、任意联网 |
| `--user <uid>:<gid>` | 命令不是 root 身份 | 降低容器内提权后的破坏能力 |
| `--read-only` | Image 自带的系统目录不可写 | 修改 `/bin`、`/etc` 或持久安装恶意程序 |
| `--tmpfs /tmp` | `/tmp` 是易失临时空间 | 给测试临时文件可写位置，重启即清理 |
| `--cap-drop ALL` | 移除 Linux 额外特权 | 挂载设备、修改网络等高权限系统操作 |
| `no-new-privileges` | 子进程不能获得更高权限 | 通过 setuid 等方式升级权限 |
| `--memory 1g` | 最多使用 1 GiB 内存 | 内存耗尽拖垮宿主机 |
| `--cpus 1` | 最多使用约 1 个 CPU | 死循环长期占满宿主 CPU |
| `--pids-limit 128` | 最多约 128 个进程 | fork bomb 无限创建进程 |
| 命令最长 120 秒 | 超时就停止并重启容器 | 卡死或后台进程残留 |
| 输出最多 1 MiB | 多余输出继续排空但不保存 | 无限日志撑爆 Harness 内存和模型上下文 |
| 只挂载 `/workspace` | 只给项目目录开门 | 读取宿主 Home、Docker Socket和其他项目；但 workspace 内 Secret 仍可见 |

这里的 `rootfs read-only` 不等于“项目只读”。Container 的系统层只读，但 `/workspace`
是单独的读写挂载，所以 Agent 仍能修改当前项目。这是有意设计：Coding Agent 必须能改
代码，V1 保护的是 workspace 外的宿主资源。

## 配置文件怎么读

默认配置写成 JSON 是：

```json
{
  "sandbox": {
    "backend": "docker",
    "image": "bear-code-sandbox:latest",
    "network": "none",
    "memory": "1g",
    "cpus": 1,
    "pidsLimit": 128,
    "commandTimeoutSeconds": 120,
    "maxOutputBytes": 1048576
  }
}
```

用户级 `~/.bear/settings.json` 先加载，项目级 `.bear/settings.json` 后加载并覆盖同名字段。
新手阶段建议保持默认值，不要为了让 `npm install` 成功就直接打开网络。确实需要联网时，
先确认项目和依赖可信，再在 Session 启动前显式修改网络配置。

`run_shell` 工具自身默认请求 30 秒超时，而 Sandbox 配置的 120 秒是不可超过的上限。也
就是说，模型不能通过传入一个特别大的 timeout 取消全局保护。

## 文件工具为什么不全放进 Container

BearCode 当前采用两条执行通道：

```text
run_shell / 测试 / 构建 / MCP  -> Docker Sandbox
read_file / write_file / edit_file -> 宿主专用文件工具
```

文件工具留在宿主侧，是为了保持精确编辑、编辑前读取和 `mtime` 并发保护的现有实现。它们
不是任意宿主文件 API：每个路径都会 canonicalize，并检查最终路径只能落在 workspace 或
明确 Runtime 目录中。`..`、绝对路径越界和符号链接逃逸都会被拒绝。

这种设计叫“窄接口”：Shell 是通用代码执行，所以放进强一些的容器边界；文件读写能力
较单一，就在宿主侧用确定性的路径校验收口。

## Approval 和 Sandbox 到底有什么区别

可以用门卫和围墙来理解：

- Approval 是门卫：这次操作是否符合用户意图，要不要询问；
- Sandbox 是围墙：操作获准后，进程最多能走到哪里。

`--yolo`/`bypassPermissions` 相当于告诉门卫“不用每次问我”，不会拆掉围墙。因此 Shell
依旧进入 Docker。`--unsafe-local` 才是显式关闭围墙、恢复宿主执行的兼容开关，风险完全
不同，不应混为一谈。

## 超时为什么要重启整个 Container

只杀掉 `docker exec` 命令不一定够。Shell 可以启动孙进程或把程序放到后台，父进程退出
后它们仍可能留在容器里。

BearCode 在超时或取消时重启整个 Session Container，相当于把执行面的所有进程一次清空。
workspace 修改不会丢，因为它在宿主 bind mount 上。代价是同容器里的 MCP Server 也会被
杀掉，V1 尚未实现自动重连，这是面试中应该主动说明的边界。

## 新手第一次怎么运行

1. 先安装并启动 Docker Desktop（macOS/Windows）或 Docker Engine（Linux）。
2. 确认 Docker daemon 可用：

```bash
docker version
```

3. 在项目根目录构建 Sandbox Image：

```bash
docker build -f Dockerfile.sandbox -t bear-code-sandbox:latest .
```

该 Image 会预装 `requirements.txt` 和 pytest。项目源码无需复制进 Image，运行时会整体挂载
到 `/workspace`；依赖文件变化后需要重新构建 Image。

4. 运行 BearCode。只有第一次使用 Shell 或可信 MCP 时才会创建 Container：

```bash
python3 -m agents.main
```

5. 用演示脚本验证真实边界：

```bash
PYTHONPATH=. .venv/bin/python scripts/sandbox_demo.py
```

常见错误：

- `Docker CLI is unavailable`：没有安装 Docker，或 `docker` 不在 PATH；
- `sandbox image ... is unavailable`：还没有执行第 3 步构建 Image；
- 容器内 `curl`、`npm install`、`npx -y` 失败：默认断网正在生效；
- 项目内 `.env` 仍可被容器读取：bind mount 不受 `.dockerignore` 过滤；把模型 Key 放到
  宿主环境或 workspace 外的父级 `.env`；
- 系统目录无法写入：`--read-only` 正在生效，应把项目产物写到 `/workspace`，临时文件写
  到 `/tmp`；
- 为了临时跑通而使用 `--unsafe-local`：命令会直接获得宿主执行能力，只适合作为明确知道
  风险的兼容模式，不能再宣称处于 Docker Sandbox。

## 升级前是什么

升级前只有应用层控制：`check_permission()` 用权限模式、配置规则和危险命令正则返回
allow/deny/confirm，但获准后 `run_shell` 直接调用宿主 `subprocess`，MCP 也继承完整
`os.environ` 在宿主启动。文件路径解析还会尝试把越界绝对路径映射回当前目录。

这套设计能减少误操作，却不是安全边界：正则不可能枚举所有危险命令；`--yolo` 会绕过
审批；一旦工具获准，进程仍可读取 Home、环境变量、Docker Socket 和 workspace 外文件。
原有 `Dockerfile` 只是把整个 Harness 打进容器，也会把模型控制面和执行面放在一起，不是
独立 Action Runtime。

## 升级后怎么跑

```text
Host control plane
  Agent Loop / model client / API key / Approval / EventJournal
                         |
                         | docker exec / stdio
                         v
Docker execution plane (one persistent container per main Session)
  /workspace rw + /tmp tmpfs
  Shell / test / build / trusted MCP server
```

关键调用链：

```text
model tool call
  -> check_permission()                 # Approval：allow / deny / confirm
  -> tools.execute_tool()
  -> SandboxSession.start()             # 首次 Shell/MCP 时懒创建
  -> SandboxSession.exec(ExecRequest)
  -> docker exec ... sh -lc <command>
  -> ExecResult                         # 固定退出码、耗时、超时、截断等字段
  -> tool result 回写模型
```

主 Agent 拥有 Sandbox，子 Agent 共享它，权限模式继承父级，工具能力取父工具集合与自身
白名单的交集。Session 退出时先断开 MCP，再 `docker rm -f` 删除容器。

## V1 的确定性边界

容器启动参数包含：

- workspace 只以 `/workspace` 读写挂载；不挂 Home、Docker Socket或其他宿主目录；
- 数字非 root UID、只读 rootfs、`tmpfs /tmp`；
- `cap-drop=ALL` 和 `no-new-privileges`；
- 默认 `network=none`；
- 1 CPU、1 GiB 内存、128 PID；
- 单命令最大 120 秒、stdout/stderr 合计最多保留 1 MiB；
- 容器只获得 `HOME/LANG/LC_ALL/CI` 与请求显式声明的环境变量，不复制宿主 API Key 环境变量；workspace 内 `.env` 仍随挂载可见。

命令超时或 Agent 取消时，不只杀 `docker exec` 客户端，而是重启整个 Session 容器，确保
命令派生出的后台进程一起消失；workspace 是 bind mount，所以代码修改保留。

宿主文件工具走另一条窄接口：路径先 canonicalize，只允许 workspace、当前项目 Memory、
plan 目录和大结果目录。`/etc/passwd`、`../other-project`、符号链接逃逸以及外部父目录都会
拒绝。编辑前读取和 `mtime` 一致性保护继续保留。

## 为什么不是只做危险命令正则

正则属于风险提示，适合决定“是否弹确认”，不适合承担隔离。例如 `python -c`、编译后的
二进制、包管理器脚本都可以间接执行任意系统调用，命令文本并不会出现 `rm` 或 `sudo`。
Docker 的 mount、namespace、capability 和资源限制是在进程执行层收口，因此绕过提示也
不能访问未映射的宿主资源。

这个分层参考现代 Coding Harness 的共同方向：Codex 强调 sandbox 与 approval policy
分离；Claude Code 默认限制网络并把高风险能力交给显式授权；OpenHands 把 Agent 控制面
与 Action Runtime 分开。BearCode 只吸收最适合 Demo 的交集，没有复制它们的远程调度、
快照、原生 OS Sandbox 或 microVM。

## MCP 为什么要单独处理

项目里的 `.mcp.json` 本身是可执行配置。现在主 Agent 第一次读取项目 MCP 时先显示 Server
名称并要求信任；拒绝后只加载用户级配置。获信任的 Server 通过 `spawn_stdio()` 进入同一
Sandbox，配置中显式写出的 `env` 可以传入，但不会继承 Harness 的 `os.environ`。

这同时处理了两类风险：不可信仓库不能在“打开项目”时静默启动任意命令；可信 Server
即使有漏洞，默认也只能看到 workspace 和断网容器环境。

默认断网也意味着依赖 `npx -y` 临时下载的 MCP 不会直接工作：应把 Server 可执行文件和
依赖预装进 Sandbox 镜像；只有确实需要联网的可信项目，才在启动 Session 前显式把网络
改为受信任模式。Plan Mode 不启动 MCP 进程，退出 Plan 后才做首次信任和懒加载。

## Trace 怎么讲

Runtime 发布：

- `sandbox.created` / `sandbox.destroyed`；
- `sandbox.exec.started` / `completed` / `failed`；
- `sandbox.violation`；
- `sandbox.restarted`。

Sandbox 事件记录 execution ID、命令首词、参数个数、命令哈希、耗时、退出码和资源限制，
不记录完整 Shell 文本。通用 tool 事件里的 Shell 入参也被替换为摘要，避免 Trace 成为新的
Secret 泄露面。

Web 顶栏同时显示 Sandbox 的真实快照状态：`not-started`、`running`、`stopped`、`closed`
或 `unsafe-local`。只有实际创建/启动过执行面才发布 `sandbox.destroyed`；从未运行 Shell/MCP
的会话关闭时不发布该事件。状态提示会列出 workspace 根目录中容器可见的常见 Secret 文件名，
但不读取或记录其内容。

## 三分钟演示

```bash
docker build -f Dockerfile.sandbox -t bear-code-sandbox:latest .
PYTHONPATH=. .venv/bin/python scripts/sandbox_demo.py
```

脚本依次验证非 root、workspace 可写、无 Docker Socket、无宿主 Home、无继承 API Key 环境变量、
默认断网、warm command 延迟，以及超时后容器重启和进程树清理。结果保存在
`.bear/sandbox-evaluation.json`，可直接展示通过率、cold start 和 warm P95。

测试分两层：普通 `pytest` 使用 fake Docker 验证参数和 fail-closed 逻辑；设置
`BEAR_RUN_DOCKER_TESTS=1` 后执行真实容器隔离测试。

## 面试回答模板

> 早期版本只有 permission check，本质是应用层审批，不是安全隔离。我后来参考现代
> Coding Harness，把控制面和执行面拆开：模型密钥、Agent Loop、审批和 Trace 留在宿主，
> Shell 与可信 MCP 放进每会话一个持久 Docker 容器。`--yolo` 只能跳过审批，不能跳出
> Sandbox。容器默认断网、非 root、只读 rootfs、无 capability，并限制 CPU、内存、PID、
> 超时和输出；超时直接重启容器清理进程树。文件工具仍是宿主窄接口，但做 canonical path
> 和符号链接逃逸检查。它不是生产多租户方案，目标是用很小的实现建立真实执行边界，并用
> 攻击用例和延迟报告证明边界有效。

## 明确限制与下一步

- workspace 是读写 bind mount，Agent 仍能修改项目；Git/checkpoint 是恢复手段，不是隔离；
- workspace 内的 `.env` 等 Secret 文件也会被挂载；V1 未实现逐文件 secret masking；
- Docker daemon 本身是信任根，V1 不防 Docker/内核逃逸，也没有多租户保证；
- 默认断网是二元开关，没有域名代理、凭据 broker 或细粒度 egress policy；
- 容器重启会终止同 Session 的 MCP 进程，V1 不做自动重连；
- 没有 copy-on-write、快照、远程 Provider、Seatbelt/bubblewrap 或 Firecracker。

如果继续升级，优先做 MCP 自动重连和 workspace checkpoint；只有进入多用户服务场景时，
才值得引入远程 sandbox provider 或 microVM，而不是让 Demo 过度工程化。
