## Why

安装好的 `clicker.exe` 仍要人自己把 MCP 配置贴进宿主。一个编码智能体可以代做核对、安装和写入，但密钥和「配置写到哪里」必须由使用这台电脑的人决定。DeepEval 的做法是一段可粘贴的提示词、写明触发条件的技能，以及一个引导式的 setup 命令。

## What Changes

- 新增 `clicker setup`。不带 `--write` 时只打印将要做的改动。`--host` 指定 cursor、claude-code、claude-desktop 或 codex。JSON 宿主只合并 `mcpServers.jev`，写之前备份 `.bak`。已有不同条目时要 `--replace`。Claude Code 生成并在条件满足时运行 `claude mcp add`。Codex 在还没有 `[mcp_servers.jev]` 时追加。`--key` 只在终端里用 `getpass` 写入用户目录的 `.env`。`--skill` 复制安装目录里的 `skills\jev`。
- 新增两个技能：`skills/jev-install` 负责安装与核对，`skills/jev` 负责使用。安装包带上后者。
- 新增 `docs/agent-install.md`，一段可以直接粘贴给宿主的安装提示词。
- 构建脚本在安装包旁边写出 `clicker-setup-<版本>.sha256`，每一卷一行。

## Capabilities

### New Capabilities

- `agent-install`：智能体如何从本机安装包安装 jev，以及 `clicker setup` 如何在用户确认后把 MCP 配置和技能写到所选宿主。

### Modified Capabilities

- `standalone-install`：`jev` 增加 `setup` 子命令。它不导入平台适配器或模型。`helper`、`stop`、`install` 仍然不是命令。

## Impact

- `yuanji_computer_use/helper/setup.py`、`helper/cli.py`
- `skills/jev-install/`、`skills/jev/`
- `docs/agent-install.md`、`docs/helper.md`、`README.md`
- `packaging/windows/clicker.iss`、`scripts/build-helper.ps1`
- 离线测试。不在本机运行安装程序、`clicker setup --write` 或 `frontmost_app`。
