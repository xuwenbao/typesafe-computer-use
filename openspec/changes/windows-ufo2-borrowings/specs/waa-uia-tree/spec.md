## MODIFIED Requirements

### Requirement: 角色与真实 Windows 机器一致
树中每个节点的角色 SHALL 由它的 UI Automation 控件类型映射而来，使用与 jev 在真实 Windows 机器上相同的映射表；可按的判断 SHALL 与真实 Windows 机器的规则相同。两处的映射 MUST 来自同一份定义。每个节点还 SHALL 带上是否可用，以及它支持的操作方式（Invoke、Toggle、ExpandCollapse、SelectionItem、ScrollItem、RangeValue、Value 中它实际支持的那些）。是否可用与操作方式的标识 MUST 与真实 Windows 机器来自同一份定义。读不到是否可用时，节点 MUST 标明未知，而不是写成不可用。

#### Scenario: 按钮
- **WHEN** 前台窗口里有一个控件类型为 Button、名称为「保存」的元素
- **THEN** 它以按钮角色、名称「保存」及其屏幕矩形出现在可点击元素中

#### Scenario: 映射一致
- **WHEN** 比较虚拟机内使用的映射与真实 Windows 机器使用的映射
- **THEN** 二者对每个控件类型给出相同的角色

#### Scenario: 不可用与操作方式一并带回
- **WHEN** 前台窗口里有一个不可用的复选框，它支持 Toggle
- **THEN** 取回的节点标明不可用，并带有 Toggle

#### Scenario: 读不到是否可用
- **WHEN** 一个节点没有报出是否可用
- **THEN** 该节点的可用状态是未知，遍历不因此失败

## ADDED Requirements

### Requirement: 在虚拟机里执行操作方式
jev 在 WAA 虚拟机里执行 Toggle、ExpandCollapse、SelectionItem、ScrollItem、RangeValue 与 Value 时，SHALL 通过 WAA 的 `/execute` 在虚拟机内运行一段 Python，按名称与控件类型找回元素再调用对应的 pattern。这段执行 MUST NOT 在虚拟机里安装软件包或写入文件。一次观测里，操作方式的执行 MUST NOT 与取树重叠：取树结束之前不得执行，执行期间不得再取树。pattern 不存在或调用失败时，MUST 向调用方返回失败，由调用方决定下一步，MUST NOT 让任务中止。

#### Scenario: 复选框在虚拟机里切换
- **WHEN** 接管对虚拟机里一个支持 Toggle 的复选框执行切换
- **THEN** 虚拟机内通过 `/execute` 调用了 Toggle，没有安装软件包

#### Scenario: 执行失败不中止任务
- **WHEN** 目标元素在执行时已经不存在
- **THEN** 这次操作返回失败，任务继续

#### Scenario: 不与取树重叠
- **WHEN** 同一步里既要取树又要执行操作方式
- **THEN** 取树先结束，执行在取树完成之后开始
