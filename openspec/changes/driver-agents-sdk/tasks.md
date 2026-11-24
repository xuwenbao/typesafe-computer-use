# Tasks

## 1. 依赖

- [x] 1.1 在 `pyproject.toml` 加入 `openai-agents==0.20.0`，`uv lock` 后确认 `openai` 仍是 2.54.0：`uv run python -c "import agents, openai; print(agents.__version__, openai.__version__)"` 输出 `0.20.0 2.54.0`
- [x] 1.2 在提交说明里记下新增的间接依赖（`mcp`、`griffelib`、`websockets` 等）和 `uv sync` 前后 `.venv` 的体积差，用 `uv tree --package openai-agents` 和 `du -sh .venv` 得出

## 2. 驱动的模型端点

- [x] 2.1 把 `make_writer()` 里 OpenAI 一路的地址和密钥选择抽成一个共用函数，写手和驱动都用它；`tests/test_config_and_writer.py` 现有的密钥测试全部通过
- [x] 2.2 新增驱动的模型构造：用上面的地址和密钥建 `AsyncOpenAI`，交给 `OpenAIChatCompletionsModel`，超时和重试与写手一致；加测试：自建地址且未设 `CLICKER_WRITER_API_KEY` 时，客户端的 key 是占位符而不是 `OPENAI_API_KEY`
- [x] 2.3 导入驱动模块时调用 `set_tracing_disabled(True)`；加测试断言导入后 tracing 处于关闭状态

## 3. 读取元素原文（平台适配器）

- [x] 3.1 `windows.py` 增加 `element_text(item)`：依次读 UIA 的 `TextPattern.DocumentRange` 和 `ValuePattern`，都没有时返回 None；`tests/test_windows.py` 用打补丁的 UIA 元素覆盖三种情况
- [x] 3.2 `macos.py` 增加 `element_text(item)`，读 `AXValue`；加测试覆盖有值和没有值
- [x] 3.3 `waa/desktop.py` 增加 `element_text(item)`，复用现有远程 UIA 脚本读取文本和值；加测试检查生成的远程脚本，不连接虚拟机
- [x] 3.4 把 `element_text` 加进 `tests/conftest.py` 的拦截：测试里未打补丁就调用会被拒绝；加一条测试证明拦截生效

## 4. 读取与计算工具

- [x] 4.1 新建 `yuanji_computer_use/driver_agent.py`，写异步工具 `read_text`：先调用 `element_text`，读不到时回退到识别文字；结果注明来源和长度，超过 `MAX_READ` 截断并注明；测试覆盖辅助功能来源、识别来源、截断和元素编号不存在
- [x] 4.2 写异步工具 `count`：文字来源是元素原文或给定文字，支持整词匹配和区分大小写；用 `largefile.txt` 的文字做测试夹具，断言整词、不区分大小写时 `example` 计数为 22
- [x] 4.3 写异步工具 `calculate`：用 `ast` 只放行数字、四则运算、整除、取余、乘方和括号；测试断言 `(230-1)*2` 正确，`__import__('os')` 和带名字的表达式返回错误

## 5. 驱动 Agent 与屏幕消息

- [x] 5.1 把 `DRIVE_SYSTEM` 改写成驱动的 instructions 放进 `driver_agent.py`：去掉 JSON 字段规则，把各动作说明移到对应工具；加入「应用不给计数时用 `read_text` 和 `count` 自己算，再通过界面写出结果」；测试断言 instructions 里没有 `your_notes`、`notes` 字段说明
- [x] 5.2 写界面工具和结束工具：参数第一个是 `reason`，函数体只把选定的动作写进运行上下文并返回 accepted；同一段里第二个界面调用返回以 refused 开头的说明；测试覆盖这两种情况
- [x] 5.3 组装 `Agent`：`StopAtTools` 列出全部界面工具和 `done`、`give_up`，`ModelSettings(parallel_tool_calls=False)`，推理强度来自回答模型的设置；写测试用的 `ScriptedModel`，断言调用 `count` 之后再调用 `click` 时，`Runner.run_sync` 在 `click` 处结束，上下文里是这个点击
- [x] 5.4 写屏幕消息的构造：第一条 user 消息放目标、分类器的动作、停下原因、之前的提示和屏幕；每张屏幕一条 user 消息，内容是现在 packet 的屏幕部分，`CLICKER_WRITER_VISION` 开着时附带编号截图；测试覆盖开、关截图两种情况
- [x] 5.5 写 `call_model_input_filter`：只保留最近 `KEEP_SCREENS` 张屏幕的截图和元素列表，更早的屏幕换成文字摘要，工具结果不动；测试构造五张屏幕，断言只剩后两张的图片，第一张屏幕上的计数结果仍在

