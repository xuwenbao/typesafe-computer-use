## 1. 操作方式的共享定义

- [x] 1.1 在 `uia_roles.py` 旁新增一份无 Windows 依赖的定义，列出 Toggle、ExpandCollapse、SelectionItem、ScrollItem、RangeValue、Value、Invoke 的标识与 pattern id。`windows.py` 与 `waa/walk_vm.py` 都引用它。验证：`tests/test_import_boundaries.py` 断言该模块不导入 jev 的其他模块，也不导入 pywin32、comtypes
- [x] 1.2 `AxNode` 增加带默认值的 `enabled` 与 `patterns`；`walk_actionable` 从适配器给出的操作方式里收下它们。`Item` 增加操作方式与状态（是否已勾选、是否已展开、当前值），`to_ax_items` 从节点抄过去。验证：现有构造 `AxNode` 的测试不用修改即可通过；一条新测试确认节点上的 Toggle 出现在对应项目上

## 2. 真实机器上的操作方式

- [x] 2.1 `windows.py` 采集控件时读 `IsEnabled` 与支持的 pattern，读不到是否可用就标成未知。验证：用伪造的 UIA 元素覆盖可用、不可用、读不到三种
- [x] 2.2 `Desktop` 增加 `ax_operate(ref, operation, value=None)`，`operation` 取 `toggle`、`select`、`expand_toggle`、`scroll_into_view`、`set_range`。`windows.py` 实现；`macos.py`、`NoDesktop` 与测试里的替身对全部返回做不到。验证：`tests/test_platform_adapter.py` 的协议检查通过
- [x] 2.3 在 `actions.py` 的 `click_item` 里按固定顺序选择：Invoke、有名字的默认动作、Toggle、Select、ExpandCollapse，都失败再鼠标点击。双击、右键、带修饰键的单击仍走 `click_at`。验证：一条测试用替身按顺序记录调用，覆盖「只有 Toggle」「列表项有默认动作」「都不支持」「Shift 单击」四个场景
- [x] 2.4 `press_offscreen` 与 `focus_offscreen` 对支持 ScrollItem 的控件先 `scroll_into_view`；屏幕上看得见的单击不滚动。验证：两条测试分别断言滚了与没滚
- [x] 2.5 明确不可用的控件，单击、按下与赋值都拒绝，包括鼠标回退，历史写明不可用；可用状态未知时保持今天的行为。验证：两条测试分别覆盖拒绝与照常点击

## 3. WAA 虚拟机里的同一套操作

- [x] 3.1 `waa/walk_vm.py` 在每个节点上输出是否可用与支持的 pattern id，读不到是否可用时输出未知。验证：伪造的 comtypes 接口覆盖复选框带 Toggle、不可用、以及没有该属性三种
- [x] 3.2 `waa/tree.py` 把这两个字段读到 `AxNode` 上。验证：用 `tests/fixtures/waa/` 里的 JSON 断言节点的可用状态与操作方式
- [x] 3.3 `WAADesktop.ax_operate` 经 `/execute` 在虚拟机内按名称和控件类型找回元素并调用 pattern，失败返回做不到，不安装软件包、不写文件。执行必须等这一步的取树结束。验证：单测检查生成的代码引用与 `uia_roles` 相同的 pattern id，并断言取树未完成时不会交出这段代码
- [x] 3.4 加一条测试：虚拟机与真实机器使用的操作方式标识来自同一模块。验证：测试通过；只改其中一处的 id 时测试失败

## 4. 接管一次给多步

