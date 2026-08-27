# BearCode Sandbox 评测报告

评测日期：2026-08-27

## 当前结果

| 层级 | 结果 | 说明 |
| --- | --- | --- |
| 全量 Python 测试 | 53 passed | 包含路径越界、`..`、符号链接逃逸、Plan Mode、bypassPermissions、fail-closed、Docker hardening 参数、生命周期状态、输出上限、超时重启、MCP 信任、环境变量与事件摘要脱敏测试 |
| Docker 集成测试 | 1 skipped | 当前验证机器没有 Docker CLI；没有伪造容器结果 |
| Web production build | passed | Sandbox 状态类型、SSE 实时更新与顶栏状态组件通过 TypeScript 和 Vite 构建 |
| Ruff E9/F | passed | 本次涉及的实现、测试和演示脚本（全仓另有 2 个与本次无关的既存 F401） |
| Python 编译 | passed | Sandbox、Agent、tools、MCP、CLI、Web 与演示脚本 |
| `git diff --check` | passed | 无空白错误 |

## 已自动验证的安全用例

- `/etc/passwd`、`../other-project` 和 workspace 内指向外部的符号链接被文件工具拒绝；
- `bypassPermissions` 不能绕过路径边界；
- Plan Mode 禁止 Shell 和 MCP；
- 未注入 `SandboxSession` 时 Shell fail closed；
- Docker CLI 缺失时不会回退宿主执行；
- 容器创建参数包含默认断网、只读 rootfs、非 root UID、无 capability、
  `no-new-privileges`、CPU/内存/PID、`tmpfs` 与唯一 workspace bind mount；
- stdout/stderr 合计超过上限时返回 `output_truncated`；
- 命令超时返回结构化 exit 124，并产生 `sandbox.restarted`；
- 项目 MCP 只有 `include_project=True`（Agent 已取得信任）才会加载；
- MCP 启动请求只携带配置显式声明的环境变量；
- fork Skill 子 Agent 继承父级权限、确认回调和 Sandbox Session。
- 从未创建执行面的 Session 关闭时不发布虚假的 `sandbox.destroyed`；
- Web 摘要区分 `not-started/running/stopped/closed/unsafe-local`；
- 状态快照只暴露 workspace 常见 Secret 文件名，不读取文件内容；
- Sandbox Image 声明安装项目 `requirements.txt` 和 pytest。

## 待有 Docker 环境后生成的指标

当前机器执行 `docker version` 返回 `command not found`，因此以下数字保持未测，不用假数据
包装面试：

| 指标 | 当前值 |
| --- | --- |
| Docker cold start | 未测 |
| warm command P95 | 未测 |
| 容器内默认断网（DNS + HTTPS） | 集成测试待跑 |
| 无 Home / Docker Socket / 继承的 API Key 环境变量 | 集成测试待跑 |
| 超时后无残留进程 | 集成测试待跑 |

在安装 Docker 并构建镜像后运行：

```bash
docker build -f Dockerfile.sandbox -t bear-code-sandbox:latest .
BEAR_RUN_DOCKER_TESTS=1 PYTHONPATH=. .venv/bin/pytest tests/test_sandbox.py -q
PYTHONPATH=. .venv/bin/python scripts/sandbox_demo.py
```

第二条命令会执行真实隔离测试；第三条会把 cold start、warm P95、攻击用例通过率和超时
清理结果写入 `.bear/sandbox-evaluation.json`。

## 当前安全声明

这是单用户本地 Demo 的 workspace 外宿主保护，不是多租户安全认证。workspace 本身可写，
其中的 `.env` 等 Secret 文件也对容器可见；
Docker daemon 和宿主内核仍是信任根；没有 copy-on-write、快照、远程 Provider、microVM、
细粒度网络代理或 MCP 自动重连。
