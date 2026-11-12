# waa-uia-tree

## Purpose

jev 在 WAA 的 Windows 虚拟机里如何取得前台应用的 UI Automation 树，并把它读成与真实 Windows 机器相同的控件、焦点字段与地址栏。

## Requirements

### Requirement: 只取前台窗口的树
每次观测，jev SHALL 在虚拟机内取前台窗口的 UI Automation 树，只从该窗口往下遍历，MUST NOT 遍历桌面上的其他窗口。jev MUST NOT 使用 WAA 自带的整桌面无障碍树接口。

#### Scenario: 背景窗口不出现
- **WHEN** 前台是 Chrome，背后开着记事本
- **THEN** 取到的树里没有记事本的任何节点

#### Scenario: 不请求整桌面树
- **WHEN** jev 跑完一个任务
- **THEN** 这期间没有对 WAA 无障碍树接口的请求

### Requirement: 不修改虚拟机
取树 SHALL 只使用 golden image 里已有的 Python 与库，MUST NOT 在虚拟机里安装软件包或写入文件。取树 MUST NOT 向虚拟机发送任何输入。

#### Scenario: 取树前后虚拟机不变
- **WHEN** jev 取一次树
- **THEN** 虚拟机里没有新增的软件包或文件，鼠标位置与焦点不变

### Requirement: 角色与真实 Windows 机器一致
树中每个节点的角色 SHALL 由它的 UI Automation 控件类型映射而来，使用与 jev 在真实 Windows 机器上相同的映射表；可按的判断 SHALL 与真实 Windows 机器的规则相同。两处的映射 MUST 来自同一份定义。

#### Scenario: 按钮
- **WHEN** 前台窗口里有一个控件类型为 Button、名称为「保存」的元素
- **THEN** 它以按钮角色、名称「保存」及其屏幕矩形出现在可点击元素中

#### Scenario: 映射一致
- **WHEN** 比较虚拟机内使用的映射与真实 Windows 机器使用的映射
- **THEN** 二者对每个控件类型给出相同的角色

### Requirement: 从树读出前台应用、焦点字段与地址栏
jev SHALL 从取到的树读出前台应用名、前台窗口矩形与焦点字段（角色、名称、值、矩形）。前台是 Chrome 或 Edge 时，SHALL 从地址栏编辑框读出当前网址。

#### Scenario: 地址栏
- **WHEN** 前台是 Edge，地址栏显示 `example.com/a`
- **THEN** jev 读到的网址是 `example.com/a`

#### Scenario: 焦点字段
- **WHEN** 焦点在一个已输入 `abc` 的编辑框里
- **THEN** jev 读到的焦点字段的值是 `abc`

### Requirement: 有限的耗时与规模
一次取树 SHALL 在有限的时间内结束，并对深度、每个节点的子节点数与总节点数设上限。超出上限时 SHALL 返回已取到的部分并标明被截断。

#### Scenario: 超大的树
- **WHEN** 前台窗口的节点数超过上限
- **THEN** 返回的树在上限处截断，并带有截断标记，这一步照常继续

#### Scenario: 超时
- **WHEN** 遍历超过时间上限
- **THEN** 遍历停止，这一步在时限内得到已取到的部分或没有树

### Requirement: 没有树时照常运行
取树失败、超时或前台没有窗口时，这一步 SHALL 视为没有树：应用、控件与焦点字段未知，感知管线仍读截图。失败 MUST NOT 让任务中止，也 MUST NOT 改用 WAA 的整桌面树。

#### Scenario: 取树失败
- **WHEN** 虚拟机内的遍历报错
- **THEN** 这一步的元素只来自截图，任务继续，失败原因记入 `run.json`

### Requirement: 取树不与动作重叠
一次观测的取树 SHALL 在这一步的动作发给虚拟机之前结束，取树期间 MUST NOT 有动作在虚拟机里执行。取树 SHALL 与截图的识别并行，读截图不等待树。

#### Scenario: 动作等待取树
- **WHEN** 取树尚未结束时 jev 已决定点击
- **THEN** 点击的动作在取树结束之后才交给 WAA

### Requirement: 取树的记录
每次取树的耗时、节点数、是否截断与失败原因 SHALL 记入该任务的 `run.json`。设置 `JEV_WAA_SAVE_TREE=1` 时，每次观测取到的原始树 SHALL 按观测序号保存在 jev 的运行目录中。

#### Scenario: 保存原始树
- **WHEN** 设置了 `JEV_WAA_SAVE_TREE=1` 并跑完一个任务
- **THEN** 运行目录里每次观测有一份原始树文件，文件名带从 000 开始的序号
