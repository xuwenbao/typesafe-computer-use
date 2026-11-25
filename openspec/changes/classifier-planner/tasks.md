## 1. 重构前的基准

- [x] 1.1 新增 `tests/test_lifecycle_replay.py`：在现有 `tests/world.py` 的假世界里跑一组覆盖各结局的场景（完成、低置信交回、卡住、`stuck`、步数用完、演练、截屏超时、接管完成、接管失败）。把历史、`outcome`、`route`、`run.json` 键集合和每一步的文件名与内容（去掉时间与耗时）存成 `tests/fixtures/lifecycle/*.json`。验证：在重构前的代码上运行通过，并生成基准文件

## 2. 会话、状态与工厂

- [x] 2.1 新建 `yuanji_computer_use/agents/session.py`：把 `RunState` 原样搬过来，改名为 `Session`，字段名与默认值不变；加 `cfg`、`ctx`、`log`、`step`。新增 `StepContext`。验证：`tests/test_signature.py`、`tests/test_answer.py` 改为从新模块导入后通过
- [x] 2.2 新建 `agents/status.py`：`Status` 枚举与阶段一的 `TRANSITIONS` 表（设计决策 3 的十条边）。验证：新增 `tests/test_agent_status.py`，逐条断言合法的边，并断言 `POLICY → DRIVER` 这类非法转移会被拒绝
- [x] 2.3 新建 `agents/base.py`：`Agent` 协议、`register` 装饰器、`AgentFactory.create`。接管登记为模块路径，`create("driver")` 时才导入。验证：新增 `tests/test_agent_factory.py`，断言未知名字会报错，且建出宿主、分类器、写手 agent 之后，`sys.modules` 里没有 `agents` SDK；`tests/test_import_boundaries.py` 通过

## 3. 分类器一步的策略与中间件

- [x] 3.1 新建 `agents/strategies.py`：把 `run_step`、`resolve`、`screen_moved`、`tried_here`、`repeating`、`_lead_covers`、`_leads` 搬成感知、决策、执行、记忆四个默认策略，以及可以单独调用的同名函数；日志文字与顺序不变。验证：`tests/test_decide.py`、`tests/test_actions.py` 及其他引用这些函数的测试，改为从新模块导入后通过
- [x] 3.2 新建 `agents/middleware.py`：`Timing`、`StepFiles`（含 `after_decide` 钩子）、`SessionReport`、`StepLog`，以及 `StepPipeline`。验证：新增 `tests/test_step_middleware.py`，断言一步写出的五个文件名、一步中途抛出异常时 `timing` 仍被记下，以及 `report_action` 收到了动作描述
- [x] 3.3 `driver.py` 去掉 7 处懒导入 `runner`，改为从 `agents.strategies` 和 `agents.session` 导入。验证：`tests/test_driver.py`、`tests/test_driver_agent.py` 通过，且 `driver.py` 里再没有 `from . import runner`

## 4. 四个 agent 与主循环

- [x] 4.1 新建 `agents/host.py`、`agents/policy.py`、`agents/writer.py`、`agents/driver.py`，分别包装 `host.prepare`、四阶段流水线、`hand_off` / `review` / `answer_last` / `wants_driver` / `stuck`、`take_over`。“步数用完”改由分类器 agent 判断。验证：`tests/test_host.py` 通过；新增的状态转移测试覆盖“步数用完时走 `WRITER`，且写手不能交回分类器”
- [x] 4.2 `runner.py` 只保留 `RunConfig`、`run()` 主循环（工厂加状态转移表）和写 `run.json`；中止仍由外层的 `try` 接住。验证：`cli.py`、`helper/service.py`、`waa/agent.py` 不用改动；`tests/test_waa_agent.py`、`tests/test_helper_service.py` 通过
- [x] 4.3 把剩下引用 `runner` 内部函数的测试文件（`test_scenarios.py`、`test_browser_loop.py`、`test_input_abort.py`、`test_step_speed.py` 等）改为引用新模块，只改导入路径和调用方式，不改 `assert` 行。验证：`uv run pytest` 全部通过；`git diff` 里被删的 `assert` 行和新增的 `assert` 行一一对应，只有引用的名字不同
- [x] 4.4 阶段一的验收。验证：`tests/test_lifecycle_replay.py` 用 1.1 的基准通过；`uv run ruff check` 与 `uv run ruff format --check` 通过
- [x] 4.5 文档：`docs/how-a-step-works.md` 增加“agent 与状态”一节（状态转移表、四个阶段、中间件），`CONTRIBUTING.md` 更新模块布局。验证：文中的每个模块名和状态名都能在代码里找到

## 5. 屏幕指纹与按法（规划层的前置）

- [x] 5.1 `models.signature` 的每一行加上来自无障碍树的条目状态：复选框、开关、下拉框、滑块取 `checked`、`expanded`、`current`，进度条和文本框不取。验证：`tests/test_signature.py` 新增两条，开关翻转算作有变化，进度条的取值变化算作没变化；`tests/test_lifecycle_replay.py` 仍然通过（场景里没有控件状态变化）
- [x] 5.2 `actions.click_item` 增加 `method` 参数，取值 `auto`、`pointer`、`accessibility`；`auto` 保持今天的顺序。验证：`tests/test_actions.py` 新增三条，`pointer` 不调用 `ax_press` 和 `ax_operate`，`accessibility` 不调用 `click_at`，`auto` 的调用顺序不变

## 6. 规划器

