# screen-perception

## Purpose

桌面路径如何把一张截图变成可点击元素。Purpose 细节待补。

## Requirements

### Requirement: 桌面路径统一的感知管线
macOS、Windows 与 OSWorld 的桌面路径 SHALL 使用同一条进程内感知管线，把一张截图变成可点击元素：RapidOCR 识别文字，YOLOv9-E（`icon_detect_v3`）检测图标，二者融合后再与无障碍树合并。感知结果 MUST NOT 依赖运行所在的平台；平台适配层 MUST NOT 提供文字识别。

#### Scenario: 三个平台得到相同的元素
- **WHEN** 同一张截图分别经由 macOS、Windows 与 OSWorld 的感知入口处理，且无障碍树为空
- **THEN** 三者产出的元素列表（文字、框、来源、编号）完全相同

#### Scenario: 平台适配层不再提供文字识别
- **WHEN** 检查 `Desktop` 协议及 `macos.py`、`windows.py`、OSWorld 的 `Desktop`
- **THEN** 其中都没有 `recognize_text`，也不导入 `ocrmac` 或 `winocr`

### Requirement: 只读取前台窗口区域
管线 SHALL 只在 `ocr_region` 范围内识别文字和检测图标（前台窗口加边距，与同列的菜单栏条合并）；没有窗口信息时 SHALL 读取整张截图。返回的所有框 MUST 是整张截图的像素坐标。

#### Scenario: 窗口外的图标不出现
- **WHEN** 截图中前台窗口之外的桌面上有一个图标
- **THEN** 元素列表中没有该图标

#### Scenario: 框使用整张截图坐标
- **WHEN** 区域从截图的 (100, 50) 开始，区域内某图标在区域裁剪图中的框是 (10, 10, 30, 30)
- **THEN** 该元素的框是 (110, 60, 130, 80)

### Requirement: 文字行的框收紧到墨迹
RapidOCR 返回的每个文字框 SHALL 收紧到框内墨迹的范围（与框背景相差超过 `INK_CONTRAST` 的像素）；框内没有墨迹时 SHALL 保持原框。

#### Scenario: 外扩的框被收紧
- **WHEN** RapidOCR 为一行高 20 像素的文字返回上下各外扩 10 像素的框
- **THEN** 进入融合的框与文字墨迹上下边界一致

### Requirement: 文字行过滤
置信度低于 `MIN_OCR_CONFIDENCE` 的行、与启动本次运行的目标文字重复的行（回显），以及落在焦点输入框内的行 SHALL 在融合之前被丢弃。

#### Scenario: 低置信度行被丢弃
- **WHEN** 一行文字的置信度为 0.2，`MIN_OCR_CONFIDENCE` 为 0.3
- **THEN** 该行不参与融合，也不出现在元素列表中

### Requirement: 图标与文字的融合
融合 SHALL 在文字行上进行，且先于行并段。规则如下：
- 图标框之间：两框的重叠度（IoU、或交集占任一框面积的比例，取最大）超过 0.7 时，保留面积较小的框。
- 文字行面积的 80% 以上落在某图标框内时，该行 SHALL 并入该图标，成为图标的文字，且不再单独成为元素。
- 图标框面积的 80% 以上落在某文字行内时，该图标 SHALL 被丢弃。
- 其余图标为无文字图标。

#### Scenario: 按钮内的文字并入按钮
- **WHEN** YOLO 检测到一个按钮框，OCR 在框内读到 "保存"
- **THEN** 元素列表中只有一个元素，来源为 `icon+ocr`，文字为 "保存"

#### Scenario: 文字里的图标被丢弃
- **WHEN** 一个 YOLO 框完全落在一行 OCR 文字之内
- **THEN** 元素列表中没有该图标，只有该文字行

#### Scenario: 嵌套图标保留小框
- **WHEN** YOLO 给出两个框，小框完全落在大框内
- **THEN** 只保留小框

#### Scenario: 融合先于并段
- **WHEN** 一段多行文字的左侧紧挨着一个小图标，图标与任何单行文字的重叠都不超过 80%
- **THEN** 该图标作为独立元素保留，文字行随后并成一段

