# Spec Delta

## Purpose

分类器彻底停在目标之前时，由一个更强的模型接管这次运行：它在多轮对话里看屏幕、读取和计算、一次执行一个界面动作，直到完成、放弃或用完预算。规格写明接管的条件、模型接口、工具、保护、隐私和记录。

## ADDED Requirements

### Requirement: 接管的条件
分类器停在目标之前、写手判断目标未完成且去向不是结束时，驱动 SHALL 接管，前提是：真操作（不是演练或重放），`CLICKER_DRIVER` 未设为 `false`，`CLICKER_DRIVER_STEPS` 大于 0，且写手可用。任一条件不满足时 MUST NOT 接管，运行停在分类器的结局上。

#### Scenario: 分类器置信度不足后接管
- **WHEN** 真操作中分类器因置信度低于阈值停下，写手判断目标未完成并给出去向 driver
- **THEN** 驱动接管，日志写出接管原因和最多可执行的界面动作数

#### Scenario: 演练不接管
- **WHEN** 不带 `--act` 运行，分类器停下且目标未完成
- **THEN** 驱动不接管，结局保持分类器的结局

#### Scenario: 关闭驱动
- **WHEN** `CLICKER_DRIVER=false`，分类器停下且目标未完成
- **THEN** 驱动不接管

### Requirement: 只支持 OpenAI 接口的模型
驱动 SHALL 通过 OpenAI Chat Completions 接口调用 `CLICKER_DRIVER_MODEL`（未设时用回答模型），端点为 `CLICKER_WRITER_BASE_URL` 或 OpenAI 官方地址。`CLICKER_WRITER_API=anthropic` 时驱动 MUST NOT 接管，并在日志里写明需要 `CLICKER_WRITER_API=openai`。分类器、写手和最终回答的接口支持不受影响。

#### Scenario: Anthropic 接口
- **WHEN** `CLICKER_WRITER_API=anthropic`，分类器停下且目标未完成
- **THEN** 驱动不接管，日志有一行说明驱动需要 `CLICKER_WRITER_API=openai`，写手仍给出最终回答

#### Scenario: OpenAI 兼容端点
- **WHEN** `CLICKER_WRITER_API=openai`，`CLICKER_WRITER_BASE_URL` 指向一个 OpenAI 兼容的本地端点
- **THEN** 驱动的请求发往该端点

### Requirement: 密钥只发往所属端点
驱动的请求 SHALL 沿用写手的密钥规则：`OPENAI_API_KEY` 只发往 `api.openai.com`；其他端点只使用 `CLICKER_WRITER_API_KEY`，未设时用占位符。驱动 MUST NOT 把任何密钥发往不属于它的端点。

#### Scenario: 自建端点
- **WHEN** `CLICKER_WRITER_BASE_URL` 是自建地址，`CLICKER_WRITER_API_KEY` 未设，`OPENAI_API_KEY` 已设
- **THEN** 发往该地址的驱动请求不带 `OPENAI_API_KEY`

### Requirement: 不上传运行轨迹
驱动 MUST NOT 把对话、屏幕内容、工具参数或结果上传到模型端点以外的任何服务，包括 SDK 自带的 tracing。

#### Scenario: 一次完整接管
- **WHEN** 驱动完成一次接管
- **THEN** 网络请求只发往配置的模型端点

### Requirement: 一次接管是一段多轮对话
一次接管内，模型 SHALL 能看到本次接管中自己此前的每一轮工具调用、工具结果，以及每个界面动作的执行结果。第一轮 SHALL 提供目标、分类器做过的动作、分类器为什么停下、写手此前给过的提示和之前屏幕的文字。

#### Scenario: 先数后写
- **WHEN** 模型在第一张屏幕上用计数工具得到 22，之后经过三个界面动作来到另存为对话框
- **THEN** 第四张屏幕时模型的输入里仍有计数结果 22

### Requirement: 每张屏幕只执行一个界面动作
模型每看到一张屏幕，驱动 SHALL 最多执行一个界面动作（点击、双击、右键、输入、按键、快捷键、滚动、拖动、设值、移焦点、等待），执行后重新截图识别，再把新屏幕交给模型。同一轮里多出来的界面动作 MUST NOT 执行，并以「refused」开头告诉模型。

#### Scenario: 一轮里两个界面动作
- **WHEN** 模型在同一轮里同时调用点击和输入
- **THEN** 只执行点击；输入不执行，模型收到以 refused 开头、说明每张屏幕只做一个动作的结果；日志记一行

### Requirement: 界面动作沿用现有的执行与拒绝
界面动作的含义、参数限制和拒绝规则 SHALL 与现在的驱动相同：目标文字必须与元素一致；按键只能是允许的键；快捷键不能是 q、w、h、m；输入不超过 2000 字；按像素拖动不超过 600。被拒绝的动作 MUST NOT 碰机器，拒绝原因写进下一张屏幕给模型看。

#### Scenario: 目标与元素不符
- **WHEN** 模型点击第 12 号元素，但给出的目标文字与第 12 号元素的文字不符
- **THEN** 不点击，下一张屏幕的上一个动作写明被拒绝和原因

#### Scenario: 被禁止的快捷键
- **WHEN** 模型调用快捷键 w
- **THEN** 不按键，模型收到拒绝原因

### Requirement: 读取元素原文
驱动 SHALL 提供读取当前屏幕某个元素原文的工具，优先从辅助功能接口读控件的文本或值，读不到时返回识别出的文字。结果 MUST 注明来源（辅助功能或识别）和长度，超过上限时截断并注明。该工具 MUST NOT 移动鼠标、按键或改变屏幕。

