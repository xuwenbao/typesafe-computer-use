## Why

桌面路径的感知只认文字：OCR 看不见无文字的图标和按钮（`VISION.md` 点名的盲区），三个平台各用一个 OCR 引擎（macOS Vision、Windows `winocr`、OSWorld RapidOCR），框的几何口径不一，文字也有错有漏；分类器只读到一份文字列表，看不到屏幕本身。OmniParser 的图标检测与 System One 新开放的图片输入，正好分别补上"认不出图标"和"看不见屏幕"这两块。

## What Changes

- 桌面路径（macOS、Windows、OSWorld）的感知换成一条进程内的 OmniParser 管线：RapidOCR 识别文字，YOLOv9-E（`icon_detect_v3`）检测图标框，按 OmniParser 的规则去重融合，Florence-2 为无文字的图标生成描述（按图标裁剪图的哈希缓存）。输出元素列表和一张编号标注图（Set-of-Mark）。
- 所需算法从 OmniParser 复制进 jev，不依赖 OmniParser 仓库，不引入 `ultralytics`、EasyOCR、Paddle。
- 管线产出的元素继续与无障碍树（macOS AX、Windows UIA）合并；无障碍控件仍可按 ref 直接按下，图标描述只在控件没有 label 时使用。
- 每一步的 `decide()` 请求在 `state` 里带上编号标注图：缩到每边不超过 1568 像素，图上编号与 `item` 选项编号一致，请求带 `TypeSafe-Danger-Zone-Images: true` 头。暂不升级 `typesafe-sdk`，通过 `extra_body` 和 `extra_headers` 发送。
- **BREAKING** 删除 macOS Vision OCR（`ocrmac`）与 Windows OCR（`winocr`），所有平台统一用 RapidOCR；`rapidocr` 由可选依赖变为必需依赖。
- **BREAKING** 从 `Desktop` 协议中删除 `recognize_text`：平台适配层只负责截图、输入与无障碍树，感知层不再区分平台。
- **BREAKING** 删除 OSWorld 的 `--ocr` 参数（`scripts/osworld run-jev`、`run_multienv_jev.py`、`JevAgent`），以及 `osworld/ocr.py` 的后端选择。
- **BREAKING** 截图里可见的内容，包括输入框中的值，会随每一步发给 System One。这是已接受的风险，改变了项目目前"桌面路径不发送焦点输入框内容"的做法。
- 新增离线感知评测：一批保存的截图加人工标注的可点击目标，在删除旧管线之前先跑出旧管线的基线，新管线须在同一评测集上不低于基线才合并。
- 浏览器路径（`browser/`，从 DOM 取元素）不改动，不带图。
- 许可证保持 MIT：只用 MIT 许可的 `icon_detect_v3` 与 Florence-2 权重，固定其 Hugging Face 修订版本。
- **BREAKING** 新管线依赖 PyTorch，而 PyTorch 不再为 Intel 芯片的 Mac 提供安装包。删除旧管线之后，桌面路径不再支持 Intel Mac；支持的平台为 Apple 芯片的 Mac、Windows（x86_64）与 Linux（OSWorld）。浏览器路径在 Intel Mac 上仍可用。

## Capabilities

### New Capabilities

- `screen-perception`：桌面路径如何把一张截图变成可点击元素。包括 RapidOCR 文字、YOLOv9-E 图标、去重融合、Florence-2 描述及其缓存、与无障碍树的合并、编号标注图的生成，以及增量识别与权重加载的约束。
- `classifier-screen-image`：分类器请求如何携带屏幕图片。包括哪张图、尺寸与格式限制、编号与选项的对应、请求头、放在 `state` 中只放一次、适用于哪些调用（`decide()` 带，`verify_typed()` 与浏览器路径不带），以及 token 用量的记录。
- `perception-evaluation`：离线感知评测。包括评测集的组成、标注格式、度量（可点击目标召回率、文字准确率、每步耗时），以及"先跑旧管线基线、新管线不低于基线才合并"的门槛。

### Modified Capabilities

（无。`openspec/specs/` 目前没有既有规格。）

## Impact

- **代码**：`perception.py`（`ocr()`、`OcrCache`、`merge_blocks`、`merge_with_origins`、`is_icon`）、`ocr_rapid.py`（`hug_ink` 的去留）、`platform_adapter.py`、`macos.py`、`windows.py`、`osworld/ocr.py`、`osworld/agent.py`、`osworld/desktop.py`、`decide.py`、`cli.py`；新增感知管线模块（YOLOv9 推理、去重融合、Florence-2 描述、标注图绘制）。
- **依赖**：新增 `torch`、`torchvision`、`transformers` 及运行时下载的权重（几百 MB），Intel Mac 上不安装；Linux 上从 PyTorch 的 CPU 索引安装，避免拉进几 GB 的 CUDA 包；`rapidocr` 与 `onnxruntime` 改为必需；删除 `ocrmac`、`winocr`。
- **平台**：Intel Mac 不再支持桌面路径。维护者目前的开发机就是 Intel Mac，新管线的真实运行要在沙盒（Linux 容器）或 Apple 芯片的 Mac 上进行。
- **测试**：`tests/test_ocr_rapid.py`、`tests/test_ocr_cache.py`、`tests/test_perception.py`、`tests/test_platform_adapter.py`、OSWorld 相关测试需要改写；`tests/conftest.py` 的机器守护需覆盖新增的调用（权重下载不得在测试中发生）；新增评测集与离线评测。
- **脚本**：`scripts/osworld`、`osworld_overlay/scripts/python/run_multienv_jev.py`。
- **文档**：`README.md`（"每个任务约 0.003 美元""每步约 1.5 秒"按实测更新）、`docs/how-a-step-works.md`、`docs/osworld.md`、`docs/windows.md`、`docs/known-limits.md`。
- **成本与性能**：每步耗时（RapidOCR 跑在 CPU 上、YOLO、Florence-2）与分类器输入 token（每步一张图）都会上升，需要实测并记录。
- **隐私**：截图里可见的输入内容会离开本机，发给 System One。
- **不受影响**：浏览器路径（`browser/`）。
