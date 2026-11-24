# Spec Delta

## Purpose

智能体从本机安装包安装 jev，并由 `clicker setup` 在用户确认后写入所选宿主的 MCP 配置与技能。

## ADDED Requirements

### Requirement: 默认只展示改动
`clicker setup --host <宿主>` SHALL 打印将要写入的 MCP 改动并以退出码 0 结束，且 MUST NOT 创建或修改任何文件。`--write` SHALL 才写入。宿主 SHALL 是 `cursor`、`claude-code`、`claude-desktop`、`codex` 或 `workbuddy`。命令 SHALL 是所给 `clicker.exe` 的绝对路径，参数 SHALL 是 `mcp`。不带 `--host` 且标准输入不是终端时 SHALL 拒绝，不进入问答。

#### Scenario: 预览不落盘
- **WHEN** 运行 `clicker setup --host cursor` 且不带 `--write`
- **THEN** 输出包含将要写入的配置路径，退出码为 0，目标路径上没有新文件

#### Scenario: 管道里没有宿主
- **WHEN** 标准输入不是终端，且命令没有 `--host` 也没有 `--key`
- **THEN** 命令拒绝，不写文件

### Requirement: JSON 宿主只合并 jev
对 Cursor 与 Claude Desktop，写入 SHALL 只设置 `mcpServers.jev`，并保留文件里的其他条目。写之前 SHALL 把原文件复制为旁边的 `.bak`，再原子替换。已有的 `jev` 条目与要写的不同时，MUST NOT 覆盖，除非带了 `--replace`。

#### Scenario: 保留其他服务器
- **WHEN** Cursor 的 `mcp.json` 里已有另一个服务器，并运行 `clicker setup --host cursor --write`
- **THEN** 该服务器仍在，`mcpServers.jev` 指向这次的 `clicker.exe`，且存在 `.bak`

#### Scenario: 不同的 jev 条目
- **WHEN** `mcpServers.jev` 的命令与要写的不同，且没有 `--replace`
- **THEN** 命令拒绝，文件内容不变

### Requirement: Codex 已有段落时不改
用户级 Codex 配置 SHALL 在还没有 `[mcp_servers.jev]` 时追加这一段。已经有这一段时 SHALL 只打印，MUST NOT 改动文件，即使带了 `--replace`。项目级 Codex 配置 SHALL 只打印，不写入。

#### Scenario: 段落已存在
- **WHEN** `config.toml` 已含 `[mcp_servers.jev]`，并运行 `clicker setup --host codex --write --replace`
- **THEN** 文件内容不变，输出说明留下未改

### Requirement: Claude Code 用它自己的命令
Claude Code SHALL 生成 `claude mcp add --scope <scope> jev -- "<exe>" mcp`。带 `--write` 且 `claude` 在 PATH 上时 SHALL 运行它。否则 SHALL 只打印，不运行。

#### Scenario: 找不到 claude
- **WHEN** 带 `--write`，且 `claude` 不在 PATH 上
- **THEN** 输出这条命令，且没有启动进程

#### Scenario: 替身执行命令
- **WHEN** 带 `--write`，且 `claude` 在 PATH 上
- **THEN** 启动的命令是 `claude mcp add`，范围和 `clicker.exe` 与参数一致

### Requirement: 密钥只从终端读
`clicker setup --key` SHALL 用 `getpass` 读取 `TYPESAFE_API_KEY` 并写入用户目录的 `.env`，保留其中的其他变量，且 MUST NOT 把密钥打印出来。标准输入不是终端时 SHALL 拒绝，不写文件。`--key` MUST NOT 与 `--host`、`--write`、`--skill` 或 `--replace` 一起用。

#### Scenario: 标准输入不是终端
- **WHEN** 运行 `clicker setup --key`，且标准输入不是终端
- **THEN** 命令拒绝，`.env` 不被创建或修改

### Requirement: 技能整目录复制
`--skill` SHALL 把 `clicker.exe` 旁边的 `skills\jev` 整目录复制到所选宿主的技能目录。目标已存在且内容不同时，MUST NOT 覆盖，除非带了 `--replace`。没有已确认技能目录的宿主 SHALL 只打印，不复制。

#### Scenario: 复制子目录
- **WHEN** 运行 `clicker setup --host cursor --skill --write`，且安装目录里的技能含 `references` 下的文件
- **THEN** 用户技能目录里有同样的文件和内容

#### Scenario: 内容不同
- **WHEN** 目标技能目录已存在且内容不同，且没有 `--replace`
- **THEN** 命令拒绝，目标内容不变

### Requirement: 安装技能与使用技能
仓库 SHALL 提供 `skills/jev-install` 与 `skills/jev`。安装技能 SHALL 写明在用户要安装、升级或检查 jev 时触发，在开发 jev 或只使用工具时不触发，并要求核对本地安装包的 SHA-256、在用户批准后才静默安装、不在对话里索要密钥、不直接运行 `clicker mcp`。使用技能 SHALL 写明在 jev 的工具可用且用户要操作桌面时触发。安装程序 SHALL 把 `skills\jev` 装到 `{app}\skills\jev`。构建 SHALL 在安装包旁边写出 `clicker-setup-<版本>.sha256`，每一卷一行。

#### Scenario: 技能文件在仓库里
- **WHEN** 查看仓库的 `skills/jev-install` 与 `skills/jev`
- **THEN** 两者都有 `SKILL.md`，安装技能写明触发与不触发，使用技能有工具说明
