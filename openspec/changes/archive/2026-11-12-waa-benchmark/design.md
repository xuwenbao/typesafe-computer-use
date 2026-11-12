# Design

## Context

OSWorld 适配层把 jev 的循环接进一个「一步一观测」的 agent 接口，结构保留，换成 WAA 的 Windows 虚拟机：

```
runner.run（worker 线程）
    │  platform_adapter.using(Desktop)
    ▼
OSWorldDesktop                观测 dict 进，pyautogui 代码出
    │  两次读屏之间的输入攒成一个动作
    ▼
JevAgent.predict              与 runner 之间用两个队列交换观测和动作
    ▼
OSWorld 的 run_multienv_*.py  宿主机上的 Python 3.12，自己起虚拟机
```

WAA 的 agent 接口是同一个来源（OSWorld 1.0），但运行方式不同。客户端跑在容器里，和 QEMU 里的 Windows 11 在同一网络：`20.20.20.21:5000` 是虚拟机里的 Flask 服务，`localhost:7200` 是 QEMU 的 QMP（截图走这里）。这两个地址不映射到宿主机。容器由 `docker run --device=/dev/kvm --cap-add NET_ADMIN` 启动，golden image 约 30 GB，挂在 `/storage`。

`lib_run_single.py` 要求 `predict` 返回 `(response, actions, logs, computer_update_args)` 四元组。动作是 pyautogui 代码，经 `/execute` 在虚拟机里以 `python -c` 执行；`WAIT`、`DONE`、`FAIL` 由客户端处理。`/accessibility` 用 pywinauto 遍历全部窗口，XML 标签是窗口类名。任务之间不回滚快照，只关闭所有程序。

动机与范围见 `proposal.md`。

## Goals / Non-Goals

**Goals:**

- 一条命令在本机的一个 WAA 容器里跑完指定任务，结果目录与 jev 自己的运行目录都能在宿主机上读到。
- jev 的循环、感知管线、`Desktop` 协议不改；只换接在协议后面的适配层。
- 无障碍树只含前台应用，角色词表与 `windows.py` 相同，宿主机上用同一个 `ax_walk` 归纳可点击元素。
- 改 jev 或 overlay 不需要重新构建镜像；改依赖或 WAA 的固定提交才需要。

**Non-Goals:**

- 见 `proposal.md` 的「不在本次范围内」。
- 不改感知管线本身。`screen-perception` 与 `perception-evaluation` 只改规格里点名 OSWorld 的句子。
- 不往 Windows 虚拟机里加软件包，不改 golden image 的内容。遍历脚本只用镜像里已经有的 Python 3.10、标准库与 comtypes。
- 不实现 WAA 的 `computer_13` 与 `code_block` 动作空间。

## Decisions

### 1. 客户端留在容器里，自建 Python 3.12 镜像

```
windowsarena/winarena-base（固定 digest）
    └── /opt/waa        Python 3.12 venv：WAA 客户端与评估器的依赖 + jev
/client                 运行时挂载，不烤进镜像
/jev                    运行时挂载本仓库，venv 里是它的可编辑安装
/storage                golden image
```

基础镜像自带的 Python 3.9 不动。`start_client.sh` 改用 `/opt/waa/bin/python`。

客户端留在容器里，因为虚拟机服务和 QMP 都不出容器。把客户端挪到宿主机要改 WAA 的网络，没有好处。

- 备选：沿用 `windowsarena/winarena:latest` 的 Python 3.9，另起进程跑 jev。否决：要维护一套跨进程协议，而 jev 要求 3.12。
- 备选：升级官方镜像里的系统 Python。否决：基础镜像的其他脚本依赖 3.9。

依赖用一份自己的锁文件，不使用 WAA 的 `requirements.txt`（`numpy~=1.24` 不支持 3.12）。锁文件覆盖 `desktop_env`、`lib_run_single.py`、`run.py` 实际导入的第三方包，不含 Navi 的 GroundingDINO。`easyocr` 会带进 CPU 版 torch，镜像因此变大，换来评估器能导入。与 jev 重叠的包（`pillow`、`openai`、`numpy`）以 jev 的版本为准。