### Requirement: 无文字图标的描述
每个无文字图标 SHALL 由 Florence-2 图标描述模型生成描述：裁剪图标框、缩放到 64×64、提示词 `<CAPTION>`、贪心解码、最多 20 个新 token。描述与图标 MUST 一一对应；某个图标裁剪失败时，它的描述 SHALL 为空字符串，其余图标的描述不得错位。没有无文字图标时 MUST NOT 调用描述模型。

#### Scenario: 无文字图标得到描述
- **WHEN** 工具栏上有一个齿轮图标，框内没有 OCR 文字
- **THEN** 元素列表中有一个来源为 `icon`、文字为模型描述的元素

#### Scenario: 裁剪失败不导致错位
- **WHEN** 三个无文字图标中第二个的框面积为零
- **THEN** 第一和第三个图标得到各自的描述，第二个的描述为空

#### Scenario: 没有无文字图标时不调用模型
- **WHEN** 所有图标都含有 OCR 文字
- **THEN** 描述模型没有被调用

### Requirement: 图标描述缓存
描述 SHALL 按 64×64 裁剪图 RGB 字节的 SHA-256 缓存，缓存属于一次运行，最多 1024 条，按最近最少使用淘汰。命中缓存的图标 MUST NOT 再送入模型。

#### Scenario: 同一图标第二步命中缓存
- **WHEN** 连续两步截图中同一个齿轮图标像素完全相同
- **THEN** 第二步不为该图标调用描述模型，描述与第一步相同

### Requirement: 元素来源与无障碍树合并
每个元素 SHALL 标明来源：`ocr`（文字块）、`icon+ocr`（含文字的图标）、`icon`（无文字图标，`role` 为 `icon`）、`ax`、`ax+ocr`、`ax+icon`。与无障碍控件的合并 SHALL 满足：
- 无障碍控件与文字块按现有规则配对（重叠且文字匹配），结果为 `ax+ocr`。
- 无障碍控件与图标框按重叠（交集占较小框 ≥ 0.5）配对，不要求文字匹配，结果为 `ax+icon`；其文字 SHALL 是控件的 label，label 为空时用图标的文字或描述。
- 合并后的元素 SHALL 保留指向无障碍控件的引用，以便直接按下。

#### Scenario: 图标与无障碍按钮合并
- **WHEN** 一个 label 为 "设置" 的无障碍按钮与一个描述为 "gear, settings" 的图标框重合
- **THEN** 元素列表中只有一个元素，来源为 `ax+icon`，文字为 "设置"，且可以通过无障碍引用按下

#### Scenario: 无 label 的控件使用描述
- **WHEN** 一个 label 为空的无障碍按钮与一个描述为 "back arrow" 的图标框重合
- **THEN** 合并后的元素文字为 "back arrow"

### Requirement: 选项预算
元素总数超过 `MAX_OPTIONS` 时，SHALL 按以下顺序淘汰：低置信度的 `ocr` 元素最先，其次低分的 `icon` 元素；来自无障碍树的元素 MUST NOT 为文字或图标让位。淘汰之后 SHALL 按阅读顺序重新编号。

#### Scenario: 超出预算时先淘汰文字
- **WHEN** 合并后有 260 个元素，其中有 `ocr`、`icon` 与 `ax` 元素，`MAX_OPTIONS` 为 255
- **THEN** 被淘汰的 5 个都不是来自无障碍树，且优先是置信度最低的 `ocr` 元素

### Requirement: 增量识别
同一次运行中，若应用、窗口与读取区域都未变化，管线 SHALL 只对变化的分块重新识别文字；没有分块变化时 SHALL 复用上一步的图标框，有任何分块变化时 SHALL 对整个区域重新检测图标。变化面积超过 `REOCR_FRACTION` 时 SHALL 整体重读。

#### Scenario: 屏幕不变时不重新检测
- **WHEN** 连续两步截图在读取区域内没有任何分块变化
- **THEN** 第二步既不调用 OCR 也不调用 YOLO，元素与第一步相同