- [x] 4.1 `config.py` 增加 `driver_batch`，默认 3，最小 1。`driver_steps` 的说明改为按执行的动作计数。验证：`tests/test_config_and_writer.py` 覆盖默认值、设为 1、以及小于 1 被拒绝
- [x] 4.2 `writer.py` 的接管回复改为一批动作：一个 `reason` 加列表，`done` 与 `give_up` 只能在最后。`DRIVE_SYSTEM` 改为说明最多 `driver_batch` 个动作，以及后一个动作必须点已经标号且来自无障碍树的项。列表为空、超长、名字不在名单里时整批作废。验证：解析测试覆盖合法的一批、`done` 在中间、以及超长
- [x] 4.3 `Desktop` 增加新鲜度查询，返回元素是否还在、是否可见、是否可用，问不到就返回空。`windows.py` 用 `IsEnabled` 与 `IsOffscreen` 回答；`macos.py` 只回答是否还在、是否可见。验证：伪造元素覆盖三项都在、已消失、以及 macOS 不报告可用
- [x] 4.4 `driver.py` 按顺序执行一批：现有校验之后做新鲜度核对；没有元素句柄的动作只允许排在第一个；被拒绝、核对失败、屏幕没变化、或遇到 `done` 与 `give_up` 时停掉剩下的。屏幕比较只用现有的分块比较，不跑 OCR 与图标检测。等待、输入、复制、剪切不参与这个判断。验证：`tests/test_driver.py` 覆盖控件消失、图标排在第二、屏幕没变、`done` 在最后、以及等待后屏幕没变仍继续
- [x] 4.5 预算只数执行了的动作，包括被拒绝的，`done` 与 `give_up` 不计数。历史里每个已执行动作一行 `driver: `，未执行的不写，提前停下时追加一行说明停在哪、为什么。验证：一条场景测试断言预算从 5 减到 3，且历史里没有第三个动作
- [x] 4.6 每次调用写一份 `step-NNN-driver.json`，`NNN` 是该批第一个动作的序号，内容含调用序号和每个动作的结果；下一次从已执行的动作之后继续。`run.json` 分别记录接管的动作数与调用次数。没有调用序号的旧文件按一次一个动作读取。验证：一条测试检查文件内容，另一条用旧格式的文件断言读取结果
- [x] 4.7 `WAADesktop` 的新鲜度查询先把已攒下的输入作为一个虚拟机步骤执行，拿回新的树再回答。一批动作不得在同一次虚拟机执行里连续发出。验证：`tests/test_waa_desktop.py` 断言第二个动作的核对发生在第一个动作交出之后
- [x] 4.8 接管模型看到的 `screen_items` 带上 `controls` 与当前状态。`DRIVE_ACTIONS` 增加 `set_value`：RangeValue 核对最小最大值并读回，Value 走现有设值，像凭据、只读、或不支持时拒绝。验证：`tests/test_driver.py` 覆盖滑块设为 40、设为 120 被拒绝、以及像密码的文本被拒绝

## 5. 隔离会话

- [x] 5.1 新增 `yuanji_computer_use/windows_session.py`，只在执行 `clicker session` 时导入。`cli.py` 增加该子命令。验证：`tests/test_import_boundaries.py` 断言 macOS 的导入路径不包含它；在非 Windows 上执行子命令的测试断言报错且没有启动进程
- [x] 5.2 实现前提检查：专业版、企业版或教育版，远程桌面能连 `localhost`，不是智能卡登录，没有另一个子会话。任一不成立就报出原因并退出，不在控制台会话运行 jev。验证：把检查拆成纯函数，单测逐条覆盖家庭版、已有子会话、连不上本机
- [x] 5.3 控制端启用子会话、嵌入远程桌面控件、创建只允许当前用户且拒绝远程客户端的命名管道。控制端不导入平台适配器。消息类型限于 `task`、`log`、`progress`、`question`、`answer`、`cancel`、`done`。验证：消息的编码与解码有单测覆盖全部七种类型；另有一条测试断言控制端模块不导入 `platform_adapter`
- [x] 5.4 子会话里的 `ctx.ask` 把问题写成 `question` 并等待 `answer`；`cancel` 走现有的中止路径。验证：用一对假管道覆盖提问得到回答、以及取消后 `check_abort` 触发
- [x] 5.5 连接成功后，从子会话的 `explorer.exe` 复制令牌，用 `CreateProcessAsUser` 启动 jev，带上 `JEV_SESSION=1` 与管道名。jev 带着该变量启动时，若发现自己在控制台会话，必须在任何截图或输入之前退出。启动失败就报错退出，不改在主桌面运行。验证：会话判断是纯函数，单测覆盖「在子会话」「在控制台会话」；跨会话启动本身留到下面的人工验证
- [x] 5.6 在 `tests/conftest.py` 的机器守护里加入本次所有新的会触及机器的调用：`ax_operate`、新鲜度查询、子会话的启用与连接、跨会话启动进程、命名管道的创建。验证：一条测试在没有打桩时调用它们，断言被拒绝

## 6. 文档与收尾

- [x] 6.1 更新 `docs/how-a-step-works.md`（接管一次多步、点击顺序、`set_value`）、`docs/writer-endpoints.md`（`driver_batch`）、`README.md`、`CONTRIBUTING.md`。新增隔离会话的使用文档，写明剪贴板共用、Office 不能两边同时开、开机自启动会再开一份、会话开着时不要重启。验证：文档里的配置名与 `config.py` 一致
- [x] 6.2 更新 `AGENTS.md`：`clicker session` 属于在这台机器上运行项目，每次都要单独批准。验证：该节能读到这条
- [x] 6.3 跑 ruff 与离线测试。验证：`uv run ruff check`、`uv run ruff format --check`、`uv run pytest` 通过，期间没有启动子会话，也没有真的连接管道
- [x] 6.4 校验这个 change。验证：`openspec validate windows-ufo2-borrowings --strict` 通过

## 人工验证

- 在一台 Windows 专业版机器上，经批准后执行 `clicker session`，确认复制 `explorer.exe` 令牌可以在子会话里启动进程。若跨会话打开进程被拒绝，就改成由连接的启动程序拉起 jev，并删掉复制令牌这条路，两条不并存。
- 用一批 WAA 任务比较 `driver_batch` 为 3 与为 1 的成功率、动作数和模型调用次数，再决定默认值要不要改。
