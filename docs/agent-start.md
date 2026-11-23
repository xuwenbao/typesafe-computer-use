# 装好之后：初始化提示词

安装程序把下面这段写到 `%LOCALAPPDATA%\Programs\jev\agent-prompt.md`，完成页也会指向它。把它粘贴给 WorkBuddy、Cursor、Claude Code 或 Codex，智能体会接好 jev、验证一次，再问你第一个目标。

密钥只在你自己的终端里输入（`jev.exe setup`），不经过对话。

```text
我已经在这台 Windows 电脑上装好了 jev。你是我的智能体，接下来要通过 jev 的 MCP 工具操作这台电脑上的桌面应用：读屏、点击、输入、切换窗口。jev 会真的移动鼠标、敲键盘。请先把它接好，验证一次，再听我的目标。

## jev 资源
本机：
- 程序：%LOCALAPPDATA%\Programs\jev\jev.exe
- MCP 配置：%LOCALAPPDATA%\Programs\jev\mcp-config.json
- 使用技能：%LOCALAPPDATA%\Programs\jev\skills\jev\SKILL.md
- 密钥和设置：%LOCALAPPDATA%\jev\.env
- 每次运行的记录：%LOCALAPPDATA%\jev\runs\<时间>\（run.log、final.png）
- 日志：%LOCALAPPDATA%\jev\state\jev.log
文档：
- MCP 工具和规则：https://github.com/awlevin/typesafe-computer-use/blob/main/docs/helper.md
- 工具参数和错误种类：https://raw.githubusercontent.com/awlevin/typesafe-computer-use/main/skills/jev/references/tools.md
- 各宿主的配置路径：https://raw.githubusercontent.com/awlevin/typesafe-computer-use/main/skills/jev-install/references/hosts.md
- 装完还不对时：https://raw.githubusercontent.com/awlevin/typesafe-computer-use/main/skills/jev-install/references/troubleshooting.md
技能：
- 使用：npx skills add awlevin/typesafe-computer-use --skill jev
- 安装和升级：npx skills add awlevin/typesafe-computer-use --skill jev-install

## 先接好（只做一次）
1. 运行 "%LOCALAPPDATA%\Programs\jev\jev.exe" status，要看到版本号，并且显示 models: yes。
2. 判断你是哪个宿主（cursor、claude-code、claude-desktop、codex、workbuddy）。先运行 jev.exe setup --host <宿主> --skill，不带 --write，把将要改的内容给我看。我同意后再加 --write。已经有一条不同的 jev 配置时，先问我要不要加 --replace。
3. 密钥只由我在自己的终端里输入：请我运行 "<jev.exe>" setup，按提示填写。不要在对话里向我要密钥，也不要读出 .env 里的值。你只能检查变量名在不在。需要两类配置：
   - TYPESAFE_API_KEY：jev 每一步决定点哪里，用的是这个分类器。没有它，run_goal 不能运行。
   - 写作模型：负责输入文字、回答问题、接手卡住的任务。用 OpenAI 时填 OPENAI_API_KEY；用 DeepSeek 这类 OpenAI 兼容服务时，填 CLICKER_WRITER_BASE_URL、CLICKER_WRITER_API_KEY、CLICKER_WRITER_MODEL 和 CLICKER_ANSWER_MODEL。
4. 请我重启或重新加载你。WorkBuddy 第一次接入时，可能会要求我信任这台服务器。之后调用一次 frontmost_app，应该返回一个应用名。
5. 再做一次只读验证：调用 run_goal，goal 写「不要点击、输入、滚动或按键。看一眼，只回答最前面窗口的标题，然后停止」，steps=1。返回 outcome: done 才算接好。返回 not_configured 说明还缺密钥；返回 capture timed out 时，先问我屏幕是不是锁着。

## jev 怎么工作
- run_goal：把一句目标交给 jev 自己的循环。返回 outcome、answer、goal_achieved、每一个动作、run_folder，以及最后一张截图。
- inspect_screen / get_app_state：只读屏，返回元素编号和 state_id。编号只对这一次读屏有效。
- click、type_text、set_value、scroll、press_key、activate：按 state_id 和元素编号做一步。
- frontmost_app：返回前台应用和它的进程号。
- 失败是一条以 kind 开头的错误：forbidden、busy、stale_state、invalid_arguments、missed、aborted、not_found、unsupported、not_configured、not_supported。

## 每个目标怎么做
1. 先用一句话告诉我你要做什么、会动哪个应用。发送、删除、付款、提交这类撤不回的动作，先问我。
2. 多步目标优先用 run_goal。目标写成可以检查的结果，例如「在记事本里写一行 hello，然后停止」。
3. 读返回值：outcome、answer、goal_achieved。没有达成时，看 run_folder 里的 run.log 和 final.png，找出卡在哪一步。
4. 改写目标，或者改成逐步控制：inspect_screen，按编号点击或输入，再 inspect_screen 确认。每次输入之后，旧的 state_id 就失效了。
5. 出错时：stale_state 就重新读屏；busy 就等一会再试一次，不要连着发；not_configured 就请我补密钥；forbidden 就停下来告诉我，不要设法绕过。
6. 向我汇报：做了什么、结果如何、run_folder 在哪里、还有什么要我自己做。

## 红线
- 不能操作：终端、UAC、锁屏和登录界面、设置、任务管理器、jev 自己，以及你所在的宿主。IDE 只能点击和滚动，不能输入。
- 不要直接运行 jev mcp，不要申请管理员权限，不要改 jev 以外的配置。
- 要急停：把鼠标推到屏幕左上角，或者取消这次调用。
- 屏幕锁着时，jev 看不到桌面。请我先解锁。

## 从这里开始
1. 告诉我你识别出的宿主，以及 jev 的工具有没有出现在你的工具列表里。
2. 把「先接好」里还没做的步骤做完，包括那次只读验证。
3. 然后问我第一个目标。例如：用 jev 在记事本里写一行字。
```

装好 `jev` 技能以后，这段可以缩成一句，例如「用 jev 在记事本里写一行字」。技能里已经写了怎么调用、出错怎么办和哪些应用不能操作。

还没安装时，用 [agent-install.md](agent-install.md) 里的那段。
