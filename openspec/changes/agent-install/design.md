## Context

`jev.exe` 装在 `%LOCALAPPDATA%\Programs\jev\`。宿主要一条绝对路径加参数 `mcp`。各宿主的配置文件格式不同，而且会变。密钥不能出现在智能体的对话里。

## Goals / Non-Goals

**Goals:**

- 智能体先问用户，再带上用户选中的参数运行 `jev setup`。
- 默认只打印改动，退出码 0，不写文件。
- 密钥只从终端读入，写进 `home.env_file()`，并保留其他变量。

**Non-Goals:**

- 不从网络下载安装包，也不从源码构建。
- 不替用户选择宿主。
- 不在对话里收集 `TYPESAFE_API_KEY`。
- 不运行 `jev mcp`，也不操作屏幕。

## Decisions

1. 写入与预览分开。`--write` 才落盘。JSON 用临时文件再替换，原文件复制为 `.bak`。只改 `mcpServers.jev`。
2. 已有且不同的 JSON 条目必须 `--replace`。Codex 已经有 `[mcp_servers.jev]` 时只打印，不改，避免手写 TOML 把别的键弄坏。
3. Claude Code 不手改它的 JSON。命令是 `claude mcp add --scope <scope> jev -- "<exe>" mcp`。`--write` 且 `claude` 在 PATH 上才运行，否则只打印。
4. 确认了的路径才写入：Cursor 的 `mcp.json` 与 `.cursor/skills`，Claude Desktop 用户级的 `claude_desktop_config.json`，Claude Code 的技能目录，Codex 用户级的 `config.toml` 与 `.codex/skills`。项目级的 Claude Desktop 和项目级的 Codex 配置只打印。
5. `--key` 在标准输入不是终端时拒绝。它不能和 `--host`、`--write`、`--skill`、`--replace` 一起用。
6. 技能源是 `jev.exe` 旁边的 `skills\jev`。目标已存在且内容不同时要 `--replace`。
7. 不带 `--host` 且标准输入是终端时才问答。智能体的管道不是终端，必须带 `--host`。

## Risks / Trade-offs

- 宿主改配置格式或技能目录 → 写入前打印；没确认的路径不写。
- 静默安装会关掉正在运行的 `jev.exe` → 安装技能在用户批准前说明。
- `npx skills` 需要 Node → 提示词同时给出复制目录和按步骤做的办法。

## Migration Plan

已有的 MCP 配置不用改。`jev setup` 看到相同条目时不重写。不同条目要用户加上 `--replace`。

## Open Questions

无。
