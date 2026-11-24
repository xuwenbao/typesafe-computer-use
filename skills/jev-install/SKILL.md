---
name: jev-install
description: >
  安装、升级或检查 Windows 上的 jev。用户要安装、升级或确认 jev 是否装好时使用。
  开发 jev 本身，或只是要调用已经接好的 jev 工具时不要使用。
---

# 安装 jev

jev 会操作这台电脑的屏幕、鼠标和键盘。每一步先说明要做什么，等用户确认后再继续。

## 流程

1. 确认系统是 Windows x64。向用户要已经下载好的 `clicker-setup-<版本>.exe` 的本地路径。不要从网络下载，也不要从源码构建。
2. 核对同一个目录里有 `clicker-setup-<版本>-1.bin`。对照 `clicker-setup-<版本>.sha256`：每一行是 `<sha256>  <文件名>`。没有这个文件时，请用户给出哈希值。对不上就停。
3. 告诉用户：静默安装会关掉正在运行的 `clicker.exe`，进行中的调用会被中断。用户批准后运行：

```
"<exe>" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

4. 运行 `%LOCALAPPDATA%\Programs\clicker\clicker.exe status`。要看到版本号，以及 `models: yes`。
5. 问用户选哪个宿主（cursor、claude-code、claude-desktop、codex、workbuddy），配置写在用户级还是项目级，要不要安装 `jev` 技能。不确定时先运行不带 `--write` 的命令，把将要改的内容给用户看：

```
clicker setup --host <宿主> --scope user|project --project <目录> --skill
```

用户确认后加上 `--write`。已有不同的 `jev` 条目时，再征得同意才加 `--replace`。
6. 看第 4 步 `status` 里的 `TYPESAFE_API_KEY` 和 `writer`。有一项是 `no`，就请用户在自己的终端里运行 `clicker setup` 补上。不要在对话里索要密钥。
7. 把 `clicker setup --write` 输出里 `next:` 那一行转告用户。WorkBuddy 要先在「我的 MCP」里信任 jev 才会启动它。其他宿主请用户重启或重新加载。用户同意后，调用一次 `frontmost_app` 做验证。
8. 报告装了什么、配置写到了哪里、还有哪些步骤要用户自己做。把安装目录里的 `agent-prompt.md` 交给用户：在别的智能体里接 jev 时，粘贴这段就能接好并验证。

## 不能做的事

- 不要申请管理员权限。
- 不要在对话里索要 `TYPESAFE_API_KEY`。
- 不要直接运行 `clicker mcp`。
- 不要改 jev 以外的宿主配置。
- 不要跳过哈希核对。
- 不要启动其他应用。

宿主路径见 [references/hosts.md](references/hosts.md)。装完仍不对时见 [references/troubleshooting.md](references/troubleshooting.md)。
