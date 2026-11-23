## Why

jev 目前只能由人在终端里用 `clicker` 驱动：入口在 `cli.py`，`.env` 从当前目录读，运行目录写在相对路径 `runs/`，向用户追问用 `input()`。其他智能体（Cursor、Claude Code、Codex 等）想把一段桌面操作交给 jev，只能拼命令行再解析输出。

VISION 说 jev 要「补充或替代一段 LLM 循环」，而交接要带上过程。最自然的交接通道就是 MCP：调用方的大模型把一个目标交给 jev，jev 用便宜的分类器跑完，返回答案、做过的动作和 run 文件夹；或者调用方需要亲自控制时，直接用 jev 的感知与动作原语。

商业产品已经给出了独立安装的成熟形态（2026 年 11 月公开资料与拆包结果）：

- **OpenAI（ChatGPT / Codex）**：Computer Use 在设置里单独「Install」。真正操作屏幕的是另一个签名的助手：macOS 上是 `LSUIElement` 应用 `com.openai.sky.CUAService`，Windows 上是用户态的 `%LOCALAPPDATA%\OpenAI\Codex\runtimes\cua_node\<哈希>\...\codex-computer-use.exe`，不注册服务。前端经本机 IPC 用长度前缀加 JSON-RPC 通信，连不上时拉起助手。
- **Anthropic（Claude Desktop）**：屏幕控制的原生模块打在应用包内；和产品安装包分开的是代理助手与 Cowork 虚拟机镜像，都按校验和后下载。
- **传统 RPA（UiPath、Automation Anywhere、Power Automate）**：执行端单独以 MSI 安装，真正操作界面的执行器跑在用户会话里。

jev 的结构适合照 Codex 的方向拆：`runner.run(RunConfig, ctx_factory)` 已经是与命令行无关的整段循环；`platform_adapter.Desktop` 已经是一组干净的感知与动作原语，`using()` 可以给它套上包装。

## What Changes

本次在 Windows 上把 jev 做成可单独安装的 MCP 服务，并且由 `jev mcp` 自己操作屏幕。

- 新增一个冻结的 `jev.exe`，子命令只有 `mcp` 和 `status`。由 PyInstaller 冻结为带控制台的程序，Authenticode 签名，由 Inno Setup 安装程序 `jev-setup-<版本>.exe` 按用户装到 `%LOCALAPPDATA%\Programs\jev\`，不需要管理员，不注册 Windows 服务。安装包内含感知模型。没有 `jev helper`、`jev stop`、`jev install`，也没有命名管道、对端校验、版本替换或空闲退出。
- `jev mcp` 是宿主智能体以 stdio 启动的 MCP 服务（FastMCP）。握手只导入前端和 FastMCP。第一次工具调用才加载感知与平台适配器，并在这个进程里执行。同一用户的多个 `jev mcp` 用命名互斥量 `Local\jev-desk-<SID>` 轮流操作桌面，拿不到就返回 `busy`。升级和卸载会关掉仍在运行的 `jev.exe`，正在进行的调用被中断，智能体要重新启动 MCP。`jev status` 报告本程序的版本、路径、用户目录，以及 exe 旁边有没有 `models\`。安装完成时把 MCP 配置写入 `mcp-config.json`，完成页不显示它。
- 两级工具：
  - 高层：`run_goal` 把一个目标交给与 `clicker --act` 相同的循环（含 `CLICKER_DRIVER` 打开时的接手），返回 `run.json` 中的结局、答案、步数、动作历史、交接与 run 文件夹；`inspect_screen` 只读屏。拉不到前台的应用返回 `not_found`，这台机器跑不了感知时 `run_goal` 返回 `not_supported`。
  - 底层：`get_app_state`、`click`、`type_text`、`set_value`、`press_key`、`scroll`、`activate`、`frontmost_app`。
- 所有工具都直接动作，没有演练模式。jev 只作为工具被调用：不向用户提问，也不向智能体发任何反向请求。门禁只剩静默硬拒绝：终端、系统防护界面、jev 自身与宿主智能体永远不可操作，IDE 只能点击，其余应用不问直接操作。`run_goal` 的写手 `ask` 为空。
- 一个进程同一时间只执行一个调用；另一个进程正持有桌面时也返回 `busy`。MCP 取消或左上角急停都会在下一次输入前停下。关掉智能体就是关掉这个进程。

不在本次范围内：

- macOS 助手（签名公证的 `.app`、Group Container 套接字、TCC 授权归属），以及 Linux 助手。
- 再拆一个常驻助手进程：不做。屏幕就在 `jev mcp` 里操作。
- 演练模式：不做；需要只看时用 `inspect_screen`。
- 锁屏后继续操作、Windows 服务形态的无人值守执行、HTTP/SSE 远程 MCP、浏览器后端的 MCP 工具。
- `run_goal` 的逐步进度通知、助手的自动更新。

## Capabilities

### New Capabilities

- `mcp-server`：jev 经 MCP 向其他智能体提供的工具与契约：`run_goal` 与 `inspect_screen`、底层原语、元素编号的有效范围、硬拒绝且不提问、永远禁止操作的目标、本进程与跨进程的串行执行、取消与急停、协议输出不被日志污染。
- `standalone-install`：Windows 上一个 `jev.exe` 如何与宿主分开安装：用户目录、一次安装的安装程序（内含模型）、单进程执行、第一次调用才加载、本用户的桌面互斥量、升级时关掉正在运行的 `jev.exe`。

### Modified Capabilities

无。现有规格规定的感知与决策行为不变。

## Impact

- **代码**：新增 `typesafe_computer_use/helper/`（`cli`、`front`、`service`、`desk_lock`、`approvals`、`guard`、`home`）；`pyproject.toml` 增加 `fastmcp==4.2.0` 与 `jev` 入口；新增 `packaging/windows/jev.spec`、`packaging/windows/jev.iss` 与 `scripts/build-helper.ps1`。`cli.py`、`runner.py`、`actions.py` 不变。
- **依赖**：FastMCP 4.2.0（基于官方 MCP SDK）；打包时用 PyInstaller 与 Inno Setup 6；发布签名版需要 Windows Authenticode 证书。
- **体积**：`jev.exe` 包含 PyTorch 等依赖，约 1.5 GB；感知模型约 1.4 GB 打进安装程序，安装后约 3 GB。每个智能体各加载一份模型。模型仍按固定提交与 SHA-256 校验。
- **测试**：`tests/conftest.py` 的机器守护只加不减：创建桌面互斥量在测试里被拒。工具与前端都在桩的 `Desktop` 上测试，互斥量用假锁。
- **文档**：新增 `docs/helper.md`；更新 `README.md`、`docs/layout.md`、`docs/windows.md`；`AGENTS.md` 写明运行 `jev mcp` 每次需要批准。
- **不受影响**：`clicker` 与 `clicker-inspect` 的现有用法、WAA 集成、沙盒、浏览器后端。
