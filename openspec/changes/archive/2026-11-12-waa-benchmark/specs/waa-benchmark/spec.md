# Spec Delta

## Purpose

jev 如何作为 WindowsAgentArena 的 agent 在本机运行、出结果：环境的准备、运行命令、agent 接口的实现，以及结果的记录与汇总。

## ADDED Requirements

### Requirement: 固定的 WAA 版本与客户端环境
`scripts/waa setup` SHALL 取得固定提交的 WAA，并构建一个客户端镜像，其中 WAA 客户端、评估器与 jev 运行在同一个 Python 3.12 环境里。WAA 的提交与基础镜像的 digest SHALL 写在脚本里，不随上游变化。setup MUST 在评估器或 jev 的 agent 无法导入时失败。

#### Scenario: 导入检查失败
- **WHEN** 构建出的客户端环境中导入评估器报错
- **THEN** `setup` 以非零状态退出并打印出错的模块，不报告成功

#### Scenario: 重复执行
- **WHEN** 已经 setup 过的机器再次执行 `scripts/waa setup`
- **THEN** 复用已有的 checkout，镜像构建成功后环境与第一次相同

### Requirement: 准备 golden image
`scripts/waa prepare-image` SHALL 用维护者提供的 Windows 11 评估版 ISO 生成 golden image，存放在被 git 忽略的目录里。ISO 不存在时 MUST 停止并说明去哪里下载、放在哪里。

#### Scenario: 缺少 ISO
- **WHEN** 未提供 ISO 执行 `scripts/waa prepare-image`
- **THEN** 脚本退出并说明 ISO 的下载地址与应放置的路径，不启动容器

### Requirement: 本机运行任务
`scripts/waa run DOMAIN/ID...` SHALL 在本机启动一个 WAA 容器，按给定顺序跑完这些任务（normal 难度），每个任务只用 jev。任务 MUST 写作 `DOMAIN/ID`；不存在的任务 MUST 在启动容器之前报错。主机没有 KVM 时 MUST 停止并说明原因。

#### Scenario: 几个任务一次跑完
- **WHEN** 执行 `scripts/waa run chrome/<id1> msedge/<id2>`
- **THEN** 一个容器内依次跑这两个任务，结束后两个任务的结果都在宿主机的结果目录里

#### Scenario: 任务不存在
- **WHEN** 给出的任务在 WAA 的任务目录中没有对应文件
- **THEN** 脚本报错并点名该任务，不启动容器

#### Scenario: 没有 KVM
- **WHEN** 主机上没有 `/dev/kvm`
- **THEN** 脚本退出并说明需要带 KVM 的 Linux 主机

#### Scenario: 不支持的参数
- **WHEN** 以 `--ocr`、`--envs` 或 `--num-workers` 调用 `run`
- **THEN** 脚本报错并说明该参数不支持

### Requirement: 密钥只从 .env 与环境变量读取
运行所需的密钥 SHALL 从本仓库的 `.env` 或环境变量读取并传给容器。脚本 MUST NOT 打印密钥，MUST NOT 把密钥写入任何文件。缺少 jev 必需的密钥时 MUST 在启动容器之前停止。

#### Scenario: 缺少密钥
- **WHEN** `.env` 与环境变量中都没有 `TYPESAFE_API_KEY`
- **THEN** `run` 在启动容器之前退出，并点名缺少的变量

#### Scenario: 命令回显不含密钥
- **WHEN** `run` 打印它要执行的 `docker run` 命令
- **THEN** 输出中只出现变量名，不出现变量值

### Requirement: jev 实现 WAA 的 agent 接口
jev 的 WAA agent SHALL 提供 `reset()` 与 `predict(instruction, obs)`，`predict` 返回 WAA 要求的四元组（回复、动作列表、日志、电脑更新参数），后两项为空。jev 的循环结束后 SHALL 以 `DONE` 结束，MUST NOT 返回 `FAIL`。动作 SHALL 是在虚拟机内执行的 pyautogui 代码，或 `WAIT`、`DONE`。

#### Scenario: 一步的返回值
- **WHEN** WAA 的运行器以一次观测调用 `predict`
- **THEN** 返回四个元素，第二个是字符串列表，第三个为空，第四个为 `None`

#### Scenario: 步数用尽
- **WHEN** jev 已交出 WAA 允许的最后一批动作
- **THEN** 这批动作之后紧跟 `DONE`，jev 的运行目录照常写出

#### Scenario: 输入文本不成为代码
- **WHEN** jev 要输入的文本含有引号、换行或 Python 代码片段
- **THEN** 这段文本在动作中只作为字符串字面量出现，在虚拟机里原样输入

### Requirement: 浏览器随任务域选择
jev 的默认浏览器 SHALL 由任务域决定：`msedge` 域用 Microsoft Edge，其余域用 Google Chrome。打开或切换浏览器的动作 SHALL 在虚拟机中执行，浏览器未运行时启动它。

#### Scenario: Edge 任务
- **WHEN** jev 在 `msedge` 域的任务里要打开一个网址
- **THEN** 动作在虚拟机里用 Edge 打开该网址

### Requirement: 结果目录与 jev 的运行目录
每个任务的结果 SHALL 落在 WAA 的结果目录结构中，jev 的运行目录 SHALL 位于该任务的结果目录内，名为 `jev`。重跑一个任务前，它之前的结果 SHALL 被移到带时间戳的归档目录，而不是被删除。

#### Scenario: 运行目录的位置
- **WHEN** 一个任务跑完
- **THEN** 该任务的结果目录里有得分文件、轨迹文件和 `jev/run.json`

#### Scenario: 重跑保留旧结果
- **WHEN** 对一个已有结果的任务再次执行 `run`
- **THEN** 旧结果出现在归档目录下，新结果写在原来的位置

### Requirement: run.json 记录运行环境
jev 的 `run.json` SHALL 记录：WAA 的提交、客户端基础镜像的 digest、所用浏览器、树的来源，以及取树的次数、失败次数与原因、耗时与节点数的均值和最大值。

#### Scenario: 记录齐全
- **WHEN** 一个任务跑完
- **THEN** `run.json` 的 `waa` 字段包含上述各项

### Requirement: 结果汇总
`scripts/waa results [目录]` SHALL 列出每个任务的得分、步数、耗时与 token，然后给出 jev 的汇总：完成的任务（得分为 1）与未完成的任务分开计算均值。没有得分的任务 SHALL 只列出，不计入均值。

#### Scenario: 没有得分的任务
- **WHEN** 结果目录中某任务没有得分文件
- **THEN** 该任务出现在列表里并标为无得分，均值不包含它

#### Scenario: 轨迹时间
- **WHEN** 轨迹文件的时间戳不带微秒
- **THEN** 汇总能算出该任务的耗时

### Requirement: 测试不启动容器
离线测试 MUST NOT 启动 WAA 容器、连接虚拟机或调用 Docker。`scripts/waa` 的行为 SHALL 能在不启动容器的情况下检查。

#### Scenario: 离线测试
- **WHEN** 运行测试套件
- **THEN** 没有进程调用 `docker`，也没有连接 WAA 虚拟机的网络请求