`setup` 在这个 venv 里执行 `import desktop_env.evaluators` 和 `import typesafe_computer_use.waa.agent`，导入失败就停止，不进入跑任务的步骤。

基础镜像的 digest 和 WAA 的提交都写在 `scripts/waa` 里。`setup` 拉取该提交到 `.waa/WindowsAgentArena`，再构建镜像 `jev-winarena`。`.waa/` 不进 git。

### 2. `scripts/waa` 是唯一入口，一次一台虚拟机

```
scripts/waa setup                 拉取固定提交，构建镜像
scripts/waa prepare-image         用 setup.iso 生成 .waa/storage（只跑一次）
scripts/waa run DOMAIN/ID...      一个容器，顺序跑这些任务
scripts/waa results [目录]        宿主机上读结果
```

`run` 的容器参数：`--device=/dev/kvm`、`--cap-add NET_ADMIN`、`--rm`，内存 8 GB，CPU 8 核，发布 8006（noVNC）和 3390（RDP）。没有 `/dev/kvm` 就直接退出并说明。ISO 放在 checkout 的 `vm/image/setup.iso`，或用 `--iso` 指定。

挂载：

| 宿主机 | 容器 | 用途 |
|---|---|---|
| `.waa/storage` | `/storage` | golden image |
| checkout 的 `client/`（已盖上 overlay） | `/client` | 运行器与任务 |
| 本仓库 | `/jev` | 可编辑安装 |
| `results/` | `/client/results` | 结果直接落在宿主机 |
| `.waa/tasks/<列表>.json` | `/tasks/list.json` | 本次任务列表 |

密钥从本仓库的 `.env` 读入，以环境变量传入（`TYPESAFE_API_KEY`、`OPENAI_API_KEY`、`CLICKER_*`），不写 WAA 的 `config.json`，不打印。

任务列表沿用 WAA 的格式 `{"<domain>": ["<id>", ...]}`，只跑 `examples/`（normal）。`run` 不接受 `--diff-lvl`、`--som-origin`、`--a11y-backend`、`--num-workers`。

- 备选：直接调用 `scripts/run-local.sh`。否决：它要求 `config.json`、默认启动 Navi，而且清理整个结果目录。

### 3. overlay 只改运行器，agent 本体在 jev 里

`waa_overlay/` 盖到 checkout 的 `client/` 上，每次 `run` 都重新复制：

- `mm_agents/jev/agent.py`：一行导入，导出 `JevAgent`。
- `run.py`：WAA 的 `run.py` 的副本，改动处标 `jev:`。只构造 `JevAgent`；`DesktopEnv` 使用 `action_space="pyautogui"`、`require_a11y_tree=False`；`--model` 固定为 `jev`，作为结果目录名。

`require_a11y_tree=False` 让 WAA 不再调用 `/accessibility`。观测里只有截图、前台窗口标题和矩形；树由 jev 自己取。结果因此落在 `screenshot/jev/<trial>/` 下，`trial` 用 WAA 的默认值 `"0"`。

`start_client.sh` 也放在 overlay 里，挂到容器的 `/start_client.sh`，把 `python` 换成 `/opt/waa/bin/python`。

步数预算用 WAA 运行器的默认值：`max_steps=15`，`sleep_after_execution=3`。这是这套测评集的预算，不用 OSWorld 的 50 步和 2 秒。

重跑时 WAA 的 `get_unfinished` 会删掉没有 `result.txt` 的任务目录，里面的 `jev/` 会一起被删。`run` 在启动容器之前把这些任务的旧目录挪到 `results/archive/<时间>/`，与现在的 `scripts/osworld` 相同。

### 4. `JevAgent` 从 OSWorld 的实现改四元组

`typesafe_computer_use/waa/agent.py` 保留 OSWorld 的线程与队列结构：worker 跑 `runner.run`，`predict` 交出一批动作后等待下一次观测，步数用尽抛 `Abort`，结束时动作列表追加 `DONE`，从不返回 `FAIL`。

返回值改为 WAA 要的四元组 `(response, actions, "", None)`。`logs` 留空，`computer_update_args` 为 `None`，这样 `lib_run_single` 不会调用 `update_computer`。

