# Design

## Context

动机见 proposal.md 的 Why。这里只写决定做法的现状和约束。

- 现在的接管在 `driver.take_over()`：每一步截图、识别，`writer.compose_drive()` 用 `json_schema` 要一个 `DriveStep`，`act()` 执行，再跑空转、重复、累计重做、换路这些止损。动作执行、拒绝原因、换路禁令、运行目录记录都已经在这条路径上，并有 `tests/test_driver.py` 覆盖。
- 写手的 OpenAI 一路是 `openai_writer.OpenAIWriter`，用同步的 `openai.OpenAI` 发 Chat Completions，并在端点拒绝时逐级退让：`json_schema`、`json_object`、无格式，`max_tokens` 改 `max_completion_tokens`，去掉 `reasoning_effort` 和 `thinking`。`OPENAI_API_KEY` 只发往 `api.openai.com`，别的地址只用 `CLICKER_WRITER_API_KEY`。
- 仓库锁定 `openai==2.54.0`，依赖全部钉死。
- 画中画子会话里，`attach_input_desktop()` 只把**主线程**绑定到 `WinSta0\Default`。在其他线程里截图或发输入，会落到错误的桌面上。
- 叫停靠 `desktop.check_abort()` 抛出 `Abort`（`Exception` 的子类），或用户按 Ctrl-C 产生 `KeyboardInterrupt`，最后由 `runner.run()` 接住。
- 已核对 `openai-agents` 0.20.0 的源码，以下三点直接影响设计：
  - Chat Completions 转换器会**丢掉工具输出里的图片**，只保留文字（`chatcmpl_converter.py`）。
  - 同步的 `function_tool` 经 `asyncio.to_thread` 在工作线程里执行；异步工具在事件循环线程里执行。
  - `Runner.run_sync` 在调用它的线程里运行事件循环。
  - `function_tool` 默认会把工具抛出的异常转成一条错误信息交给模型。

## Goals / Non-Goals

**Goals:**

- SDK 只负责「问模型、处理工具调用、管理对话」。截图、动作执行、叫停、防打转、运行目录仍由 jev 自己的代码做，并且都在主线程上。
- 已有的接管行为和日志格式尽量不变，`test_driver.py` 里对执行、拒绝、止损的断言大部分可以保留。
- 离线测试不联网、不碰机器。

**Non-Goals:**

- 不用 SDK 的 `ComputerTool`、handoff、guardrail、session，也不用 Responses API。
- 不做多代理编排。驱动只有一个 `Agent`。
- 不改写手、分类器和最终回答的调用方式。

## Decisions

### 1. 锁定 `openai-agents==0.20.0`，`openai` 保持 2.54.0

PyPI 上 0.20.0 是最后一个接受 `openai<3` 的版本（要求 `openai>=2.45.0,<3`）。0.21.0 及以后，包括最新的 0.25.0，都要求 `openai>=3`。设计用到的接口 0.20.0 里都有：`Agent`、`Runner.run_sync`、`function_tool`、`StopAtTools`、`ModelSettings(parallel_tool_calls, reasoning)`、`RunConfig.call_model_input_filter`、`OpenAIChatCompletionsModel`、`set_tracing_disabled`、`RunState.to_json`。

新增的间接依赖有 `mcp`、`griffelib`、`websockets`。`requests` 和 `pydantic` 已经在锁文件里。

- 备选：升级到 `openai` 3.x 和 `openai-agents` 0.25.0。这样会连带改动写手的 OpenAI 一路，扩大这次的影响面。留给单独的变更去做。

### 2. 用 Chat Completions 模型和 jev 自己的客户端，关掉 tracing

驱动的模型是 `OpenAIChatCompletionsModel(model=resolved_driver_model, openai_client=AsyncOpenAI(...))`。`base_url`、key 和超时都由 `make_writer()` 现有的规则决定，抽成一个共用函数，保证 key 只发往它所属的地址。不用 Responses API，因为 LM Studio、vLLM、DeepSeek 这些兼容端点大多只实现了 Chat Completions。

进程启动时调用 `set_tracing_disabled(True)`。SDK 默认会把 trace 上传到 OpenAI，trace 里有屏幕文字和用户的目标，不能外发。

- 备选：用 `set_default_openai_client` 设置全局客户端。这种做法隐式依赖全局状态，测试要额外清理，所以不用。

### 3. 外层循环仍由 jev 控制，每张屏幕跑一段 `Runner.run_sync`

```
take_over()（jev，主线程）
 └─ 每张屏幕：
     look()：截图、识别、空转检查
     segment = Runner.run_sync(agent, 对话 + 这张屏幕, max_turns=MAX_TOOL_TURNS)
       ├─ 模型调用读取与计算工具（可以多次）→ 结果进对话
       └─ 模型调用一个界面工具或结束工具 → StopAtTools 结束这一段
     从上下文里取出这一段选定的动作（DriveStep）
     act()、拒绝、换路禁令、止损：沿用现有代码
     对话 = segment.to_input_list()，下一张屏幕接在后面
```

