# Spec Delta

## Purpose

jev 经 MCP 向其他智能体提供的工具：把一个目标交给 jev 跑完的高层工具，以及调用方自己决策时使用的感知与动作原语；还有这些工具共同遵守的硬拒绝、串行与取消规则。jev 只回答调用，不向用户或智能体发问。本次只在 Windows 上提供。

## ADDED Requirements

### Requirement: run_goal 把目标交给 jev 的完整循环
`run_goal` SHALL 接受 `goal`，以及可选的 `steps`、`handoffs`、`min_confidence`，并用与 `clicker --act` 相同的步骤循环真实执行，包括 `CLICKER_DRIVER` 打开时由更强的模型接手；它 MUST NOT 有演练模式。它 SHALL 返回本次运行的 `outcome`、`answer`、`goal_achieved`、`steps_taken`、`history`、`handoffs`、`questions`、`calls`、`seconds` 与 run 文件夹的绝对路径，以及最后一屏的标注图。这些字段 MUST 与该次运行写入的 `run.json` 一致。这台机器跑不了桌面感知时 SHALL 返回 `not_supported`。`CLICKER_WRITER_VISION`、`CLICKER_DRIVER` 或 `CLICKER_DRIVER_STEPS` 不合法时 SHALL 返回 `not_configured`。

#### Scenario: 跑完返回过程
- **WHEN** 调用方调用 `run_goal`，循环以 `done` 结束
- **THEN** 返回里 `outcome` 为 `done`，`run_folder` 指向的目录里有 `run.json`，其中的 `answer` 与返回的 `answer` 相同

#### Scenario: 没有 TypeSafe 密钥
- **WHEN** 进程的环境里没有 `TYPESAFE_API_KEY`，调用方调用 `run_goal`
- **THEN** 返回 `not_configured`，没有任何输入事件

#### Scenario: 这台机器跑不了感知
- **WHEN** 桌面感知在这台机器上不可用，调用方调用 `run_goal`
- **THEN** 返回 `not_supported`，没有任何输入事件

### Requirement: inspect_screen 只感知不动作
`inspect_screen` SHALL 读一次屏，返回前台应用、URL、焦点字段、编号元素列表（编号、文本、角色、来源、以屏幕点为单位的框）、`state_id` 与标注图。它 MUST NOT 发出任何输入事件，也 MUST NOT 激活、启动或关闭任何应用。

#### Scenario: 不动作
- **WHEN** 调用方调用 `inspect_screen`
- **THEN** 适配器没有收到任何输入方法的调用

### Requirement: 底层原语与状态编号
服务 SHALL 提供 `get_app_state`、`click`、`type_text`、`set_value`、`press_key`、`scroll`、`activate` 与 `frontmost_app`，它们都直接动作。按元素操作的原语 MUST 带当前 `state_id`。服务只保留最近一次状态，任何输入都使它过期；带着其他 `state_id` 的调用 SHALL 返回 `stale_state` 且不执行。

#### Scenario: 过期编号被拒
- **WHEN** 调用方先取得状态 A，再取得状态 B，然后用状态 A 的 `state_id` 点击元素 1
- **THEN** 返回 `stale_state`，没有点击发生

### Requirement: 元素上的动作与找不到的应用
元素上的左键单击 SHALL 先经无障碍树按下，没按下时再点框中心；右键与双击 SHALL 直接点框中心。光标没到目标时 SHALL 返回 `missed`。`type_text` 给了元素时 SHALL 先点击它再逐键输入。`set_value` SHALL 只对无障碍元素设值，否则返回 `unsupported`。`get_app_state` 与 `activate` 拉不到点名的应用时 SHALL 返回 `not_found`，且不读屏。

#### Scenario: 按元素点击走无障碍树
- **WHEN** 元素 1 来自无障碍树，调用方以当前 `state_id` 左键单击元素 1
- **THEN** 服务对它执行无障碍按下，不发出鼠标点击

#### Scenario: 点击没有落到目标
- **WHEN** 调用方以坐标点击，光标没有到达该点
- **THEN** 返回 `missed`

#### Scenario: 右键点在元素中心
- **WHEN** 调用方以当前 `state_id` 对元素 1 右键
- **THEN** 在元素框的中心发出右键，不经无障碍树按下

