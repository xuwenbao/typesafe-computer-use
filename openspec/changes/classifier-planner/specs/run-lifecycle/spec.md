## Purpose

规定一次运行里宿主、分类器、写手、接管与规划器之间由谁在什么时候接手，以及每种结局和每一步在运行目录里留下的、外部可以依赖的结果。

## ADDED Requirements

### Requirement: 交接顺序
一次真操作的运行 SHALL 先由宿主把目标应用放到前台，再交给分类器。分类器停下时 MUST 交给写手。写手 MUST 在三条路里选一条：把运行交回分类器、交给接管、结束运行。接管结束后，写手 MUST 在最终屏幕上再回答一次，然后运行结束。任何交接 MUST NOT 越过这个顺序；规划器的交接见 `goal-planner`。

#### Scenario: 写手交回分类器
- **WHEN** 分类器因低置信停下，写手判断目标未完成，并给出一个焦点
- **THEN** 分类器带着这个焦点继续下一步

#### Scenario: 接管之后再回答一次
- **WHEN** 接管以完成结束
- **THEN** 写手在最终屏幕上回答一次，运行结束，不再交回分类器

#### Scenario: 演练不交给写手
- **WHEN** 没有 `--act`，分类器选出一个动作
- **THEN** 运行以 `dry run` 结束，不调用写手和接管

### Requirement: 结局的取值
`run.json` 的 `outcome` SHALL 是下列取值之一：`done`、`nothing helps`、`low confidence`、`stalled`、`stuck`、`step limit`、`dry run`、`capture timed out`、`aborted (...)`、`crashed`、`driver done`、`driver gave up`、`driver failed`、`driver stalled`、`driver step limit`。重构 MUST NOT 增加、删除或改写其中任何一个取值，也不得改变产生它们的条件。

#### Scenario: 分类器把步数用完
- **WHEN** 分类器走满 `--steps` 步仍未停下
- **THEN** `outcome` 是 `step limit`，写手被调用，且不能再交回分类器

#### Scenario: 中途中止
- **WHEN** 用户在任意一个角色工作时按下中止
- **THEN** `outcome` 以 `aborted` 开头，`run.json` 照常写出

### Requirement: 接管只在需要时加载
接管所依赖的模型 SDK SHALL 只在接管真正开始时才加载。加载失败时，运行 MUST 保留写手已经给出的回答，在 `run.json` 的 `driver_error` 里写明原因，然后结束。

#### Scenario: 不需要接管的运行
- **WHEN** 一次运行在分类器和写手之间就结束
- **THEN** 接管的 SDK 从未被导入

#### Scenario: SDK 加载失败
- **WHEN** 需要接管，但导入接管模块时出错
- **THEN** 运行结束，最终回答是写手之前的回答，`driver_error` 非空

### Requirement: 每一步留下的文件
分类器的每一步 SHALL 在运行目录里留下 `step-NNN-raw.png`、`step-NNN-sent.png`、`step-NNN-payload.txt`、`step-NNN.png` 和 `step-NNN-answers.json`。写手每次被调用 MUST 写 `step-NNN-review.json`；接管每次调用模型 MUST 写 `step-NNN-driver.json`。`NNN` 是三位的步号。即使一步中途出错，已用时间 MUST 计入 `run.json` 的 `timing`。

#### Scenario: 一步正常执行
- **WHEN** 分类器完成一步并执行了动作
- **THEN** 这一步的五个文件都在运行目录里

#### Scenario: 截屏超时
- **WHEN** 某一步截屏超时
- **THEN** `outcome` 是 `capture timed out`，这一步不写图片文件，`timing` 仍记下这一步

### Requirement: 重构前后结果一致
在不启用规划层时，对同一组离线场景，运行 SHALL 产生与本变更之前相同的历史、`outcome`、写手去向，以及相同的每一步文件名与文件内容（时间与耗时除外）。`run.json` 的键集合 MUST 只多出规划层的新字段，不少任何旧字段。唯一允许的差别是：两次截屏之间有控件状态变了，屏幕算作有变化（见 `subgoal-verification`）。

#### Scenario: 用离线场景回放
- **WHEN** 关闭规划层，用同一个假世界和同一份脚本化的分类器回答各跑一次，期间没有控件状态变化
- **THEN** 两次运行的历史、`outcome` 和运行目录里的文件一一对应

#### Scenario: 开关翻转不再算作没变化
- **WHEN** 关闭规划层，一次点击只翻转了一个开关，屏幕上的文字没变
- **THEN** 这次点击的历史行不带 `; the screen did not change`，空转计数不增加