`Context.browser` 由任务域决定：`chrome` 用 `Google Chrome`，`msedge` 用 `Microsoft Edge`，其余域用 `Google Chrome`。写进 `run.json` 的 `waa.browser`。

`run.json` 的 `waa` 字段记录：WAA 的提交、基础镜像 digest、`tree`（`jev-uia`）、取树的次数、失败次数与原因、每次耗时和节点数的均值与最大值。感知管线版本与权重哈希仍由现有的运行记录写入。

### 5. `WAADesktop`：pyautogui 代码，浏览器改用 `start`

`typesafe_computer_use/waa/desktop.py` 沿用 OSWorld 适配层的步边界：输入攒进当前动作，第一次读屏把动作交给 `predict`；步内的停顿是代码里的 `time.sleep`，跨步的等待才是单独的 `WAIT`。`ax_press`、`ax_focus`、`ax_set_value` 一律返回做不到，点击都是坐标点击。`display_scale` 返回 1.0。

按键与 `windows.py` 一致：Command 变成 Ctrl，Command-[ 变成 Alt-Left，Mac 的 delete 变成 backspace。

浏览器不再用 `wmctrl`。代码用 `start chrome` 或 `start msedge`：已在运行时 Windows 会把窗口调到前台，带 URL 时打开该 URL。文本仍只通过 `repr()` 进入代码字符串。

`WAIT` 在 WAA 里会睡两次 `sleep_after_execution`（分支里一次，`step` 末尾又一次）。适配层不为此把一次等待拆开或合并，接受这 6 秒。

- 备选：调用 `/setup/activate_window` 与 `/setup/launch`。否决：适配层的输出只有动作字符串，加一条旁路会让「一个动作做了什么」从结果目录里看不出来。

### 6. 前台窗口的 UIA 树，在虚拟机里走完，在宿主机上归纳

```
观测到达
    │
    ├─ 截图 ──────────────▶ OCR 与图标检测（不等树）
    │
    └─ /execute  python -c <脚本>
         虚拟机内：前台窗口的 UIA 树，最多 8 秒，打印一段 JSON
                        │
                        ▼
         宿主机：解析成节点，ax_walk 归纳可点击元素
```

脚本是 `waa/walk_vm.py`，只使用标准库、`ctypes` 与 comtypes。comtypes 已在虚拟机里，因为 WAA 的服务用 pywinauto 的 uia 后端。脚本自己计时，到 8 秒就输出已走到的部分并退出。客户端再等 2 秒；超时、脚本报错或没有前台窗口时，这一步没有树，感知仍读截图。不回退到 `/accessibility`。

脚本只从前台窗口的句柄往下走，不枚举桌面。深度不超过 30，一个节点的子节点不超过 256，总节点不超过 8000，超出就截断并在 JSON 里标明。

输出是一棵 JSON：应用名、进程、窗口矩形，以及每个节点的角色、名称、矩形、是否聚焦、值、是否可按。角色在虚拟机里就映射成 `AX*` 词表。

映射表放在 `uia_roles.py`，只有标准库，供 `windows.py` 的 `role_for` 与这段脚本共用。脚本不能导入 jev，所以发送时把 `uia_roles.py` 的源码拼在 `walk_vm.py` 前面，与 OSWorld 把 `light_walk.py` 整段送进虚拟机的做法相同。测试断言两处映射一致。可按的判断沿用 `windows.py` 的规则：有 Invoke，或 LegacyIAccessible 的默认动作非空。

宿主机上 `waa/tree.py` 把 JSON 交给 `ax_walk`。地址栏沿用 `windows.py` 的规则：前台窗口里名称匹配 `(?i)address` 的编辑框的值，Chrome 与 Edge 都不带 scheme。焦点字段从标了 focused 的节点来。这些都用保存在 `tests/fixtures/waa/` 里的 JSON 测试，不接触虚拟机。

取树在后台线程进行，动作要等它结束才发给 WAA，所以脚本不会和点击同时碰 UI。`run.json` 记录每次的耗时、节点数、截断与失败原因。`JEV_WAA_SAVE_TREE=1` 把每次观测的原始 JSON 存为运行目录里的 `obs-NNN-uia.json`，取代 `JEV_OSWORLD_SAVE_A11Y`；`tests/fixtures/waa/` 的树就从这里来。

- 备选：解析 `/accessibility` 的 XML。否决：标签是窗口类名，没有 ControlType，映射不到角色词表，而且遍历整个桌面。
- 备选：在虚拟机里装 `uiautomation` 包，直接调用 `windows.py`。否决：要改 golden image，而且 `windows.py` 导入 pywin32。

`/execute` 把脚本放在 `python -c` 的参数里，Windows 命令行上限约 32 KB。拼好的脚本必须低于这个上限，测试检查脚本文本的长度。

### 7. 结果读取认 WAA 的目录与时间戳

`waa/results.py` 读取 `results/pyautogui/screenshot/jev/<trial>/<domain>/<id>/`：`result.txt` 是得分，`traj.jsonl` 一行一个动作。WAA 的 `action_timestamp` 格式是 `%Y%m%d@%H%M%S`，没有微秒。jev 的 token 从任务目录里的 `jev/run.json` 读。

汇总只针对 jev：完成的任务与未完成的任务分开算均值，没有得分的任务只列出。不读 `usage.json`，不写 `benchmarks/`。

## Risks / Trade-offs

- [任务之间不回滚快照，前一个任务的窗口、文件和设置会留下] → 结果解读要考虑顺序；回滚留到以后，不在本次改 `DesktopEnv.reset`。
- [不可完成任务要最后一个动作是 `FAIL` 才得分，jev 总是回答 `DONE`] → 这 13 个任务得 0 分。处理方式不在本次决定。
- [点击坐标按截图像素 1:1 发送，QEMU 分辨率与 Windows 桌面分辨率可能不一致] → 第一次真实运行时核对一次点击是否落在目标上；若不一致，只在适配层加比例，不改 agent 接口。
- [客户端放弃等待后，虚拟机里的脚本最多还要跑到 `/execute` 的 90 秒超时] → 脚本自己在 8 秒退出；它只读 UI，不发送输入。
- [评估器的包升到支持 3.12 的版本后行为可能变化] → `setup` 做导入检查；真正的评分差异要等选定任务后用该任务的评估器核实。
- [Windows 11 评估版 90 天后过期] → `prepare-image` 可以重新生成 `.waa/storage`；文档写明这一点。
- [本机 15 GB 内存跑 8 GB 的虚拟机，同时只够一台] → 脚本不提供多容器；内存不够时用 WAA 已有的做法调小，通过 `WAA_RAM_SIZE` 与 `WAA_CPU_CORES` 覆盖默认的 8G 和 8 核。

## Migration Plan

1. 加上 `typesafe_computer_use/waa/`、`waa_overlay/`、`scripts/waa`、镜像的 Dockerfile 与锁文件、`docs/waa.md`，以及对应测试。测试全程不启动容器、不连接虚拟机。
2. 更新 `README.md`、`docs/layout.md`、`docs/how-a-step-works.md`、`CONTRIBUTING.md`、`OBSERVATIONS.md`、`AGENTS.md`、`pyproject.toml` 里的 ruff 例外，以及 `screen-perception` 和 `perception-evaluation` 的规格。`AGENTS.md` 写明：启动这个容器（`prepare-image` 和 `run`）每次都要单独批准；有 KVM 的 WSL2 是运行 WAA 的机器。
3. 删除 OSWorld：`typesafe_computer_use/osworld/`、`osworld_overlay/`、`scripts/osworld`、`scripts/osworld-gcp`、`infra/gcp/`、`docs/osworld.md`、`benchmarks/osworld/`、`tests/test_osworld_*.py`、`tests/fixtures/osworld/`，以及 `.gitignore` 里的 `.osworld/`。主包里只出现在注释中的 OSWorld 一并改掉。
4. 回滚就是还原这次提交。没有数据迁移；删除的测评记录不保留。

真实运行不在实现步骤里。`prepare-image` 与第一次 `run` 由维护者在实现完成并选定任务后执行，每次启动容器都先征得同意。