界面工具和结束工具的函数体**只记录**模型选的动作：把参数写进运行上下文里的 `DriveStep`，返回 `"accepted"`。真正的执行在 `Runner.run_sync` 返回之后，由 `take_over()` 调用现有的 `act()` 完成。`Agent` 用 `tool_use_behavior=StopAtTools(stop_at_tool_names=[界面工具名…, "done", "give_up"])`，并设置 `ModelSettings(parallel_tool_calls=False)`。

这样做的原因：

- SDK 从不直接碰机器。截图、输入都在主线程上发生，画中画的桌面绑定没有影响。
- `Abort` 和 `KeyboardInterrupt` 在 SDK 之外抛出，不会被 `function_tool` 的错误处理吞掉。
- 目标不匹配就拒绝、`DONE_BEFORE` 提示、换路禁令、`MAX_SAME_ACTION`、`MAX_REVISITS`、`MAX_REROUTES`、`driver_steps` 预算、`step-NNN-sent.png` 都不用搬家。
- 界面动作之间天然是一段一段的。`ca1445f` 定下的「一次一个动作」靠结构保证，不靠提示词。

端点可能不理会 `parallel_tool_calls`，一轮里返回两个界面调用。这时只有第一个会写进上下文，第二个返回 `"refused: one screen action per turn; this one was not taken"`，并在日志里记一行。

- 备选：让界面工具在函数体里直接执行动作，整个接管只调用一次 `Runner.run`。这样会遇到三个问题：同步工具在工作线程上执行；`Abort` 会被转成错误信息交给模型；止损逻辑要改写成钩子，分散在 SDK 的生命周期里。所以不采用。

### 4. 屏幕作为 user 消息发送；只保留最近两张屏幕的图片和元素列表

Chat Completions 会丢掉工具输出里的图片，所以每张屏幕作为一条 user 消息追加：内容是现在 `compose_drive()` 那个 packet 的 JSON（`now`、`last_action`、`already_tried_on_this_screen`、`change_course`、`on_screen`、`screen_items`、`focused_field` 等），`CLICKER_WRITER_VISION` 开着时再附上带编号的截图（data URL）。上一个界面动作的执行结果（`did: …` 或 `refused: …`）写在下一条屏幕消息的 `last_action` 里，不靠工具输出传回。

第一条 user 消息放目标、分类器做过的动作、`earlier_stops`、`why_the_classifier_stopped` 和 `earlier_screens`，也就是现在 packet 里不随屏幕变化的部分。

`RunConfig.call_model_input_filter` 在每次调用模型之前执行：只保留最近 `KEEP_SCREENS = 2` 条屏幕消息的图片和 `screen_items`；更早的屏幕消息只留下 `frontmost_app`、`focused_field`、`last_action`，外加一行 `"(earlier screen; picture and items dropped)"`。读取与计算工具的结果属于事实，不裁剪。

- 备选：让工具输出带图片，并打开 `preserve_tool_output_all_content`。兼容端点对 tool 角色里带图片的支持参差不齐，所以不采用。

### 5. 读取与计算工具写成异步函数，在主线程执行

三个工具：

- `read_text(item: int)`：读当前屏幕第 `item` 个元素的原文。先通过平台适配器的新函数 `desktop.element_text(item)` 去读控件的文本（Windows 依次试 UIA 的 `TextPattern.DocumentRange` 和 `ValuePattern`，Mac 读 `AXValue`，WAA 走 `waa/desktop.py` 已有的远程 UIA 脚本）。读不到时返回识别出的文字。返回值注明来源（`accessibility` 或 `ocr`）、长度，以及是否被截断（上限 `MAX_READ = 20000` 字）。
- `count(text_from_item: int | None, text: str | None, word: str, whole_word: bool = True, case_sensitive: bool = False)`：统计出现次数。文字来源二选一：某个元素的原文（同 `read_text`），或模型直接给出的文本。
- `calculate(expression: str)`：用 `ast` 解析，只允许数字、四则运算、整除、取余、乘方和括号。日期差这类计算不在第一版。

这三个工具都用 `async def` 定义。SDK 会在事件循环线程上执行异步工具，而 `run_sync` 的事件循环就在主线程上。`element_text` 会碰机器，属于读辅助功能树，所以加进 `tests/conftest.py` 的拦截。这些工具调用不计入 `driver_steps`，但每一段最多 `MAX_TOOL_TURNS = 8` 轮。

### 6. 提示词迁到 `driver_agent.py`，去掉备注字段

新建 `yuanji_computer_use/driver_agent.py`，里面放 `Agent` 的定义、各个工具，以及 `instructions`。`instructions` 由 `DRIVE_SYSTEM` 改写：

- 删掉「填哪些字段、哪些留空」这类 JSON 格式规则。工具的参数 schema 已经约束了这些。
- 每个动作的含义写进对应工具的说明（docstring）。
- 新增一条：应用不提供计数、合计这类结果时，用 `read_text` 和 `count` 自己算，再通过界面把结果写出去。这不算应用缺少功能，不能因此放弃。

`your_notes` 和 `notes` 删掉，`state.driver_notes` 跟着去掉，因为对话本身已经保留了前面的事实。`state.driver_done` 保留，`answer_last()` 还要用它。