#### Scenario: 记事本编辑区
- **WHEN** 记事本已打开 `largefile.txt`，模型读取编辑区元素的原文
- **THEN** 返回文件的完整文字，来源为辅助功能，`example` 出现 22 次

#### Scenario: 控件不提供文本
- **WHEN** 被读的元素在辅助功能接口里没有文本或值
- **THEN** 返回识别出的文字，并注明来源为识别

### Requirement: 计数与算术
驱动 SHALL 提供计数工具：统计一个词在某个元素原文或给定文字中出现的次数，可选整词匹配和区分大小写；以及算术工具：只计算数字、四则运算、整除、取余、乘方和括号组成的表达式，其他输入 MUST 返回错误而不执行。两者 MUST NOT 碰机器。

#### Scenario: 对元素原文计数
- **WHEN** 模型对记事本编辑区元素统计 `example`，整词匹配、不区分大小写
- **THEN** 结果是 22

#### Scenario: 不合法的表达式
- **WHEN** 模型让算术工具计算 `__import__('os')`
- **THEN** 返回错误，不执行任何代码

### Requirement: 读取与计算不占动作预算
读取与计算工具 SHALL NOT 计入 `CLICKER_DRIVER_STEPS`。模型在同一张屏幕上连续调用读取与计算工具 MUST 有上限；超过上限算一次不可用的回复。

#### Scenario: 多次读取后点击
- **WHEN** 模型在一张屏幕上调用读取 1 次、计数 2 次，然后点击
- **THEN** 本次接管的已用动作数只增加 1

### Requirement: 结束接管
模型 SHALL 用「完成」或「放弃」结束接管，并写出理由。「完成」时结局为 `driver done`，「放弃」时为 `driver gave up`。完成的理由否认当前屏幕列出的名字时，这次「完成」MUST NOT 生效，模型在不带截图的情况下再判断一次。

#### Scenario: 放弃
- **WHEN** 模型调用放弃并说明缺少的东西
- **THEN** 接管结束，结局为 `driver gave up`，写手随后给出最终回答

### Requirement: 动作预算
一次接管执行的界面动作（包括被拒绝的）达到 `CLICKER_DRIVER_STEPS` 时，接管 SHALL 结束，结局为 `driver step limit`。

#### Scenario: 用完预算
- **WHEN** `CLICKER_DRIVER_STEPS=3`，模型连续选了 3 个界面动作仍未结束
- **THEN** 接管在第 3 个动作后结束，结局为 `driver step limit`

### Requirement: 防打转
驱动 SHALL 沿用现有止损：连续三个动作屏幕不变、同一屏幕上连续重复旧动作、同一动作连续四次，或同一屏幕上同一动作累计三次时，第一次要求模型换路并在该屏幕上禁止打转的动作；本次接管已换过一次路时，再触发就结束，结局为 `driver stalled`。

#### Scenario: 换路后仍打转
- **WHEN** 模型被要求换路后，在同一屏幕上又连续三个动作屏幕都不变
- **THEN** 接管结束，结局为 `driver stalled`

### Requirement: 不可用的回复与请求失败
一张屏幕上模型没有调用界面动作、完成或放弃（只回文字，或读取与计算超过上限）时，驱动 SHALL 提醒一次再问；连续两次不可用时结局为 `driver failed`。端点报错（包括不支持工具调用）时结局 MUST 为 `driver failed`，日志写明错误；端点只是拒绝推理强度参数时，SHALL 去掉该参数重试。

#### Scenario: 端点不支持工具
- **WHEN** 端点对带工具的请求返回错误
- **THEN** 接管结束，结局为 `driver failed`，日志写明端点返回的错误

### Requirement: 叫停
接管中按 Ctrl-C，或关闭画中画窗口，运行 SHALL 以 `aborted` 结束。叫停发生在模型请求进行中时，请求返回后 MUST NOT 再执行任何界面动作。

#### Scenario: 画中画关窗
- **WHEN** 接管进行中用户关闭画中画窗口
- **THEN** 运行以 aborted 结束，不再有动作发出

#### Scenario: 请求中叫停
- **WHEN** 模型请求尚未返回时用户关闭画中画窗口，随后请求返回了一个点击
- **THEN** 不执行这个点击，运行以 aborted 结束

### Requirement: 画中画中的动作落在子会话桌面
在画中画子会话里，驱动的截图、界面动作和元素原文读取 SHALL 都作用于子会话的输入桌面。

#### Scenario: 子会话里点击
- **WHEN** 画中画里驱动点击一个元素
- **THEN** 点击发生在子会话的桌面上，控制台桌面不受影响

### Requirement: 截图保留
发给模型的对话 SHALL 只带最近两张屏幕的截图和元素列表；更早的屏幕只保留应用名、焦点和上一个动作的结果。读取与计算的结果 MUST 保留在对话里。`CLICKER_WRITER_VISION=false` 时不发送截图。

#### Scenario: 第五张屏幕
- **WHEN** 接管来到第五张屏幕
- **THEN** 发给模型的输入里只有第四、第五张屏幕的截图，前三张屏幕只剩文字摘要

### Requirement: 运行目录记录
每张屏幕 SHALL 在运行目录留下发给模型的截图和一份记录：选定的界面动作或结束、执行结果，以及这张屏幕上每次工具调用的名称、参数和结果（过长的结果截断）。`run.json` SHALL 记录驱动的界面动作数、模型请求数、各工具的调用次数和 token 用量。

#### Scenario: 记事本计数
- **WHEN** 模型在一张屏幕上调用读取和计数后选择新建标签页
- **THEN** 该屏幕的记录里依次有读取、计数和快捷键三项，以及快捷键的执行结果；`run.json` 的工具调用次数里读取和计数各为 1
