## Context

动机见 proposal.md 的 Why。这里只写设计要依赖的现状。

- **主循环。** `runner.run` 先调 `host.prepare`，再循环调 `run_step`。`run_step` 返回假时调 `hand_off`：返回真就回到分类器，返回假就跳出循环。之后由 `wants_driver` 决定要不要调 `driver.take_over`，接管完再调 `answer_last`。“谁接手”的信息分散在三处：这几个函数的返回值、`state.outcome`（停下的原因，写进 `run.json`）、`state.route`（写手给的去向）。
- **分类器的一步。** `run_step` 一个函数做完全部事情：截屏、识别、`screen_moved`、写四个 `step-NNN-*` 文件、`decide`、记日志、`resolve`（门槛判断、`perform`、`repeating`），最后计时并 `settle`。
- **运行状态。** `RunState` 有 40 多个字段：分类器的空转和重复计数、写手的交回记录、接管的计数和换路状态都在里面。`driver.py` 在 7 处函数里懒导入 `runner`，用来拿 `screen_moved`、`tried_here` 等函数，因为顶层互相导入会成环。
- **对外接口。** `cli.py`、`helper/service.py`、`waa/agent.py` 只用 `RunConfig` 和 `run`。16 个测试文件直接引用 `runner` 的内部函数。
- **判定依据。** 分类器的每个 Choice 都返回完整的概率分布。`_leads` 只在低于门槛时看第一名是否领先第二名 2 倍。屏幕指纹 `signature` 是（应用，网址，焦点，每行文字及其所在行），不含控件状态。Windows 适配器已经把 `ToggleState`、`ExpandCollapseState`、取值写进 `Item.checked`、`expanded`、`current`；macOS 不读这些状态。
- **参考的 UFO2 代码**（`microsoft/UFO`，MIT）：
  - `ufo/agents/agent/basic.py`：`BasicAgent`、`AgentRegistry`。
  - `ufo/agents/agent/host_agent.py`：`AgentFactory`。
  - `ufo/agents/states/`：`AgentState`、`AgentStateManager`，每个状态一个单例类。
  - `ufo/agents/processors/core/processor_framework.py`：`ProcessorTemplate`，按数据收集、模型交互、动作执行、记忆更新四个阶段装配策略，外面套中间件，并校验策略之间声明的依赖。
  - `ufo/module/basic.py`：`BaseRound.run`，循环执行“`agent.handle` → `next_state` → `next_agent`”。

## Goals / Non-Goals

**Goals:**

- 交接规则集中到一张状态转移表里。加一个角色，只需要注册一个 agent、在表里加几条边。
- 分类器的一步变成四个可替换的阶段，规划层通过换策略改变行为，不在主循环里加分支。
- 阶段一结束时，外部可见的结果与重构前完全一致（见 `run-lifecycle` 规格），这样阶段二的行为变化可以单独归因。
- 阶段二的每个机制都可以单独关掉：`planner=0` 等于阶段一的行为。

**Non-Goals:**

- 异步执行。jev 一次只操作一台机器，整个流程是同步的；中止靠 `desktop.check_abort`，不靠取消协程。
- 每个状态一个类、单例的状态管理器、策略声明依赖与产出并在运行时校验。jev 只有四个 agent、四个阶段，用一张表加类型检查就够了。
- 多 agent 并发和 UFO2 的黑板。jev 同一时刻只有一个 agent 在动。
- 改动浏览器路径（`browser/runner.py`）的循环。

## Decisions

### 1. 包结构

新建 `yuanji_computer_use/agents/`：

| 模块 | 内容 |
|---|---|
| `status.py` | `Status` 枚举与 `TRANSITIONS` 状态转移表 |
| `session.py` | `Session`（由 `RunState` 搬来，字段名不变，另加规划相关字段）、`StepContext` |
| `base.py` | `Agent` 协议、`register` 装饰器、`AgentFactory` |
| `strategies.py` | 四个阶段的策略接口与默认实现 |
| `middleware.py` | `StepMiddleware` 接口与四个中间件 |
| `host.py`、`policy.py`、`writer.py`、`driver.py` | 四个 agent，各自薄薄一层，调用现有模块 |
| `planner.py` | 阶段二：计划、子目标、断言、空间过滤，以及登记为 `plan` 的规划 agent |

`agents/driver.py` 包装的是现有的 `yuanji_computer_use/driver.py`，不搬动它：`take_over` 内部逻辑多、测试多，这次只改它导入公共函数的位置。现有的 `host.py`、`decide.py`、`actions.py`、`writer.py` 也留在原处，agent 只是调用它们。

