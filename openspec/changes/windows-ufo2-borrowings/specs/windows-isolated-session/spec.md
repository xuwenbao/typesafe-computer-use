## ADDED Requirements

### Requirement: 只在 Windows 上提供隔离会话
`clicker session` SHALL 在 Windows 上启动一个子会话并在其中运行 jev。在 macOS 与其他系统上，该命令 MUST 报错说明只支持 Windows，且 MUST NOT 导入 Windows 专用模块。隔离会话的实现 MUST NOT 进入 macOS 的导入路径。

#### Scenario: 在 macOS 上执行
- **WHEN** 在 macOS 上执行 `clicker session`
- **THEN** 命令报错说明只支持 Windows，且没有启动任何会话或 jev 进程

### Requirement: 前提不满足就停
子会话 SHALL 只在以下前提都成立时启动：系统是 Windows 专业版、企业版或教育版；本机可以用远程桌面连接 `localhost`；当前登录不是智能卡登录；这台机器上还没有另一个子会话。任一前提不成立时，命令 MUST 报出具体原因并退出，MUST NOT 改在主桌面的控制台会话里运行 jev。

#### Scenario: 家庭版
- **WHEN** 在 Windows 家庭版上执行 `clicker session`
- **THEN** 命令报错并退出，主桌面上没有 jev 在操作

#### Scenario: 已经有一个子会话
- **WHEN** 这台机器上已有一个子会话在运行，再次执行 `clicker session`
- **THEN** 命令报出已有会话在运行并退出，不创建第二个子会话

#### Scenario: 连不上本机
- **WHEN** 远程桌面被关闭，或本机不信任连向 `localhost` 的证书
- **THEN** 命令报出这个原因并退出，不在主桌面运行

### Requirement: 截图与输入只落在子会话
子会话里的 jev SHALL 只截取并操作该子会话的桌面。jev 在带有 `JEV_SESSION=1` 启动时，若发现自己仍在控制台会话里，MUST 在任何截图或输入之前拒绝运行。父会话里的控制端 MUST NOT 导入平台适配器，也 MUST NOT 向主桌面发送鼠标、键盘或滚动。

#### Scenario: 进程落在了控制台会话
- **WHEN** jev 带着 `JEV_SESSION=1` 启动，且自身进程在控制台会话里
- **THEN** jev 拒绝运行，没有截图，也没有输入

#### Scenario: 主桌面保持可用
- **WHEN** 子会话里的 jev 正在点击和打字
- **THEN** 这些输入不作用于主桌面，控制端本身没有发出任何输入

### Requirement: 子会话沿用当前用户
子会话 SHALL 以当前登录的同一用户运行，沿用该用户的账号与已装软件。除智能卡登录外，启动 MUST NOT 另要一次密码。

#### Scenario: 自动登录
- **WHEN** 当前用户用密码登录 Windows，并执行 `clicker session`
- **THEN** 子会话直接进入该用户的桌面，没有出现凭据提示

### Requirement: 控制端与子会话用命名管道通信
控制端 SHALL 创建一条只允许当前用户访问、并拒绝远程客户端的命名管道。子会话里的 jev 与控制端 MUST 用一行一个 JSON 对象通信，类型限于 `task`、`log`、`progress`、`question`、`answer`、`cancel`、`done`。控制端收到 `log` MUST 原样打印；收到 `done` MUST 展示结局与运行目录的位置。

#### Scenario: 下发任务
- **WHEN** 控制端沿管道发送一条 `task`
- **THEN** 子会话里的 jev 按其中的目标开始运行

#### Scenario: 管道不接受其他用户
- **WHEN** 检查管道的访问控制
- **THEN** 只有当前用户可以连接，远程客户端被拒绝

### Requirement: 提问与取消经管道转达
子会话里写手向用户提问时，`ctx.ask` SHALL 把问题写成 `question` 发给控制端并等待。控制端 MUST 从自己的标准输入读入回答，再以 `answer` 送回。用户在控制端取消时，控制端 SHALL 发送 `cancel`，子会话里的运行 MUST 走现有的中止路径。

#### Scenario: 写手提问
- **WHEN** 子会话里的写手提出一个问题
- **THEN** 问题出现在控制端，用户的回答被送回子会话，运行继续

#### Scenario: 用户取消
- **WHEN** 用户在控制端取消
- **THEN** 子会话里的运行按现有的中止路径停下，不再执行下一步

### Requirement: 启动失败不退回主桌面
在子会话里启动 jev 失败时，命令 SHALL 报错并退出。无论失败发生在复制令牌还是改由连接的启动程序拉起，MUST NOT 在控制台会话里运行 jev，也 MUST NOT 同时保留两种启动方式。

#### Scenario: 启动进程失败
- **WHEN** 子会话已经连上，但 jev 没能在其中启动
- **THEN** 命令报错退出，主桌面上没有 jev 在操作
