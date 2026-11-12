# Tasks

## 1. 共享的 UIA 角色规则

- [ ] 1.1 新建 `typesafe_computer_use/uia_roles.py`（只用标准库）：控件类型到 `AX*` 角色的映射表与可按规则，从 `windows.py` 迁出；`windows.py` 的 `role_for`、`pressable` 改为调用它。验证：`tests/test_platform_adapter.py` 与 Windows 适配层原有的单测全部通过，`uia_roles.py` 在没有 pywin32 的 Linux 上可以导入
- [ ] 1.2 在 `tests/test_import_boundaries.py` 加一条：`uia_roles` 不导入 jev 的其他模块，也不导入任何第三方包。验证：该测试通过，并且在 `uia_roles.py` 里故意加一个 `import PIL` 时测试失败

## 2. 在虚拟机里取前台窗口的树

- [ ] 2.1 写 `typesafe_computer_use/waa/walk_vm.py`：只用标准库、`ctypes` 与 comtypes，从前台窗口句柄遍历 UIA 树，限深度 30、单节点子节点 256、总节点 8000、时长 8 秒，超限就截断并标明，最后打印一段 JSON（应用名、进程、窗口矩形，每个节点的角色、名称、矩形、焦点、值、可按）。验证：在 Linux 上用伪造的 comtypes 接口驱动它的单测通过，覆盖截断、超时和没有前台窗口三种情况
- [ ] 2.2 写 `waa/tree.py` 的组装与发送：把 `uia_roles.py` 与 `walk_vm.py` 的源码拼成一段脚本，经 `/execute` 以 `python -c` 执行，后台线程取回，客户端多等 2 秒，任何失败都返回「没有树」并带原因。验证：单测用桩的控制器覆盖成功、超时、非零退出与非法 JSON；另有一条测试断言拼好的脚本短于 32 KB
- [ ] 2.3 写 `waa/tree.py` 的解析：把 JSON 读成前台应用、窗口矩形、焦点字段、地址栏网址（名称匹配 `(?i)address` 的编辑框的值）与节点，节点交给 `ax_walk` 归纳可点击元素。验证：单测以 `tests/fixtures/waa/` 中手写的 JSON 覆盖规格里的按钮、地址栏、焦点字段与截断场景
- [ ] 2.4 加一条测试：虚拟机内使用的映射与 `windows.py` 使用的映射来自同一份 `uia_roles.py`，对每个控件类型给出相同的角色。验证：测试通过；改动其中一处时测试失败
- [ ] 2.5 实现取树的统计（次数、失败数与原因、耗时与节点数的均值和最大值、截断次数），以及 `JEV_WAA_SAVE_TREE=1` 时把每次观测的原始 JSON 存为 `obs-NNN-uia.json`。验证：单测检查统计结果，以及文件名从 `000` 开始编号

## 3. `WAADesktop` 适配层