## 6. 接管循环

- [x] 6.1 改写 `driver.take_over()` 的提问部分：每张屏幕跑一段 `Runner.run_sync`，取出选定的动作，交给现有的 `act()`、拒绝、换路禁令、止损和预算逻辑；`tests/test_driver.py` 里关于执行、拒绝、止损和预算的断言改用 `ScriptedModel` 后全部通过
- [x] 6.2 实现失败映射：只回文字和 `MaxTurnsExceeded` 算不可用的回复，提醒一次再问，连续两次结局为 `driver failed`；`APIError` 结局为 `driver failed` 并记录错误；端点拒绝 `reasoning_effort` 时这次接管去掉该参数并重跑这一段；每种情况一条测试
- [x] 6.3 `done` 的理由否认屏幕上列出的名字时，沿用 `denied_listed_names`，不带截图再问一次；测试断言第一次的 done 没有生效
- [x] 6.4 叫停：`Runner.run_sync` 返回后、执行动作前调用 `check_abort`；测试让模型请求期间标记取消，断言返回的点击没有执行，结局为 aborted
- [x] 6.5 删除 `writer.compose_drive`、`DRIVE_PROPERTIES`、`DriveStep` 里的 `notes`，以及 `state.driver_notes`；`rg "compose_drive|driver_notes|DRIVE_PROPERTIES" yuanji_computer_use tests` 没有结果，相关测试已改写或删除
- [x] 6.6 用量与记录：每段从 `raw_responses` 把请求数和 token 记到 `Calls` 的 driver 名下；`step-NNN-driver.json` 增加 `tools` 列表；`run.json` 增加 `driver_tool_calls`；测试断言一段里调用 `read_text`、`count` 再 `shortcut` 后，记录顺序正确，计数各为 1；更新 `docs/run-folder.md`

## 7. 接口限制与文档

- [x] 7.1 `runner.driver_ready()` 在 `CLICKER_WRITER_API` 不是 `openai` 时返回 False，并记一行日志说明原因；测试断言 Anthropic 接口时不接管、写手仍给出最终回答
- [x] 7.2 更新 `docs/how-a-step-works.md` 的接管一节（多轮对话、三类工具、每张屏幕一个动作、截图保留）和 `docs/writer-endpoints.md` 中 `CLICKER_DRIVER*` 的说明（只支持 OpenAI 接口、需要支持工具调用的模型）；README 里提到驱动的地方同步修改；文档里出现的每个环境变量名都能在 `config.py` 里找到

## 8. 集成检查

- [x] 8.1 在仓库根目录跑 `uv run ruff check .` 和 `uv run ruff format --check .`，都没有问题
- [x] 8.2 清掉代理环境变量后跑完整 `uv run pytest`：只剩已知的 10 个失败（`tests/test_answer.py` 1 个、`tests/test_actions.py` 9 个），其余全部通过
- [x] 8.3 用一张保存的截图做离线重放，配合 `ScriptedModel` 走一遍完整接管（读取、计数、界面动作、完成），确认运行目录和 `run.json` 的内容符合规格

## Workflow follow-up

以下几步会碰本机或启动容器，每一步都要先得到维护者批准：

- 在 Windows 画中画用的 venv 里重新 `uv sync`，确认 `import agents` 成功。
- 重建 WAA 客户端镜像（`scripts/waa setup`）。
- 重跑 `benchmarks/waa/` 下的题，和 30 题那次的结果对比得分、步数和 token。
- 用画中画重跑记事本计数题，确认 `example_count.txt` 的内容是 `22`。
- 实测完成以后归档这个变更。
