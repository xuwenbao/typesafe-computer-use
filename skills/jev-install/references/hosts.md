# 宿主路径

命令一律是安装目录里 `jev.exe` 的绝对路径，参数是 `mcp`。默认目录是 `%LOCALAPPDATA%\Programs\jev\jev.exe`。

`jev setup` 只合并或追加名为 `jev` 的那一条。确认不了的路径它只打印，不写入。

| 宿主 | 配置 | 技能目录 | 重新加载 |
|---|---|---|---|
| Cursor 用户级 | `%USERPROFILE%\.cursor\mcp.json` | `%USERPROFILE%\.cursor\skills\jev` | 重启 Cursor，或在 MCP 设置里刷新 |
| Cursor 项目级 | `<项目>\.cursor\mcp.json` | `<项目>\.cursor\skills\jev` | 同上 |
| Claude Code | `claude mcp add --scope user\|project jev -- "<exe>" mcp` | 用户级 `%USERPROFILE%\.claude\skills\jev`，项目级 `<项目>\.claude\skills\jev` | 新开一个 Claude Code 会话 |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json`（只有用户级） | 不写入 | 退出并重新打开 Claude Desktop |
| Codex 用户级 | `%USERPROFILE%\.codex\config.toml` 的 `[mcp_servers.jev]` | `%USERPROFILE%\.codex\skills\jev` | 新开一个 Codex 会话 |
| WorkBuddy 用户级 | `%USERPROFILE%\.workbuddy\mcp.json` | `%USERPROFILE%\.workbuddy\skills\jev` | 重启 WorkBuddy，或新开一个会话。首次接入时它可能要求信任这台服务器 |
| WorkBuddy 项目级 | `<项目>\.workbuddy\mcp.json` | `<项目>\.workbuddy\skills\jev` | 同上 |

JSON 宿主只改 `mcpServers.jev`，其他条目保留。写入前会把原文件复制为旁边的 `.bak`。Codex 里已经有 `[mcp_servers.jev]` 时不改动。