不放进一个 `ufo/` 风格的深层目录（`agents/agent/`、`agents/processors/strategies/`），因为 jev 的包是平铺的，每个概念一个文件就够。

### 2. Agent、注册表与工厂

```python
class Agent(Protocol):
    name: str

    def handle(self, session: Session) -> Status: ...
```

`register("policy")` 装饰器把类登记到 `AgentFactory._registry`。`AgentFactory.create(name)` 按名字取类并实例化；注册表里找不到就报错。

接管的 agent 在注册表里登记的是“类所在的模块路径”，不是类本身。`create("driver")` 时才导入这个模块。这样 Agents SDK 仍然只在接管真正发生时才加载，`tests/test_import_boundaries.py` 和冻结构建（打包后的程序可能缺 SDK 读的数据文件）的行为都不变。导入失败时，接管 agent 返回 `FINISH`，并把原因写进 `session.driver_error`，与今天相同。

与 UFO2 的区别：UFO2 的工厂把 `host`、`app` 等类型写在 if 分支里，另有注册表兜底。jev 只走注册表，避免两套查找。

### 3. 状态与状态转移表

控制流和停下的原因分开：

- `Status` 只回答“下一步该谁”：`HOST`、`POLICY`、`PLAN`、`WRITER`、`DRIVER`、`FINAL_ANSWER`、`FINISH`。
- `session.outcome` 继续回答“为什么停”，取值与今天完全一样（`done`、`low confidence`、`stalled`、`driver done` 等），`run.json` 照写不误。
- `session.route` 继续记写手给的去向。

主循环：

```python
status = Status.HOST
while status is not Status.FINISH:
    agent = factory.create(AGENT_FOR[status])
    nxt = agent.handle(session)
    assert nxt in TRANSITIONS[status], (status, nxt)
    status = nxt
```

阶段一的转移表，每条边对应今天代码里的一个分支：

| 从 | 到 | 今天对应的分支 |
|---|---|---|
| `HOST` | `POLICY` | `prepare` 之后总进入循环 |
| `POLICY` | `POLICY` | `run_step` 返回真 |
| `POLICY` | `WRITER` | `run_step` 返回假（分类器停下、步数用完） |
| `POLICY` | `FINISH` | 截屏超时、演练 |
| `WRITER` | `POLICY` | `hand_off` 返回真 |
| `WRITER` | `DRIVER` | `hand_off` 返回假，且 `wants_driver` |
| `WRITER` | `FINISH` | 其余情况 |
| `DRIVER` | `FINAL_ANSWER` | `take_over` 返回真 |
| `DRIVER` | `FINISH` | `take_over` 返回假、导入失败 |
| `FINAL_ANSWER` | `FINISH` | `answer_last` 之后 |

“步数用完”今天写在 `for ... else` 里，改为由分类器 agent 在 `session.step` 超过 `cfg.steps` 时把 `outcome` 设为 `step limit`，然后返回 `WRITER`。写手 agent 收到 `step limit` 时，`may_resume` 按今天的规则为假。

中止（Ctrl-C、`Abort`）仍然是异常，由主循环外层的 `try` 接住，和今天一样写 `aborted (...)`。异常不进转移表：它可能出现在任何 agent 里，而表只描述正常交接。

阶段二加三条边：`POLICY → PLAN`（首步分差小，或分类器停下且还能规划）、`PLAN → POLICY`（得到计划）、`PLAN → WRITER`（规划失败或次数用完）。断言不单独作为一个状态：它是分类器一步之内的一个阶段（见决策 5），不需要换 agent。

转移表上的断言（上面的 `assert`）只是开发期的保护。测试逐条覆盖每条边，确认非法转移会被发现。

与 UFO2 的区别：UFO2 每个状态是一个类，状态自己决定 `next_agent` 和 `next_state`。jev 由 agent 返回下一个状态，转移表检查它是否合法。这样“谁决定去哪”只有一处，表本身就是一份可以对照阅读的交接说明。

### 4. Session 与 StepContext

- `Session`：就是现在的 `RunState`，搬到 `agents/session.py`，字段名和默认值都不变，`driver.py` 照旧通过 `state.xxx` 访问。另外几个从 `run()` 局部变量提上来的字段：`cfg`、`ctx`、`log`、`step`。阶段二再加 `plan`、`subgoal`、`replans`、`widened` 和 `verdicts`。
- `StepContext`：一步之内用的数据，包括 `step`、`prefix`、`screen`、`items`、`tried`、`picture`、`decision`、`what`、`timing`、`keep_going`。它在各阶段之间传递，一步结束就丢掉。

