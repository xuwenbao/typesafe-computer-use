# 用智能体安装 jev

把下面这一段粘贴给 Cursor、Claude Code 或 Codex。智能体读得到技能文件时，按技能做；读不到时，按这段里写的步骤做。

jev 会操作这台电脑的屏幕。密钥只在用户自己的终端里输入。

```
你要在这台 Windows x64 电脑上安装 jev。jev 通过 MCP 让你操作这台电脑的屏幕、鼠标和键盘。

资源：
- 安装技能：https://raw.githubusercontent.com/xuwenbao/yuanji-computer-use/main/skills/jev-install/SKILL.md
- 也可以用：npx skills add xuwenbao/yuanji-computer-use --skill jev-install
- 读不到上面的地址、也跑不了 npx 时，按下面的步骤做，不要停下来等技能文件。

步骤：
1. 先问我本地的 clicker-setup-<版本>.exe 在哪里。不要下载，也不要从源码构建。
2. 确认同一个目录里有 clicker-setup-<版本>-1.bin。对照 clicker-setup-<版本>.sha256（每一行是「哈希值  文件名」）。没有这个文件时，向我要哈希值。对不上就停。
3. 告诉我安装会关掉正在运行的 clicker.exe。我同意后运行："<exe>" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
4. 运行 %LOCALAPPDATA%\Programs\clicker\clicker.exe status，确认有版本号，并且 models: yes。
5. 问我要用哪个宿主（cursor、claude-code、claude-desktop、codex、workbuddy）、用户级还是项目级、要不要安装 jev 技能。先运行不带 --write 的 clicker setup，把将要改的内容给我看。我同意后才加 --write。模型密钥请我在自己的终端里配置，不要在对话里向我要。
6. 请我在自己的终端运行 clicker setup --key。不要在对话里向我要密钥。
7. 请我重启或重新加载宿主。我同意后，调用一次 frontmost_app。
8. 报告装了什么、写到了哪里、还有哪些要我自己做。把 %LOCALAPPDATA%\Programs\clicker\agent-prompt.md 的位置告诉我，以后在别的智能体里接 jev 时用它。

规则：不要管理员权限，不要提交代码，不要改 jev 以外的配置，不要跳过哈希核对，不要直接运行 clicker mcp，不要启动其他应用。每一步先说明要做什么，等我确认后再继续。

装好之后，我用一句话就能让你开始，例如：用 jev 在记事本里写一行字。
```