- [ ] 3.1 由 `osworld/desktop.py` 改写出 `waa/desktop.py`：沿用步边界（输入攒进当前动作、第一次读屏交出动作、步内停顿写成代码、跨步等待用 `WAIT`）；截图取自观测，比例为 1.0；树来自第 2 组，取树结束后才交出动作；`ax_*` 一律返回做不到。验证：从 `tests/test_osworld_agent.py` 中适用的步边界测试迁移出 `tests/test_waa_desktop.py`，全部通过
- [ ] 3.2 实现按键映射（Command 换成 Ctrl，Command-[ 换成 Alt-Left，delete 换成 backspace），以及用 `start chrome`、`start msedge` 打开或调起浏览器的代码。文本只经 `repr()` 进入代码。验证：单测检查生成的代码；含引号、换行和 Python 代码片段的文本经 `ast.parse` 后仍只是一个字符串字面量
- [ ] 3.3 在 `tests/test_platform_adapter.py` 让 `WAADesktop` 接受与 macOS、Windows 适配层相同的 `Desktop` 协议检查。验证：该测试通过

## 4. `JevAgent`

- [ ] 4.1 由 `osworld/agent.py` 改写出 `waa/agent.py`：`predict` 返回 `(response, actions, "", None)`，步数用尽时追加 `DONE`，从不返回 `FAIL`；`reset` 停掉上一个 worker；浏览器按任务域选择（`msedge` 用 Edge，其余用 Chrome）。验证：`tests/test_waa_agent.py` 覆盖四元组的形状、步数用尽、`reset`、worker 抛错与浏览器选择
- [ ] 4.2 在 `run.json` 写入 `waa` 字段：WAA 提交、基础镜像 digest、浏览器、`tree`（`jev-uia`）与第 2.5 项的统计。WAA 提交和 digest 从容器的环境变量读取。验证：单测检查一次模拟运行后的 `run.json`

## 5. overlay 与客户端镜像

- [ ] 5.1 在 `scripts/waa` 里固定 WAA 的提交与 `windowsarena/winarena-base` 的 digest，写出 overlay：`waa_overlay/mm_agents/jev/agent.py`、由该提交的 `run.py` 复制并只改 `jev:` 标注之处的 `waa_overlay/run.py`（只构造 `JevAgent`，使用 `pyautogui`、`require_a11y_tree=False`、模型名 `jev`）、改用 `/opt/waa/bin/python` 的 `waa_overlay/start_client.sh`。验证：测试检查每个副本的文件头写着固定提交，`jev:` 标注之外的行与上游一致（用保存的上游副本对比）
- [ ] 5.2 写客户端镜像的 `Dockerfile`，以及 Python 3.12 的依赖锁：只覆盖 `desktop_env`、`lib_run_single.py`、`run.py` 实际导入的第三方包，不含 Navi 的依赖；与 jev 重叠的包以 jev 的版本为准。验证：在本机用 uv 按这份锁建一个 3.12 环境，`import desktop_env.evaluators` 成功（只装包和导入，不启动容器）
- [ ] 5.3 在 `pyproject.toml` 把 OSWorld overlay 的 ruff 例外换成 WAA 的上游副本。验证：`uv run ruff check` 与 `uv run ruff format --check` 通过

## 6. `scripts/waa` 与结果

- [ ] 6.1 写 `scripts/waa` 的 `setup`：拉取固定提交到 `.waa/WindowsAgentArena`、盖上 overlay、构建 `jev-winarena` 镜像，然后在镜像里检查 `desktop_env.evaluators` 与 `typesafe_computer_use.waa.agent` 能否导入，失败就以非零状态退出。`.gitignore` 加入 `.waa/`。验证：bash 单测检查参数解析与打印出的命令；真正构建镜像并在容器里做导入检查前，先给出确切命令，经批准后执行
- [ ] 6.2 写 `prepare-image`：没有 ISO 时说明下载地址与放置路径后退出；有 ISO 时用 `--prepare-image` 启动容器，生成 `.waa/storage`。验证：bash 单测覆盖缺 ISO 的报错，并检查打印出的 `docker run` 命令（`--device=/dev/kvm`、挂载、8006 与 3390 端口）
- [ ] 6.3 写 `run DOMAIN/ID...`：检查任务格式与任务文件是否存在、检查有无 `/dev/kvm`、检查 `TYPESAFE_API_KEY`、拒绝 `--ocr`、`--envs`、`--num-workers`、写出任务列表、把将要重跑的旧结果移到 `results/archive/<时间>/`、以环境变量传入密钥、`WAA_RAM_SIZE` 与 `WAA_CPU_CORES` 可以覆盖默认的 8G 和 8 核。验证：bash 单测逐条覆盖规格中的场景，其中「打印出的命令不含密钥值」用假密钥检查
- [ ] 6.4 由 `osworld/results.py` 改写出 `waa/results.py` 与 `scripts/waa results`：读取 `results/pyautogui/screenshot/jev/<trial>/<domain>/<id>/`，时间戳不带微秒，token 从 `jev/run.json` 读取；完成与未完成的任务分开算均值，没有得分的任务只列出，不读 `usage.json`。验证：`tests/test_waa_results.py` 用手造的结果目录覆盖这些场景
- [ ] 6.5 在 `tests/conftest.py` 确认 `docker` 进程与连接 WAA 虚拟机地址的请求都已被拒绝，缺的就补上。验证：一条测试在没有打桩时调用它们，断言被拒绝
- [ ] 6.6 写 `docs/waa.md`：前提条件（KVM、Docker、ISO、内存）、`setup`、`prepare-image`、`run`、`results`、结果目录结构、`run.json` 的 `waa` 字段、`JEV_WAA_SAVE_TREE`、评估版 90 天过期、任务之间不回滚快照、不可完成的任务会得 0 分。验证：文档里出现的每个命令和参数都与 `scripts/waa --help` 一致

## 7. 删除 OSWorld，更新文档与规格

- [ ] 7.1 删除 `typesafe_computer_use/osworld/`、`osworld_overlay/`、`scripts/osworld`、`scripts/osworld-gcp`、`infra/gcp/`、`benchmarks/osworld/`、`docs/osworld.md`、`tests/test_osworld_*.py`、`tests/fixtures/osworld/`，以及 `.gitignore` 中的 `.osworld/`；`tests/test_import_boundaries.py` 中的 OSWorld 条目改为 `waa`。验证：`rg -i osworld` 在代码、脚本、测试与配置中没有结果（`openspec/changes/archive/` 除外）
- [ ] 7.2 把主包注释中的 OSWorld 改为 WAA 或删掉（`runner.py`、`perception.py`、`actions.py`、`dates.py`、`platform_adapter.py`）；`pyproject.toml` 里关于「Linux 运行 OSWorld 主机」的注释改为 WAA 客户端容器。验证：同上的 `rg` 检查
- [ ] 7.3 感知评测的覆盖标签把 `linux` 换成 `waa`（`perception_eval.py` 的 `COVERAGE` 及其测试）。验证：`tests/test_perception_eval.py` 通过
- [ ] 7.4 更新 `README.md`、`docs/layout.md`、`docs/how-a-step-works.md`、`CONTRIBUTING.md`、`OBSERVATIONS.md`；`AGENTS.md` 的 OSWorld 一节改为 WAA，写明 `setup` 的容器导入检查、`prepare-image` 和 `run` 每次启动容器都要单独批准，并说明有 KVM 的 WSL2 是运行 WAA 的机器。验证：文档中的链接都指向存在的文件，`rg -i osworld` 在文档中没有结果

## 8. 集成检查

- [ ] 8.1 跑完整的离线测试套件与 ruff。验证：`uv run pytest` 与 `uv run ruff check` 全部通过，期间没有启动任何容器
- [ ] 8.2 校验 OpenSpec change。验证：`openspec validate waa-benchmark --strict` 通过

## Workflow follow-up

- 维护者把 Windows 11 Enterprise 评估版 ISO 放到位，批准后执行 `scripts/waa prepare-image`。
- 维护者选定第一批任务，批准后执行 `scripts/waa run`；核对第一次运行的点击是否落在目标上（design 风险中的分辨率一项）。
- 用 `JEV_WAA_SAVE_TREE=1` 保存真实的 UIA 树，替换 `tests/fixtures/waa/` 中手写的 fixture。
- 决定 13 个不可完成任务的处理方式。
- 评审通过后归档这个 change。