#### Scenario: 输入先点击元素
- **WHEN** 调用方以当前 `state_id` 对元素 2 调用 `type_text`
- **THEN** 先点击元素 2，再把文本逐键输入

#### Scenario: 不能设值的元素
- **WHEN** 元素 2 不来自无障碍树，调用方对它调用 `set_value`
- **THEN** 返回 `unsupported`，没有输入发生

#### Scenario: 应用拉不到前台
- **WHEN** 调用方对一个没有窗口的应用调用 `activate` 或 `get_app_state`
- **THEN** 返回 `not_found`，没有读屏

### Requirement: 硬拒绝，不提问
服务 MUST NOT 向用户或调用方智能体提问，也 MUST NOT 为批准一个应用而等待。IDE 与代码编辑器 SHALL 只能点击与滚动，不能输入或按键。其余应用 SHALL 直接操作。`run_goal` 内的每个输入 SHALL 在执行前经过同一道门禁，且写手的 `ask` SHALL 为空，因此写手不会提问。被拒时 `runner.run` 把它当作急停接住：运行 SHALL 在该动作之前结束，这次调用返回运行记录，其中 `outcome` 为 `aborted (...)`，而不是 `forbidden`。原语上的拒绝 SHALL 返回 `forbidden`。

#### Scenario: 未列出的应用直接操作
- **WHEN** 前台是记事本，调用方调用 `press_key`
- **THEN** 按键发出，期间没有任何提问

#### Scenario: 只点级别不能输入
- **WHEN** 前台是 VS Code，调用方调用 `type_text`
- **THEN** 返回 `forbidden`，说明该应用只允许点击与滚动

#### Scenario: 运行中被拒绝
- **WHEN** `run_goal` 下一步要操作的前台应用属于永远拒绝或只点且这次是输入
- **THEN** 该动作没有发出，返回的 `outcome` 以 `aborted` 开头，run 文件夹里有 `run.json`

### Requirement: 永远禁止操作的目标
服务 MUST NOT 向以下目标发出任何输入：终端与 shell、`clicker.exe` 自身、发起请求的宿主智能体（前端的祖先进程）、设置、任务管理器、锁屏与登录界面、UAC 提示。没有任何配置能解除这条限制。服务 MUST NOT 打开文件。

#### Scenario: 宿主应用
- **WHEN** 宿主是 Cursor，调用方在 Cursor 前台时调用 `press_key`
- **THEN** 返回 `forbidden`，没有按键发生

### Requirement: 一次一个调用
一个 `clicker mcp` 进程 SHALL 同一时间只执行一个调用。另一个 `clicker mcp` 正在操作这台电脑时，新的调用 SHALL 立即返回 `busy`。两种情况都不排队，也不影响正在运行的调用。

#### Scenario: 本进程已有调用
- **WHEN** 一个 `run_goal` 正在运行，同一进程又收到 `click`
- **THEN** 立即返回 `busy`，正在运行的循环不受影响

#### Scenario: 另一个进程正在操作
- **WHEN** 另一个 `clicker mcp` 正持有这台电脑，本进程收到 `click`
- **THEN** 立即返回 `busy`，没有输入发生

### Requirement: 取消与急停
智能体取消一个进行中的调用时，服务 SHALL 在下一次急停检查时停止该调用，不再发出输入，`run_goal` 的 `outcome` 记为 `aborted (cancelled by the client)` 并照常写出 `run.json`。进程退出时这次调用随之结束。左上角急停 SHALL 保持现有行为。

#### Scenario: 调用被取消
- **WHEN** 一个调用正在运行，智能体取消这次调用
- **THEN** 该调用在下一次急停检查时以 `cancelled by the client` 停止

### Requirement: 协议输出不被日志污染
`clicker mcp` MUST 只把 MCP 协议消息写到 stdout。第一次工具调用 SHALL 把之后的标准输出与标准错误指到用户目录下的 `jev.log`；一次运行的步骤日志 SHALL 写到该次的 run 文件夹。

#### Scenario: 运行日志
- **WHEN** 一次调用里有打印，或一次 `run_goal` 产生步骤日志
- **THEN** stdout 上只有 MCP 消息，打印出现在 `jev.log`，步骤日志出现在 `run.log`
