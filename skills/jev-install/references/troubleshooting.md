# 安装后仍不对

## `models: no`

`jev status` 是在安装目录里的 `jev.exe` 上运行的，模型应在它旁边的 `models\`。解压出来的构建目录没有这份模型，会显示 `no`。安装程序没把模型放进去时，用同一套 `.exe` 和 `.bin` 再装一次。

## 宿主看不到工具

看配置里的命令是不是 `jev.exe` 的绝对路径，参数是不是 `mcp`。改完配置后要重启或重新加载宿主。Claude Code 需要 `claude` 在 PATH 上，`jev setup --write` 才会真正执行那条 `claude mcp add`。

WorkBuddy 读到 `mcp.json` 里的 jev 之后，还要用户在「MCP 服务管理」→「我的 MCP」里点「信任」。没有信任时它不会启动 `jev.exe`，重启也没用。信任记录在 `%USERPROFILE%\.workbuddy\mcp-approvals.json`，和这条配置的内容绑定：`jev setup --replace` 改了配置之后，要再信任一次。

## `TYPESAFE_API_KEY: no` 或 `writer: no`

`jev status` 只看 `%LOCALAPPDATA%\jev\.env` 里有没有这些名字。请用户在自己的终端运行 `jev.exe setup` 补上，再运行一次 `jev status`。

## `busy`

另一个 `jev mcp` 正拿着这台电脑的桌面。等它结束再试一次，不要连着发。

## 安装包分卷缺失

`jev-setup-<版本>.exe` 和 `jev-setup-<版本>-1.bin` 必须在同一个目录。缺了分卷，安装程序无法继续。哈希文件里每一卷一行。