#### Scenario: 局部变化时整区重新检测图标
- **WHEN** 只有一个分块发生变化
- **THEN** 文字只重读覆盖该分块的矩形，图标对整个区域重新检测

### Requirement: 预读与推理互斥
在查询前台应用、窗口与无障碍树的同时，管线 SHALL 在另一个线程里预先完成 OCR 与图标检测；预读结果只在截图、应用、窗口与缩放都与预读时一致时采用。OCR、图标检测与图标描述 MUST 在同一时刻至多运行一个。

#### Scenario: 应用变化时丢弃预读
- **WHEN** 预读假设前台应用是 A，查询结果却是 B
- **THEN** 本步不采用预读结果，而是重新识别

### Requirement: 模型权重
模型 SHALL 在第一次使用时加载，导入感知模块 MUST NOT 加载任何模型。权重 SHALL 从 Hugging Face 下载并固定到具体提交哈希；`icon_detect_v3/model.pt` 下载后 SHALL 校验 SHA-256，不一致时报错。MUST NOT 使用基于 Ultralytics 的图标检测权重，也 MUST NOT 依赖 `ultralytics` 包。设置了 `HF_HUB_OFFLINE` 时 MUST NOT 访问网络。SHALL 提供一个预取全部权重的命令。

#### Scenario: 导入不加载模型
- **WHEN** 导入感知模块但没有处理任何截图
- **THEN** 没有任何模型权重被读取或下载

#### Scenario: 权重校验失败
- **WHEN** 下载到的 `icon_detect_v3/model.pt` 的 SHA-256 与记录不符
- **THEN** 感知报错并指出该文件，不使用该权重

#### Scenario: 离线模式缺权重
- **WHEN** 设置了 `HF_HUB_OFFLINE` 且本地缓存中没有权重
- **THEN** 感知报错并提示运行预取命令，不发起网络请求

### Requirement: 不支持的平台明确报错
桌面路径 SHALL 支持 Apple 芯片的 Mac、Windows（x86_64）与 Linux。在 Intel Mac 上，推理依赖 MUST NOT 被安装，导入 jev 与运行离线测试 MUST 不受影响；`clicker` 在桌面路径启动时 SHALL 立即退出，并说明该平台不受支持。

#### Scenario: Intel Mac 上启动桌面路径
- **WHEN** 在 Intel Mac 上运行 `clicker "某个目标"`
- **THEN** 程序在截屏之前退出，错误信息说明新感知管线不支持 Intel Mac

#### Scenario: Intel Mac 上导入与测试
- **WHEN** 在 Intel Mac 上 `uv sync` 并运行离线测试
- **THEN** 安装成功，测试不因缺少 torch 而失败

### Requirement: 测试不加载真实模型
在测试中，OCR、图标检测与图标描述的模型加载 SHALL 直接报错；需要它们的测试 MUST 自行打桩。

#### Scenario: 测试误用真实模型
- **WHEN** 某个测试在没有打桩的情况下触发了图标检测
- **THEN** 测试以明确的错误失败，指出不得在测试中加载模型

### Requirement: 浏览器路径不受影响
浏览器路径（`browser/`）SHALL 继续从 DOM 取元素，MUST NOT 调用本管线。

#### Scenario: 浏览器步骤不跑 OCR 与检测
- **WHEN** 浏览器路径执行一步
- **THEN** OCR、图标检测与图标描述都没有被调用

### Requirement: OSWorld 不再选择 OCR 后端
OSWorld 的运行入口（`scripts/osworld run-jev`、`run_multienv_jev.py`、`JevAgent`）MUST NOT 接受 `--ocr` 参数。每次运行的 `run.json` SHALL 记录感知管线版本与所用权重的提交哈希。

#### Scenario: 旧参数被拒绝
- **WHEN** 以 `scripts/osworld run-jev TASK --ocr rapidocr` 启动
- **THEN** 脚本报错并说明 `--ocr` 已删除

#### Scenario: 运行记录包含权重版本
- **WHEN** 一次 OSWorld 运行结束
- **THEN** `run.json` 中有感知管线版本与图标检测、图标描述权重的提交哈希
