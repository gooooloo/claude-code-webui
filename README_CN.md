# claude-code-webui

一个 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 的 Web UI，用浏览器界面替代默认的终端交互。在手机、平板或局域网内的任意浏览器上审批工具调用、提交 prompt、上传图片、管理会话。

## 架构

**基于 transcript 驱动，跨平台（Linux/macOS 用 Tmux，Windows 用 Windows Terminal），3 个 Python hook。**

- 所有会话状态从 Claude Code 的 transcript JSONL 文件推导，服务端不维护状态机
- Prompt 通过平台原生方式投递（Linux/macOS 用 `tmux send-keys`，Windows 用 `WriteConsoleInput`），不依赖文件轮询
- 只有 3 个 hook 脚本（纯 Python，不依赖 jq、curl 等外部工具）

### 工作流程

**权限审批流程：**
```
Claude Code                          Web 浏览器
      |                                   |
      |-- PermissionRequest hook ------>  |
      |   (hook-permission-request.py)         |
      |   写入 .request.json 到           |
      |   /tmp/claude-webui/              |
      |                                   |
      |          server.py                |
      |          读取 transcript JSONL    |
      |          推导会话状态             |
      |          提供 dashboard  ------> |  用户看到请求
      |                                   |  点击 Allow / Deny
      |          写入 .response.json <----|
      |                                   |
      |<-- hook 读取响应                  |
      |   (allow 或 deny)                 |
```

**Prompt 提交流程：**
```
Claude Code                          Web 浏览器
      |                                   |
      |          server.py                |
      |          显示会话 dashboard       |
      |          带 prompt 输入框  ----> |  用户输入 prompt
      |                                   |  点击 Send
      |                                   |
      |<-- prompt 投递                    |
      |    tmux send-keys (Linux/macOS)   |
      |    或 WriteConsoleInput (Windows) |
```

### 组件

1. **`server.py`** — Python HTTP 服务器（端口 19836）。会话注册、transcript 解析、多会话 dashboard。
2. **`hook-permission-request.py`** — `PermissionRequest` hook。自动放行检查，写入 `.request.json`，轮询 `.response.json`。
3. **`hook-session-start.py`** — `SessionStart` hook。向服务器注册会话（transcript 路径、tmux/console 信息、cwd）。
4. **`hook-session-end.py`** — `SessionEnd` hook。注销会话，清理文件。
5. **`platform_utils.py`** — 跨平台工具。OS 检测、临时目录路径、进程树遍历。
6. **`win_send_keys.py`** — Windows console 输入辅助。通过 `WriteConsoleInputW` 注入键盘输入。
7. **`channel_feishu.py`** — 可选的飞书通知渠道。
8. **`install.sh`** / **`uninstall.sh`** — Hook 安装脚本（Linux/macOS）。**`install.ps1`** / **`uninstall.ps1`** — Windows 版（PowerShell）。

## 功能

- **多会话 dashboard** — 一览所有活跃的 Claude Code 会话
- **Transcript 驱动的状态** — idle、working、needs approval、question、plan review
- **Allow / Deny** — 逐个审批或拒绝工具调用
- **Always Allow** — 审批并将模式写入 `settings.local.json`
- **Allow Path** — 对 Write/Edit 工具，放行整个目录下的操作
- **Split Always Allow** — 复合 Bash 命令拆分为单独的模式
- **Session 级自动放行** — 对单个会话自动审批特定工具
- **Prompt 提交** — 在 dashboard 中发送后续 prompt（Linux/macOS 通过 tmux，Windows 通过 console 输入）
- **AskUserQuestion 支持** — 用选项或自定义文本回答 Claude 的提问
- **Plan review** — 审批、拒绝或反馈计划
- **图片上传** — 在 prompt 区域附加图片
- **Machines** — 通过 DevTunnels 在一个页面集中监控多台机器上的同一服务
- **飞书集成** — 可选的通知渠道，支持手机审批
- **优雅降级** — 服务器离线时 hook 自动放行
- **自动清理** — 僵尸会话（已死进程）自动清理
- **暗色主题，移动端适配**

## 依赖

**Linux/macOS：**
- Python 3
- tmux（prompt 投递必需）
- Bash、`jq`（安装/卸载脚本）

**Windows：**
- Python 3
- PowerShell 5.1+（安装/卸载脚本）

## 安装

1. 克隆仓库：
   ```bash
   git clone https://github.com/gooooloo/claude-code-webui.git
   ```

2. 启动服务器：
   ```bash
   /path/to/claude-code-webui/server.py
   ```

