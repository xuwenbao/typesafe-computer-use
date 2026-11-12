## Why

jev 的桌面测评目前只有 OSWorld：任务跑在 Ubuntu 虚拟机里，无障碍树来自 AT-SPI，而 jev 在真实机器上要支持的是 macOS 与 Windows。WindowsAgentArena（WAA）是 Microsoft 在 OSWorld 1.0 基础上做的 Windows 11 测评集（12 个应用域、154 个任务），agent 接口（`predict`、`reset`）与 OSWorld 几乎相同，能直接检验 jev 的 Windows 路径（UI Automation 树、Windows 按键与窗口），而这条路径至今没有任何测评。维护者的开发机（WSL2）有 KVM，WAA 的容器可以在本机直接运行，不再需要云上机器。

## What Changes

- 新增 jev 作为 WAA agent 的运行方式：`scripts/waa` 提供 `setup`（拉取 WAA 的固定提交、构建客户端镜像）、`prepare-image`（用 Windows 11 评估版 ISO 生成 golden image）、`run TASK...`（在本机启动一个 WAA 容器，跑一个或多个任务）与 `results`（每个任务的得分、步数、耗时与 token）。
- WAA 的客户端改为在 Python 3.12 下运行：基于 `windowsarena/winarena-base` 构建自己的镜像，装入 Python 3.12、jev 与一份只含 WAA 客户端和评估器所需依赖的锁文件，不装 Navi agent 的依赖（GroundingDINO 等）。WAA 官方的 Python 3.9 环境与 `requirements.txt` 不再使用。
- jev 的 WAA 适配层由现有 OSWorld 适配层改造而来：`JevAgent` 的 `predict` 改为返回 WAA 要求的四元组；`Desktop` 适配层继续把输入累积为 pyautogui 代码，浏览器的启动与切换改为 Windows 的做法（Chrome 与 Edge），不再依赖 `wmctrl`。
- jev 在 WAA 虚拟机里自己取无障碍树：通过 WAA 服务的 `/execute` 在虚拟机内运行一段 Python，只遍历前台应用的 UI Automation 树，直接按 jev 的角色词表输出；复用 `windows.py` 与 `ax_walk.py` 的纯规则。不使用 WAA 自带的 `/accessibility`，它遍历整个桌面，XML 标签是窗口类名而不是控件类型。
- WAA 运行方式的改动放在 overlay 里（agent 模块与改写后的 `run.py`，每处改动标 `jev:`），由 `scripts/waa` 挂载进容器，与 OSWorld 的 overlay 做法一致。
- 只在本机跑单个 worker，一次一台 Windows 虚拟机。任务之间沿用 WAA 的默认做法（关闭所有程序），不回滚虚拟机快照。
- **BREAKING** 删除 OSWorld 集成：`typesafe_computer_use/osworld/`（含 AT-SPI 解析与 `light_walk.py`）、`osworld_overlay/`、`scripts/osworld`、`.osworld/` 的忽略项，以及 OSWorld 的测试与 fixture。
- **BREAKING** 删除对比基线：OSWorld 的 GPT agent（Luna）的 runner、agent 与 token 计数（`osworld/usage.py`）。WAA 上只跑 jev。
- **BREAKING** 删除云上运行：`scripts/osworld-gcp`、`infra/gcp/`。
- **BREAKING** 删除 OSWorld 的历史测评记录 `benchmarks/osworld/` 与写入它的 `osworld/record.py`；本次不为 WAA 新建对应的记录文件。

不在本次范围内：

- 第一批要跑的任务。实现完成后由维护者选定。
- WAA 中 13 个不可完成任务（`infeasible`，只有最后一个动作为 `FAIL` 才得分）的处理。jev 结束时总是回答 `DONE`，这些任务目前会得 0 分。
- WAA 的 hard 模式（`examples_noctxt`）、多 worker 并行、云上运行（Azure ML 或 GCP）。
- 任务之间回滚虚拟机快照。

## Capabilities

### New Capabilities

- `waa-benchmark`：jev 如何在 WAA 上运行与出结果。包括 WAA 固定提交与客户端镜像的构建、golden image 的准备、运行命令与参数、`JevAgent` 对 WAA agent 接口的实现、jev 的运行目录在任务结果目录中的位置、`run.json` 记录的运行环境，以及结果的读取与汇总。
- `waa-uia-tree`：jev 如何在 WAA 虚拟机里取得前台应用的 UI Automation 树。包括通过 `/execute` 运行的遍历脚本、只遍历前台应用、输出格式与角色映射、超时与失败时的处理（没有树时感知仍读截图），以及记录每次取树的耗时与节点数。

### Modified Capabilities

- `screen-perception`：要求"macOS、Windows 与 OSWorld 的桌面路径使用同一条感知管线"中的 OSWorld 改为 WAA；要求"OSWorld 不再选择 OCR 后端"改为针对 WAA 的运行入口（`scripts/waa run`、`JevAgent`），保留"`run.json` 记录感知管线版本与权重哈希"。
- `perception-evaluation`：评测集覆盖面中的"Linux（OSWorld）桌面"删除，改为"WAA 的 Windows 11 虚拟机桌面"。

## Impact

- **代码**：删除 `typesafe_computer_use/osworld/`；新增 `typesafe_computer_use/waa/`（agent、Desktop 适配层、虚拟机内的 UIA 遍历脚本、树的解析、结果读取）；`windows.py` 的纯规则可能需要拆出，以便在虚拟机内的脚本里复用而不导入 pywin32。`runner.py`、`perception.py`、`actions.py`、`dates.py`、`platform_adapter.py` 里提到 OSWorld 的注释改为 WAA 或删除。
- **依赖**：jev 自身的依赖不变；WAA 客户端在镜像内有一份自己的 Python 3.12 锁文件。WAA 评估器依赖的部分包（`numpy`、`gymnasium`、`pydrive`、`borb`、`librosa`、`easyocr` 等）需要升级到支持 3.12 的版本，升级后评估器的行为要逐一验证。`pyproject.toml` 中 OSWorld overlay 的 ruff 例外删除，改为 WAA overlay 的。
- **环境**：需要有 `/dev/kvm` 的 Linux 主机与 Docker；本机（WSL2）满足。需要手动下载 Windows 11 Enterprise 评估版 ISO（约 6 GB，90 天评估期），golden image 约 30 GB，准备一次约 20 分钟；评估期过后需重新准备。Windows 虚拟机默认占 8 GB 内存与 8 个 CPU 核。
- **测试**：删除 `tests/test_osworld_*.py` 与 `tests/fixtures/osworld/`；新增 WAA 适配层、UIA 树解析、结果读取与 `scripts/waa` 的离线测试，以及从 WAA 虚拟机保存的 UIA 树 fixture。`tests/test_import_boundaries.py` 中的 OSWorld 条目改为 WAA；`tests/conftest.py` 的机器守护保持不变。
- **文档**：`docs/osworld.md` 改为 `docs/waa.md`；更新 `README.md`、`docs/layout.md`、`docs/how-a-step-works.md`、`CONTRIBUTING.md`、`OBSERVATIONS.md`；`AGENTS.md` 的 OSWorld 一节改为 WAA，写明启动 WAA 容器（`prepare-image`、`run`）每次都需要批准，并说明开发机可能是有 KVM 的 WSL2。
- **可比性**：任务之间不回滚快照，前一个任务留下的状态可能影响后一个任务的得分。
- **不受影响**：jev 在 macOS 与 Windows 真实机器上的运行、浏览器路径（`browser/`）、沙盒（`scripts/sandbox`）。