不把 `Session` 拆成多个子对象（分类器的、写手的、接管的），是因为 `driver.py` 有几十处直接读写这些字段。拆开就得改接管模块，超出阶段一的“只搬不改”。

### 5. 分类器一步的四个阶段

```
StepPipeline.run(session, sc):
  for mw in middleware: mw.before(session, sc)
  perceive.run(session, sc)  -> 截屏、识别、screen_moved、tried_here；没变化太多次就停
  decide.run(session, sc)    -> 标号图、decide()；或者停下：done / none / 低置信
  act.run(session, sc)       -> perform；没有 --act 时演练
  memory.run(session, sc)    -> 写历史、report_action、repeating
  [阶段二] verify.run(session, sc) -> 断言当前子目标
  for mw in reversed(middleware): mw.after(session, sc)
```

- 每个阶段是一个带 `run(session, sc)` 方法的对象，可以通过设置 `sc.keep_going = False` 和 `session.outcome` 让这一步提前结束。后面的阶段检查 `keep_going`，为假就跳过。
- 默认实现逐行搬自 `run_step`、`resolve`、`screen_moved`、`repeating`，日志的文字和顺序不变。
- `PolicyAgent` 每一步按当前子目标装配阶段。阶段一总是用默认的四个。

与 UFO2 的区别：UFO2 的阶段之间通过 `ProcessingContext` 的键值对传数据，并声明依赖再做校验。jev 用一个带类型的 `StepContext` 数据类，字段写错在类型检查和测试里就会暴露，不需要运行时校验。

### 6. 中间件

```python
class StepMiddleware(Protocol):
    def before(self, session, sc) -> None: ...
    def after(self, session, sc) -> None: ...
    def on_error(self, session, sc, error) -> None: ...
```

阶段一有四个中间件，按顺序：

1. `Timing`：开始和结束时计时，写 `timing["total"]`，并把 `sc.timing` 追加到 `session.timings`。各阶段内部的 `phase(timing, ...)` 计时保持不变。
2. `StepFiles`：写 `step-NNN-raw.png`、`-sent.png`、`-payload.txt`、`step-NNN.png`、`-answers.json`，内容和写入时机与今天一致：截图、标号图和发出的请求在决策前写，标注图和答案在动作后写。所以它还有一个 `after_decide` 钩子，由流水线在决策阶段之后调用。
3. `SessionReport`：把 `sc.what` 交给 `windows_session.report_action`。
4. `StepLog`：写 `step N: app=... kind=...` 那一段日志和 `files:` 那一行。

`after` 按相反顺序执行。`on_error` 只用于计时：一步中途抛出异常时，仍然把已用时间记下，再把异常抛出去。

为什么是中间件，而不是让阶段自己写：写文件、计时、上报都是每一步都要做、和决策无关的事。阶段二换掉决策或执行策略时，不该再重复写一遍这些。

宿主、写手和接管不走这条流水线。写手的 `step-NNN-review.json` 和接管的 `step-NNN-driver.json` 仍然由各自的模块写。

### 7. 规划器（阶段二）

**何时规划。**

- 首步分类器决策之后，合并同义候选后的分差 ≥ `plan_skip_margin`（0.3），就直接执行，之后不再主动规划。分差更小时，这一步的动作不执行，直接转到 `PLAN`。
- 分类器在任何一步停下（低置信、没有可用动作、卡住），且 `session.replans < max_replans`，就转到 `PLAN`；首步跳过了规划也一样，因为卡住正是规划最有用的时候。
- 分类器说 `done` 或步数用完时，照旧交给写手。
- 第一次规划不算重规划，所以一次运行最多调用规划 1 + `max_replans` 次。

**规划调用。** `writer.compose_plan(writer, goal, screen, items, history, guidance)`，结构化输出，校验方式同 `compose_host`。输入沿用分类器 `base_state` 的字段，每个条目另带 `role`、`checked`、`current`、`expanded`，并附上 `previous_subgoals`（之前子目标及其断言结果）。输出：

```json
{
  "goal_state": {"space": {"controls": ["toggle"], "label": "TUN|虚拟网卡"}, "require": {"checked": false}},
  "subgoals": [
    {"do": "click", "space": {"controls": ["toggle"], "label": "TUN|虚拟网卡"},
     "verify": {"checked": false}, "on_fail": "widen"}
  ]
}
```