每个界面工具和结束工具的第一个参数都是 `reason: str`，沿用现在「先写理由再选动作」的顺序。`target` 也保留，目标与元素不匹配仍然拒绝。

### 7. 失败映射沿用现有结局

| 情况 | 处理 |
|---|---|
| 一段结束时没有调用界面工具或结束工具（模型只回了文字） | 算一次读不成的回复，追加一条「请调用一个工具」再问一次；连续 `MAX_UNUSABLE = 2` 次，结局为 `driver failed` |
| `MaxTurnsExceeded`（一段里读取和计算的轮数超限） | 同上，算读不成的回复 |
| `openai.BadRequestError` 提到 `reasoning_effort` | 这次接管剩下的部分不再带 `reasoning`，重跑这一段 |
| 端点不支持工具调用，或其他 `openai.APIError` | 结局为 `driver failed`，日志写明错误 |
| `done` 的理由否认了 `on_screen` 列出的名字 | 沿用 `denied_listed_names`：这一次不执行，追加更正消息，不带截图再问一次 |

### 8. Anthropic 接口时不接管

`runner.driver_ready()` 增加一个条件：`settings().writer_api == "openai"`。不满足时第一次判断记一行日志：`the driver needs CLICKER_WRITER_API=openai; stopping at the classifier`。`config.driver_budget` 不变，`CLICKER_DRIVER=false` 的含义也不变。

### 9. 用量和运行目录

每一段结束后，从 `result.raw_responses` 读出每次模型调用的 usage，记到 `Calls` 的 `DRIVER` 名下：请求数等于 `raw_responses` 的条数；输入 token 扣掉缓存命中的部分，和 `OpenAIWriter._usage` 的口径一致。`step-NNN-driver.json` 的 `actions` 后面加一个 `tools` 列表，按顺序记录这一段每次工具调用的名字、参数和结果（结果超过 2000 字的截断）。`run.json` 新增 `driver_tool_calls`，按工具名计数。

### 10. 测试用假模型

测试里实现一个 `ScriptedModel(agents.models.interface.Model)`：`get_response` 按预先写好的脚本返回 `ModelResponse`，里面是 function call 项。这样 `Runner.run_sync` 的真实工具分发、`StopAtTools`、`call_model_input_filter` 都会被执行到，又不联网。平台调用仍然由 `conftest.py` 拦截，测试需要时自己打补丁。

## Risks / Trade-offs

- [兼容端点不支持工具调用，比如部分本地模型] → 第一次请求就失败，结局 `driver failed`，日志写明原因；`docs/writer-endpoints.md` 注明驱动需要支持 tools 的模型。不回退到旧的单 JSON 驱动，避免维护两套。
- [端点不理会 `parallel_tool_calls`] → 第二个界面调用被拒绝并记入日志（见决定 3），机器上只会执行一个动作。
- [多轮对话让 token 增加] → 只保留两张屏幕的图片和元素列表（决定 4）；读取和计算的轮数有上限；用量照常记入 `run.json`，WAA 汇总里可以对比。
- [0.20.0 是 2026 年 8 月的版本，之后的修复都拿不到] → 只用核心循环和少量接口，以后跟着 `openai` 3.x 一起升级。
- [`mcp` 等新依赖增加 Windows venv 和 WAA 镜像的体积] → 不导入 `agents.mcp`；依赖体积在任务里实测，记进提交说明。
- [`read_text` 在大文档上返回很长的文字] → `MAX_READ` 截断并注明；`count` 直接对元素原文计数，不需要把全文放进对话。
- [辅助功能读到的原文和屏幕显示不一致，比如还没保存的编辑] → 返回值注明来源；提示词写明以 `accessibility` 来源为准。
- [SDK 某个版本改变 `StopAtTools` 或转换器的行为] → 版本锁死；假模型测试覆盖「界面工具结束一段」「一轮两个界面调用」「工具输出不带图」这三条。

## Migration Plan

1. 加入依赖并更新锁文件；在 Linux 和 Windows venv 里确认能导入（`uv sync` 在 Windows venv 上要重跑）。
2. 新增 `driver_agent.py` 和 `desktop.element_text`；改写 `take_over()` 的问答部分；删除 `compose_drive`、`DRIVE_PROPERTIES` 和备注逻辑。
3. 改写 `test_driver.py` 里依赖 `compose_drive` 的测试；`conftest.py` 加入对 `element_text` 的拦截。
4. 更新文档。
5. 用户批准后重建 WAA 镜像（`scripts/waa setup`）；重跑 `benchmarks/waa/` 里的题，对比得分和 token；用画中画重跑记事本计数题。

回滚就是 revert 这次的提交。旧的单 JSON 驱动不保留开关。

## Open Questions

- `KEEP_SCREENS` 取 2 还是 3、`MAX_TOOL_TURNS` 取 8 是否够用，可以在 WAA 重跑以后再调，不影响结构。
- WAA 远程 UIA 脚本读 `TextPattern` 的耗时要实测；如果太慢，`read_text` 在 WAA 里可以先只读 `ValuePattern`，接口不变。