3. 安装 hook：

   **Linux/macOS：**
   ```bash
   # 仅当前项目（在项目目录下执行）：
   /path/to/claude-code-webui/install.sh --project

   # 或全局安装（所有项目）：
   /path/to/claude-code-webui/install.sh --global

   # 或两者都装：
   /path/to/claude-code-webui/install.sh --all
   ```

   **Windows (PowerShell)：**
   ```powershell
   # 仅当前项目：
   \path\to\claude-code-webui\install.ps1 -Scope Project

   # 或全局安装：
   \path\to\claude-code-webui\install.ps1 -Scope Global

   # 或两者都装：
   \path\to\claude-code-webui\install.ps1 -Scope All
   ```

4. 如果 Claude Code 已在运行，**需要重启** — hook 在启动时加载。

5. 在浏览器打开 `http://localhost:19836`（或用局域网 IP 从手机/平板访问）。

6. 运行 Claude Code — Linux/macOS 在 **tmux 内** 运行，Windows 在 **Windows Terminal** 内运行。Dashboard 会自动显示你的会话。

> **Windows 限制：** Prompt 投递使用 `AttachConsole`/`WriteConsoleInputW`，不支持 Windows Terminal 的 split pane。如果一个窗口被拆分成多个 pane，prompt 可能发送到错误的 pane 或直接失败。请每个窗口只运行一个 Claude Code 会话（多窗口没问题）。

## Machines（多机监控）

Machines 让你在一个页面上访问多台机器上运行的同一个 WebUI 服务。远程服务器向中心 hub 自动注册，Machines 页面自动发现所有机器并提供快捷打开链接。

### DevTunnels 配置（每台机器一次性操作）

[Microsoft DevTunnels](https://learn.microsoft.com/en-us/azure/developer/dev-tunnels/overview) 为每台机器提供公网 HTTPS URL，无需端口转发。安装 CLI：

```bash
# Windows (winget)
winget install Microsoft.devtunnel

# Linux
curl -sL https://aka.ms/DevTunnelCliInstall | bash

# macOS
brew install --cask devtunnel
```

登录并创建**命名 tunnel**（持久化 — 重启不丢失，只有 `devtunnel delete` 才会删除）：

```powershell
devtunnel login
devtunnel create $env:COMPUTERNAME
devtunnel port create $env:COMPUTERNAME -p 19836
```

每次需要激活 tunnel 时，只需 host：

```powershell
devtunnel host $env:COMPUTERNAME
```

公网 URL 格式为 `https://<random-id>-19836.asse.devtunnels.ms`。`<random-id>` 在创建时分配，只要不 delete 就不会变（自定义 tunnel ID 只是方便命令引用，不会出现在 URL 中）。可以通过 `devtunnel list` 查看。

> **提示：** 同一个 tunnel 可以暴露多个端口：
> ```powershell
> devtunnel port create $env:COMPUTERNAME -p 8080
> devtunnel port create $env:COMPUTERNAME -p 3000
> ```
> 每个端口有独立的 URL：`https://<random-id>-8080.asse.devtunnels.ms` 等。

### Machines 配置

1. **选一台机器作为 hub**（你在浏览器上打开的那台）。在不同窗口分别运行：
   ```powershell
   # 窗口 1：启动服务器
   python3 server.py --name $env:COMPUTERNAME
   ```
   ```powershell
   # 窗口 2：启动 tunnel
   devtunnel host $env:COMPUTERNAME
   ```

2. **启动远程服务器**，用 `--hub-tunnel-id` 指向 hub 的 random ID。在不同窗口分别运行：
   ```powershell
   # 窗口 1：启动服务器（手动指定 tunnel ID，通过 `devtunnel list` 查看）
   python3 server.py --name $env:COMPUTERNAME --tunnel-id 1c6j6jlh --hub-tunnel-id abc123

   # 或自动检测 tunnel ID
   python3 server.py --name $env:COMPUTERNAME --detect-tunnel --hub-tunnel-id abc123
   ```
   ```powershell
   # 窗口 2：启动 tunnel
   devtunnel host $env:COMPUTERNAME
   ```

3. 打开 `https://<hub-tunnel-id>-19836.asse.devtunnels.ms/multiview` — 所有注册的机器会自动出现。

### CLI 参数

| 参数 | 用途 |
|------|------|
| `--hub-tunnel-id <id>` | Hub 的 DevTunnels ID，向 hub 注册本机 |
| `--tunnel-id <id>` | 本机的 DevTunnels ID |
| `--detect-tunnel` | 通过 `devtunnel list` 自动检测本机 devtunnel ID |
| `--name <name>` | 本机的显示名称（默认：有 tunnel ID 时用 tunnel ID，否则 `local`） |

## 安全提示

服务器默认绑定 `127.0.0.1:19836`（仅本地访问）。要允许局域网访问（如从手机审批），使用 `--lan`：

```bash
python3 server.py --lan
```

这会绑定到 `0.0.0.0:19836`，局域网内可访问。

Web UI 没有身份验证。网络中任何能访问 19836 端口的人都可以审批或拒绝请求。请仅在可信网络中使用，或自行添加认证层。

## 许可证

MIT
