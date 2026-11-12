# Spec Delta

## MODIFIED Requirements

### Requirement: 桌面路径统一的感知管线
macOS、Windows 与 WAA 的桌面路径 SHALL 使用同一条进程内感知管线，把一张截图变成可点击元素：RapidOCR 识别文字，YOLOv9-E（`icon_detect_v3`）检测图标，二者融合后再与无障碍树合并。感知结果 MUST NOT 依赖运行所在的平台；平台适配层 MUST NOT 提供文字识别。

#### Scenario: 三个平台得到相同的元素
- **WHEN** 同一张截图分别经由 macOS、Windows 与 WAA 的感知入口处理，且无障碍树为空
- **THEN** 三者产出的元素列表（文字、框、来源、编号）完全相同

#### Scenario: 平台适配层不再提供文字识别
- **WHEN** 检查 `Desktop` 协议及 `macos.py`、`windows.py`、WAA 的 `Desktop`
- **THEN** 其中都没有 `recognize_text`，也不导入 `ocrmac` 或 `winocr`

## ADDED Requirements

### Requirement: WAA 不选择 OCR 后端
WAA 的运行入口（`scripts/waa run` 与 jev 的 WAA agent）MUST NOT 接受选择 OCR 后端的参数。每次运行的 `run.json` SHALL 记录感知管线版本与所用权重的提交哈希。

#### Scenario: 旧参数被拒绝
- **WHEN** 以 `scripts/waa run TASK --ocr rapidocr` 启动
- **THEN** 脚本报错并说明不支持 `--ocr`

#### Scenario: 运行记录包含权重版本
- **WHEN** 一次 WAA 运行结束
- **THEN** `run.json` 中有感知管线版本与图标检测、图标描述权重的提交哈希

## REMOVED Requirements

### Requirement: OSWorld 不再选择 OCR 后端
**Reason**: OSWorld 集成整体删除，由 WAA 取代。
**Migration**: 改用 `scripts/waa run`；同样的约束见「WAA 不选择 OCR 后端」。
