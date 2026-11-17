## ADDED Requirements

### Requirement: 点击按固定顺序选择操作方式
对来自无障碍树的项目做普通单击时，SHALL 按下面的顺序使用第一种可用的方式：Invoke；有名字的 Legacy 默认动作；Toggle；SelectionItem 的 Select；ExpandCollapse，折叠着就展开、展开着就折叠；最后是鼠标点击。前五种都不可用或执行失败时，MUST 回退到鼠标点击。这个顺序 MUST 写在一处，真实机器与 WAA 共用。macOS 上这些新方式 MUST 报告不支持，单击因此回退到鼠标。

#### Scenario: 只有 Toggle 的复选框
- **WHEN** 单击一个没有 Invoke、也没有有名字的默认动作、但支持 Toggle 的复选框
- **THEN** 执行 Toggle，不发送鼠标点击

#### Scenario: 列表项仍走默认动作
- **WHEN** 单击一个没有 Invoke、但有名为打开的默认动作、同时支持 SelectionItem 的列表项
- **THEN** 执行该默认动作，不调用 Select

#### Scenario: 都不支持时用鼠标
- **WHEN** 单击一个来自无障碍树、但上述方式都不支持的项目
- **THEN** 在该项的中心发送鼠标点击

### Requirement: 手势类点击不走操作方式
双击、右键、带修饰键的单击 SHALL 继续用鼠标发送，MUST NOT 使用 Invoke、Toggle、SelectionItem 或 ExpandCollapse。

#### Scenario: 按着 Shift 单击
- **WHEN** 接管对一个支持 SelectionItem 的项目做带延伸选择的单击
- **THEN** 发送带 Shift 的鼠标点击，不调用 Select

### Requirement: 明确不可用的控件直接拒绝
采集到的控件明确为不可用时，对它的单击、按下与赋值 MUST 被拒绝，包括鼠标回退，历史里 MUST 写明是因为不可用。读不到是否可用时，MUST 保持原来的行为，不因此拒绝。不可用的控件 SHALL 仍出现在项目列表中。

#### Scenario: 不可用的按钮
- **WHEN** 单击一个无障碍树标明不可用的按钮
- **THEN** 动作被拒绝，没有鼠标点击，历史写明不可用

#### Scenario: 读不到是否可用
- **WHEN** 一个控件没有报出是否可用，其余条件与今天相同
- **THEN** 单击按今天的方式执行

### Requirement: 视野外的控件先滚进视野
`press_offscreen` 与 `focus_offscreen` 操作一个支持 ScrollItem 的控件之前，SHALL 先把它滚进视野。屏幕上看得见的单击 MUST NOT 为了操作而滚动页面。

#### Scenario: 按下视野外的控件
- **WHEN** 按下列表中一个不在视野内、且支持 ScrollItem 的控件
- **THEN** 先把该控件滚进视野，再按下

#### Scenario: 看得见的单击不滚动
- **WHEN** 单击一个已经在屏幕上、且支持 ScrollItem 的项目
- **THEN** 不滚动页面

### Requirement: 接管模型可以给控件赋值
接管模型的动作名单 SHALL 包含 `set_value`，用项目编号与一段文本。目标支持 RangeValue 时，MUST 把文本解析成数值，核对它落在该控件的最小值与最大值之间，设置后读回；读回与设定不一致时 MUST 记为失败。目标支持 Value 而不支持 RangeValue 时，MUST 用现有的设值并读回。两者都不支持、文本像凭据、或控件只读时，MUST 拒绝。分类器的动作集合 MUST 保持不变，文本仍由它现有的输入动作处理。

#### Scenario: 给滑块设值
- **WHEN** 接管对一个范围为 0 到 100 的滑块给出 `set_value`，文本是 `40`
- **THEN** 滑块的值变为 40，历史写明通过 RangeValue 设值

#### Scenario: 超出范围
- **WHEN** 接管对同一个滑块给出的文本是 `120`
- **THEN** 动作被拒绝，滑块的值不变

#### Scenario: 分类器没有这个动作
- **WHEN** 检查分类器的动作集合
- **THEN** 其中没有 `set_value`

### Requirement: 模型看得到控件的操作方式与状态
来自无障碍树的项目 SHALL 把它支持的操作方式带进接管模型看到的项目列表。开关 MUST 带上是否已勾选，可展开的控件 MUST 带上是否已展开，有当前值的控件 MUST 带上当前值。这些字段 MUST 来自采集控件时读到的事实。

#### Scenario: 复选框的状态
- **WHEN** 屏幕上有一个已勾选、支持 Toggle 的复选框
- **THEN** 接管模型收到的该项带有 Toggle，并标明已勾选

### Requirement: 操作方式的定义只有一份
Toggle、ExpandCollapse、SelectionItem、ScrollItem、RangeValue 的标识与 UI Automation 的 pattern id MUST 定义在一处，且不依赖 Windows 的库。真实机器的适配器与 WAA 虚拟机内的遍历 MUST 引用这一份。`walk_actionable` 采集到的操作方式与是否可用，MUST 随节点带到项目上。

#### Scenario: 两处使用同一份标识
- **WHEN** 比较真实机器与虚拟机遍历脚本引用的操作方式标识
- **THEN** 二者来自同一个模块，对每种操作方式给出相同的 pattern id
