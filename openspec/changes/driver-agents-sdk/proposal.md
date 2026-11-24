# Proposal

## Why

分类器彻底停下后，驱动每一步只向模型要一个 JSON 动作，而且只能是界面操作。每次调用都是全新的，前面的步骤只靠 400 字的备注传下来。记事本计数题（`notepad/a7d4b6c5`）暴露了这个上限：文件已经打开，驱动也想到了「全文都在屏幕上，可以自己数」，但没有动作能数，于是放弃了；WAA 里同一题则写出了错数 `9`（答案是 `22`）。Anthropic computer use、Agent S3、CoAct-1 都在界面动作旁边加了非界面工具，并让模型在一轮对话里先算再动。驱动需要同样的循环。

## What Changes

- 驱动改用 OpenAI Agents SDK（`openai-agents`，锁定版本）的 `Agent` 和 `Runner` 实现：一次接管是一段多轮对话，模型通过工具调用行动，前面每一轮的推理和工具结果都留在对话里，不再只靠备注。
- **BREAKING**：驱动只支持 OpenAI 接口的模型，也就是 `CLICKER_WRITER_API=openai`，包括任何 OpenAI 兼容的 `CLICKER_WRITER_BASE_URL`（LM Studio、vLLM、DeepSeek 等）。`CLICKER_WRITER_API=anthropic` 时驱动不接管，效果等同于 `CLICKER_DRIVER=false`，日志写明原因。分类器、写手和最终回答仍然两种接口都支持。
- 驱动的工具分三类：
  - **界面工具**：沿用现有动作（点击、输入、按键、快捷键、滚动、拖动、设值、移焦点、等待）。每轮最多执行一个，执行后重新截图，跑一遍现有的防打转检查，再把新屏幕交给模型。这一条保留 `ca1445f` 定下的「一次一个动作」。
  - **读取与计算工具**：不碰鼠标键盘、不改屏幕，可以连续调用，不占接管步数。第一版包括：读一个元素的原文（优先从辅助功能接口读控件的值或文本，读不到时返回识别出的文字并注明来源）、统计一段文字里某个词出现的次数，以及简单的算术。
  - **结束工具**：`done` 和 `give_up`，结束这次接管。
- 现有保护原样保留，仍由 jev 在执行界面动作前后检查：目标不匹配就拒绝、换路后禁止的动作、空转和重复止损、`CLICKER_DRIVER_STEPS` 预算（只计界面动作，被拒绝的也算）、Ctrl-C 和画中画关窗叫停。
- 对话里只保留最近几张截图，更早的截图换成一行文字说明，避免 token 随步数线性增长。
- 运行目录里，每一轮驱动都记录模型调用了哪些工具、参数和结果；`run.json` 的驱动字段增加读取与计算工具的调用次数和用量。
- 不在这次范围内：交还给调用方、读写任意文件、执行命令、按应用加载技巧、跨进程恢复运行。新循环建在 Agents SDK 上，`RunState` 可以序列化，以后做交还和恢复时可以直接用它。

## Capabilities

### New Capabilities
- `driver-takeover`：分类器彻底停下后，由强模型接管的行为：什么时候接管、支持哪种模型接口、可用的工具和每类工具的约束、保护与预算、截图保留，以及运行目录里的记录。

### Modified Capabilities
（无。现有规格里没有驱动；`waa-benchmark` 的要求不变，WAA 里驱动的动作仍经平台适配器在虚拟机内执行。）

## Impact

- 代码：`yuanji_computer_use/driver.py`（接管里向模型提问的部分改为 Agents SDK 的工具循环，动作执行和防打转逻辑留在原处）；`writer.py`（`DRIVE_SYSTEM`、`compose_drive`、`DRIVE_PROPERTIES` 移到驱动的 agent 定义中）；`config.py`（接口为 `anthropic` 时关闭驱动）；`runner.py`（`driver_ready` 判断接口）。
- 平台适配器：`windows.py`、`macos.py` 和 WAA 的 `waa/desktop.py` 增加「读元素原文」。这是新增的碰机器调用，同一变更里要加进 `tests/conftest.py` 的拦截。
- 依赖：新增 `openai-agents==0.20.0`，这是最后一个与现有 `openai==2.54.0` 兼容的版本。WAA 客户端镜像需要重建（`scripts/waa setup`，需要单独批准），Windows 画中画用的 venv 需要重新 `uv sync`。
- 测试：用不联网的假模型驱动工具循环；删除或改写只针对单 JSON 驱动的测试。已知的 10 个失败测试保持不动。
- 文档：`docs/how-a-step-works.md` 的接管一节、`docs/writer-endpoints.md` 中 `CLICKER_DRIVER*` 的说明（注明只支持 OpenAI 接口），以及 README。
