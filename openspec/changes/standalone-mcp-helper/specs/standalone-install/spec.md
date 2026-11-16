# Spec Delta

## Purpose

jev 如何在 Windows 上与调用它的宿主智能体分开安装：一个 `jev.exe`，用户目录，一次安装的安装程序（内含模型）。`jev mcp` 在自己的进程里操作屏幕，握手时不加载模型，第一次工具调用才加载；同一用户的多个进程用一把命名互斥量轮流操作桌面。升级会关掉仍在运行的 `jev.exe`。

## ADDED Requirements

### Requirement: 配置与运行目录在用户目录
`jev mcp` SHALL 从固定的用户目录读取 `.env`、写入 run 文件夹与日志，而不依赖任何工作目录：Windows 为 `%LOCALAPPDATA%\jev\`，设置了 `JEV_HOME` 时以它为准。`clicker` 与 `clicker-inspect` 的现有默认行为 MUST 保持不变。

#### Scenario: 宿主在任意目录启动
- **WHEN** 宿主在任意目录启动 `jev mcp`，`TYPESAFE_API_KEY` 只写在用户目录的 `.env` 里
- **THEN** 这次调用读到这个密钥，本次运行的 run 文件夹建在用户目录的 `runs\` 下

### Requirement: 一个进程里执行
屏幕读取与输入 SHALL 由 `jev mcp` 在自己的进程里执行。MCP 握手 MUST NOT 导入平台适配器、感知或模型；第一次工具调用 SHALL 才加载它们，并创建这次进程的服务。`jev` SHALL 只有 `mcp` 与 `status` 两个子命令，MUST NOT 提供 `helper`、`stop` 或 `install`，也 MUST NOT 注册为 Windows 服务。

#### Scenario: 握手不加载模型
- **WHEN** 智能体启动 `jev mcp` 并完成列举工具，尚未调用任何一个工具
- **THEN** 进程没有创建服务，也没有导入平台适配器、感知或模型

#### Scenario: 第一次调用才加载
- **WHEN** 智能体第一次调用一个工具
- **THEN** 这次调用在本进程里执行，并读屏或输入

### Requirement: 本用户的桌面互斥量
每次调用 SHALL 先拿本进程的锁，再非阻塞地拿命名互斥量 `Local\jev-desk-<用户 SID>`。`WAIT_OBJECT_0` 与 `WAIT_ABANDONED` 都算拿到。任一把拿不到 SHALL 立即返回 `busy`，并释放已经拿到的锁，不发出输入。拿锁与放锁 SHALL 发生在执行这次调用的同一个线程上。进程崩溃时操作系统 SHALL 释放这把互斥量。这条路径 MUST NOT 监听任何网络端口。

#### Scenario: 互斥量已被占用
- **WHEN** 另一个 `jev mcp` 正持有这把互斥量，本进程收到一次工具调用
- **THEN** 立即返回 `busy`，没有输入发生，本进程的锁也被释放

#### Scenario: 持有者崩溃
- **WHEN** 持有互斥量的进程崩溃
- **THEN** 下一个 `jev mcp` 可以拿到这把互斥量并执行调用

### Requirement: 一次安装就能用
安装 SHALL 是一个按用户安装的 Windows 安装程序 `jev-setup-<版本>.exe`，不需要管理员权限，安装到 `%LOCALAPPDATA%\Programs\jev\`。安装目录 SHALL 包含 `jev.exe`、`version.txt` 和感知模型。程序 SHALL NOT 提供 `jev install` 子命令，SHALL NOT 使用 `runtimes`、`current.json` 或目录联接。升级或卸载 SHALL 关掉仍在运行的 `jev.exe`，因此正在进行的调用会被中断，智能体需要重新启动 MCP。安装完成 SHALL 显示 MCP 配置，并把同样内容写入安装目录的 `mcp-config.json`。其中的命令 SHALL 是安装目录里 `jev.exe` 的绝对路径，参数是 `mcp`。升级 SHALL 覆盖同一目录，使已有的 MCP 配置不用改。`jev status` SHALL 打印版本、可执行文件路径、用户目录，以及 exe 旁边有没有 `models\`。

#### Scenario: 首次安装
- **WHEN** 用户运行安装程序
- **THEN** 程序出现在 `%LOCALAPPDATA%\Programs\jev\jev.exe`，旁边有 `models\`，完成页给出指向这个 exe 的 MCP 配置

#### Scenario: 升级关掉正在运行的 jev
- **WHEN** 一个 `jev mcp` 正在执行调用，用户运行新版本的安装程序
- **THEN** 安装程序关掉这个进程，这次调用被中断，文件被覆盖，MCP 配置里的命令路径不变

### Requirement: 感知模型随安装程序分发
安装程序 SHALL 内置感知模型文件。这些文件 SHALL 仍按固定的仓库提交取用，并在加载前校验 SHA-256。冻结的 `jev.exe` 在旁边存在 `models\` 时 SHALL 在导入模型库之前把 Hugging Face 缓存指到该目录，且不覆盖已经设置的缓存路径，并离线读取。模型缺失时，冻结程序 SHALL 报告安装不完整并提示重新运行安装程序；未冻结的检出 SHALL 仍提示运行 `clicker-models fetch`。

#### Scenario: 安装后第一次读屏
- **WHEN** 新装的 `jev mcp` 第一次读屏
- **THEN** 它读取旁边的 `models\`，不访问网络

#### Scenario: 安装不完整
- **WHEN** 冻结的程序读不到它需要的模型文件
- **THEN** 报错说明安装不完整，并提示重新运行安装程序