- [x] 6.1 新建 `agents/planner.py`：`Plan`、`Subgoal`、`Space`、`Predicate`，空间匹配（角色或操作方式加标签正则，标签同时匹配 `text` 与 `beside`，没有操作方式信息的条目视为满足）与断言求值（真、假、未知）。验证：新增 `tests/test_planner.py`，覆盖同名行标题与带 `toggle` 的开关、错误描述的图标被排除、文字条目的断言为未知、条目不存在时 `exists` 为假
- [x] 6.2 `config.py` 增加 `planner`（默认真）、`plan_skip_margin`（0.3）、`decide_margin`（0.15）、`max_replans`（2）、`max_subgoals`（5），以及对应的 `CLICKER_*` 环境变量。验证：`tests/test_config_and_writer.py` 覆盖默认值、关闭规划，以及取值越界时被拒绝
- [x] 6.3 `writer.compose_plan`：结构化输出，按 `compose_host` 的方式校验；读不成计划时返回空。`do` 不在名单里、正则编译不了、子目标超过 3 个，都算读不成。验证：`tests/test_config_and_writer.py` 新增解析测试，覆盖合法计划、`do` 为 `drag`、正则不合法、以及 4 个子目标
- [x] 6.4 `docs/writer-endpoints.md` 写明新增的配置项和规划调用。验证：每个配置项的默认值与 `config.py` 一致

## 7. 候选空间与合并分差

- [x] 7.1 `decide.decide` 增加 `candidates` 参数，`item_question` 只列这些条目，编号保持原样。新增 `merged_margin`（框的交并比至少 0.5，或者同标签且垂直中心相差不到半个行高）。验证：`tests/test_decide.py` 新增三条：子集保留原编号；同一行同名的 0.41 与 0.37 合并后分差大于 0.15；不重叠的“编辑”和“删除”不合并
- [ ] 7.2 `SpaceDecide` 策略：三级放宽，放宽时重问分类器并记入 `session.widened`；到全部候选后交回门槛规则。验证：`tests/test_planner.py` 新增两条：规划写成 `checkbox` 而真开关是 `button` 时放宽到只按标签；三级分差都太小时按门槛规则处理

## 8. 规划层接入主循环

- [ ] 8.1 状态转移表加 `POLICY → PLAN`、`PLAN → POLICY`、`PLAN → WRITER`。新增规划 agent：调用 `compose_plan`，写 `step-NNN-plan.json`，读不成计划时在历史里记 `planner: unusable plan` 并去 `WRITER`。验证：`tests/test_agent_status.py` 覆盖新的三条边
- [ ] 8.2 `PolicyAgent` 按子目标装配阶段：`ensure` 只跑感知和断言；其余子目标用 `SpaceDecide` 加 `VerifyStrategy`。每一步之后检查 `goal_state`，成立就以 `done` 交给写手，并带上 `goal_state_held: true`。验证：`tests/test_scenarios.py` 新增一个开关场景：首步分差小，规划一次，点开关后断言成立，运行以 `done` 结束，写手只被调用一次
- [ ] 8.3 首步跳过规划（合并分差 ≥ `plan_skip_margin`）；分差小时这一步不执行。分类器停下时，只要重规划次数没用完就去规划；`done` 和步数用完照旧交给写手。验证：场景测试覆盖首步分差 0.6 时没有规划调用、首步跳过后第三步低置信时去规划、以及 `CLICKER_PLANNER=0` 时完全不规划
- [ ] 8.4 换按法：执行子目标时，一次点击后指纹没变且断言不为真，下一次不调分类器，直接对同一条目换按法；每屏每条目只换一次，换后仍无效就回到正常决策，并把两种按法都记为已尝试。验证：场景测试覆盖无障碍按下无效后改点坐标且没有调用分类器，以及换后仍无效时已尝试列表里有这两种按法
- [ ] 8.5 重规划：把之前的子目标及其断言结果交给规划；重规划不超过 `max_replans`，累计子目标不超过 `max_subgoals`；用完后按阶段一的方式交给写手。验证：场景测试覆盖第三次断言失败后交给写手且不再规划，以及已执行 4 个子目标时新计划只执行第一个
- [ ] 8.6 `run.json` 加最后一份计划、每个子目标的断言结果、放宽次数、重规划次数、规划调用次数与总耗时；没有规划时为空值或零。验证：`tests/test_lifecycle_replay.py` 关闭规划时仍然通过（只多出新字段）；新增一条测试断言规划过一次的运行里这些字段的值
- [ ] 8.7 `docs/how-a-step-works.md` 增加“规划”一节：何时跳过、计划的结构、放宽、合并分差、断言、换按法，以及 macOS 上断言总是未知。验证：文中的阈值与 `config.py` 的默认值一致

## 9. 整体验收

- [ ] 9.1 `uv run pytest`、`uv run ruff check`、`uv run ruff format --check` 全部通过，且机器守护没有被放宽。验证：命令退出码为 0；`tests/conftest.py` 的 diff 里只有新增，没有删除
- [ ] 9.2 实机对照（Windows）：选一个开关类目标，先手动置为“开”，用同一句目标分别跑 `CLICKER_PLANNER=0` 和默认配置，记录步数、耗时、点击命中率、模型调用次数。每条命令都要先向维护者提出并得到批准。验证：两份 `run.json` 的对比写进本变更目录下的 `ab-run.md`

## Workflow follow-up

- 维护者评审通过后归档本变更，并确认 `openspec/specs/` 下新增了 `run-lifecycle`、`goal-planner`、`subgoal-verification`。
- 后续变更：把成功的计划存成带断言的宏并回放；macOS 读开关状态。