- `do` 取分类器已有的动作：`click`、`open`、`press_offscreen`、`scroll`、`press_enter`、`press_escape`、`go_back`、`wait`，再加一个 `ensure`：只断言、不行动，用来确认已经到了某一屏。
- `space.role` 取 `models.ROLE_WORDS` 里的短词（`button`、`checkbox`、`radio`、`slider`、`popup`、`field`、`link`……），不是平台的原始角色名。短词里没有“开关”：Windows 上的开关常常是带 Toggle 的 `button`，自绘的开关在 macOS 上也是 `button`。
- `space.controls` 取 `uia_patterns` 里的操作方式名（`toggle`、`expand_collapse`、`selection_item`、`range_value`、`value`、`invoke`），对应 `Item.controls`。开关该用它来认，而不是用 `role`：行标题和真开关的角色可能一样，但只有真开关带 `toggle`。macOS 的条目没有 `controls`，所以在 macOS 上这一项不参与过滤（视为匹配）。
- `space.role` 与 `space.controls` 同时给出时，满足其中一个即可。
- `space.label` 是正则，不区分大小写，同时匹配条目的 `text` 和 `beside`（同一行的邻居文字）。没有标签的开关，名字通常来自同一行的文字。
- `verify` 与 `require` 只能引用 `checked`、`expanded`、`current`、`exists` 这几个键。
- 每次最多 3 个子目标。累计子目标数超过 `max_subgoals`（5）时，计划在超出的那一个之前截断。
- 回复读不成计划（JSON 不合法、`do` 不在名单里、正则编译不了）时，不重试，返回 `WRITER`，并在历史里记一行 `planner: unusable plan`。

**执行计划。** `PolicyAgent` 按当前子目标装配阶段：

- `ensure`：只跑感知和断言两个阶段。成立就前进到下一个子目标，不成立就重规划。
- 其余子目标：决策阶段换成 `SpaceDecide`，在执行和记忆阶段之后加 `VerifyStrategy`。
- 子目标的 `do` 不约束分类器选什么动作。分类器仍然自己选；`do` 只用来决定换按法时换成什么。

**断言的结果。** 计划的 `goal_state` 在每一步的断言阶段之后检查一次。成立就把 `outcome` 设为 `done` 并交给写手：写手仍然负责回答用户；它看到断言成立，就不必再为“完成没有”读一遍文案。写手的提示词里加一项 `goal_state_held: true`。

**重规划。** 子目标的断言为假，且换过按法也没用，或者分类器停下时，`replans + 1`，回到 `PLAN`，并带上 `previous_subgoals`。`replans` 达到 `max_replans` 后，后续停下都按阶段一的方式交给写手。

### 8. 动作空间与放宽（阶段二）

`SpaceDecide` 先用子目标的 `space` 过滤候选，过滤后的候选交给 `decide(..., candidates=subset)`，`item_question` 只列这些条目，编号保持原样。放宽有三级：

1. `role` 或 `controls` 匹配，且 `label` 匹配。
2. 只匹配 `label`。
3. 全部候选（今天的行为）。

触发放宽的条件：这一级一个候选都没有；或者 `decide` 返回后，合并分差 < `decide_margin`（0.15）。每升一级就重问一次分类器（每次约 0.8 秒），`session.widened` 加一。到第 3 级还是分差太小，就按今天的门槛规则处理（包括 `_leads` 的放行）。

为什么必须能放宽：规划器在看到无障碍树之前写出的 `space` 只是猜测。TUN 那次运行里，真开关的角色就是 `button`，而人写的过滤条件是 `checkbox`。不能放宽的过滤会把正确答案删掉。

`kind` 问题（做哪类动作）不过滤。只过滤 `item`。

### 9. 合并同义候选后的分差（阶段二）

`merged_margin(answer, items)`：把 `item` 的概率按“同一目标”合并后，取第一名减第二名。两个条目算同一目标，需要满足两条之一：

- 框的交并比 ≥ 0.5。`ax+ocr` 的合并漏掉的同一控件属于这种。
- 标签相同（去掉空白、不区分大小写），且垂直中心相差不到半个行高。即同一行里同名的行标题和开关。

合并后的分差用于两处：首步决定是否跳过规划（≥ 0.3 就跳过），`SpaceDecide` 决定是否放宽（< 0.15 就放宽）。

不用合并前的分差，原因是 `docs/how-a-step-works.md` 记录的实测：卡住都来自两个意思相同的选项分票。两个同义候选任选一个都能点中，分差小不代表模型拿不准。如果点中的那个只是行标题而不是开关，下面的换按法和断言会把这种情况接住。

这只影响规划层。没有计划时（`planner=0`，或首步跳过了规划），`resolve` 的门槛规则保持阶段一的样子。

### 10. 屏幕指纹与换按法（阶段二）

**指纹。** `signature` 的第四个元素从 `(text, row)` 扩展为 `(text, row, state)`。`state` 是来自无障碍树的条目的 `(checked, expanded, current)`，其余条目为空。`same_screen` 的比较规则不变。效果：开关翻转后，那一行算作变了，屏幕不会被误判成“没变化”。

这个改动在 `planner=0` 时也生效，因为它修正的是一个误判：开关翻转了，却被记成“屏幕没变”。对 `planner=0` 的影响写进 Migration。

**换按法。** 下面两个条件同时成立时，下一次对同一个条目的点击换一种按法：

- 对一个条目执行了点击，执行后指纹没变，且子目标的断言不成立（结果为假，或者为未知且指纹没变）；
- 这一屏还没换过按法。

换法有两种：

- 上一次是无障碍按下的（`pressed ... via accessibility` 或某种操作方式），这次直接点坐标。
- 上一次点的是坐标，且条目有无障碍元素，这次走无障碍按下。

`click_item` 增加参数 `method`，取值 `auto`（今天的顺序）、`pointer` 或 `accessibility`。

换按法这次决策不重问分类器：直接对上一次选中的条目换一种按法执行，因为“选哪个”没有错，错的是“怎么按”。换过一次仍然没效果，才回到正常决策，此时这个条目和这种按法都进入 `already_tried_on_this_screen`。

**断言的“未知”。** 条目不是来自无障碍树，或者适配器没有报告状态（macOS），断言结果就是未知。结果未知时不能用来判定“没效果”，只看指纹。

## Risks / Trade-offs

- [阶段一的重构悄悄改了行为] → 一条回放对比测试，在重构前后对同一组离线场景（`tests/test_scenarios.py` 的假世界）跑一遍，比较历史、结局、`run.json` 键集合和每一步的文件名与内容。阶段一合并前这条测试必须通过。
- [迁移 16 个测试文件时顺手改了断言] → 阶段一的测试提交只允许改导入路径和调用方式。审查时逐个对比被修改的 `assert` 行。
- [`Session` 仍然是一个 40 多个字段的大对象] → 这次接受。拆分要改 `driver.py` 的几十处访问，留给以后专门的变更。
- [规划器写的 `space` 太窄，删掉了正确答案] → 三级放宽。最后一级就是今天的全部候选，所以最坏情况是多问 2 次分类器，约 1.6 秒。
- [合并同义候选把两个确实不同的控件合在一起，例如同一行里的“编辑”和“删除”] → 合并要求标签相同或者框重叠。不同标签、不重叠的按钮不会合并。
- [指纹加入控件状态后，有的应用每一步都在变化（例如进度条的取值），让“没变化”永远判不出来] → `current` 只取复选框、开关、下拉框、滑块的值，不取进度条和文本框的值。离线测试覆盖“进度条的取值变化不算屏幕变化”。
- [macOS 上断言总是未知，规划层的收益只剩缩小候选和合并分差] → 本次接受，读 macOS 开关状态是下一个变更。macOS 上 `goal_state` 永远不会成立，仍由写手判定完成。
- [多一次规划调用，反而让原本一两步就能完成的任务变慢] → 首步分差 ≥ 0.3 就跳过规划。`run.json` 记录规划耗时，对照实验会比较总时长。

## Migration Plan

- 没有数据要迁移。
- 阶段一：配置不变，外部行为不变。测试的导入路径改到新模块。
- 阶段二：
  - `planner` 默认开；设 `CLICKER_PLANNER=0`，就回到阶段一的行为，只有屏幕指纹的修正除外。
  - 指纹修正不受开关控制：之前把开关翻转误判成“没变化”的运行，现在不会再在历史里加 `; the screen did not change`，空转计数也会少记一次。WAA 结果和旧结果比较步数时，以这一点为准。
  - `run.json` 新增的字段只是附加的。读取结果的代码遇到旧文件里没有这些字段时，按“没有规划”处理。
- 回退：阶段二出问题，设 `CLICKER_PLANNER=0`；阶段一出问题，回退那一批提交。两个阶段分批合并。
