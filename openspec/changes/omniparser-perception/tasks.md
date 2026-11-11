## 1. 评测集与评测工具（先于任何感知改动）

- [x] 1.1 定义 `.labels.json` 格式（框、`text`/`icon` 类型、文字、可选读取区域），写校验函数与单测：合法标注通过，缺 `text` 的文字目标报错并指出文件与序号
- [x] 1.2 实现度量：中心落在框内的命中判定、`text`/`icon` 分类召回率、CER，配单测（含 "设置"→"没置" CER 为 0.5）
- [x] 1.3 实现 `scripts/perception-eval`：对一个目录运行当前感知管线（不含无障碍树），输出 JSON 报告（总体与逐张度量、分段耗时、管线版本）；只读图片文件，不截屏、不发输入、不调无障碍接口
- [x] 1.4 实现两份报告的对比：两类召回率、CER 与耗时的差值，以及召回状态变化的目标
- [ ] 1.5 收集并标注评测样本：中文界面、Retina 小号文字、纯图标工具栏、OSWorld Linux 桌面、Windows 桌面各至少一张；提交脱敏的小样本，完整集放在 git 之外的本地目录
- [ ] 1.6 用旧管线跑出基线报告并提交（macOS 那次要调用 Vision OCR，先提出确切命令，经批准后再运行）

## 2. 依赖与权重

- [x] 2.1 在 `pyproject.toml` 加入 `torch`、`torchvision`、`transformers`、`huggingface_hub`（带标记，Intel Mac 不装；Linux 走 PyTorch CPU 索引）；把 `rapidocr`、`onnxruntime` 移入必需依赖（暂不删除 `ocrmac`、`winocr`），更新 `uv.lock`，确认 Intel Mac 上 `uv sync` 成功
- [x] 2.6 实现推理依赖可用性检查：`clicker` 桌面路径启动时在 Intel Mac 等不支持的平台上立即退出并说明原因；配单测
- [x] 2.2 查明 `icon_detect_v3/model.pt`（PR #37）、`icon_caption/model.safetensors`、`florence-community/Florence-2-base-ft` 配置与处理器的提交哈希，以及 `model.pt` 的 SHA-256，写入 `parse/weights.py`
- [x] 2.3 实现 `parse/weights.py`：按固定提交下载到 Hugging Face 缓存、校验 `model.pt` 的 SHA-256、遵守 `HF_HUB_OFFLINE`（缺权重时报错并提示预取命令），配单测（下载用桩）
- [x] 2.4 新增预取命令（如 `clicker-models fetch`），在 `pyproject.toml` 注册入口，并让 `scripts/osworld setup` 调用它
- [x] 2.5 确认 `transformers` 原生 Florence-2 类能否加载 OmniParser 的微调权重：能则改用原生类，不能则只对固定提交使用 `trust_remote_code=True`；把结论写回 `design.md`

## 3. `parse/` 感知模块

- [x] 3.1 新建 `typesafe_computer_use/parse/` 子包；把 `ocr_rapid.py` 的 RapidOCR、`hug_ink`、`reading_order` 迁入 `parse/ocr.py`，原单测随之迁移
- [x] 3.2 移植 YOLOv9-E TorchScript 推理到 `parse/detect.py`（letterbox、三层特征图解码、置信度过滤、`batched_nms`、坐标还原与裁剪），检测输入尺寸做成参数；不依赖 `ultralytics`
- [x] 3.3 实现 `parse/fuse.py`（纯函数，不依赖 torch）：图标间 IoU 0.7 保留小框、文字行 80% 落在图标内则并入、图标 80% 落在文字行内则丢弃；为规格中的每个融合场景写单测
- [x] 3.4 实现 `parse/caption.py`：裁剪、缩放到 64×64、`<CAPTION>`、贪心解码最多 20 个 token、按批推理；裁剪失败给空描述占位，没有无文字图标时不调用模型；配单测（模型用桩）
- [x] 3.5 实现描述缓存：64×64 裁剪图 RGB 字节的 SHA-256 为键，每次运行最多 1024 条，按最近最少使用淘汰；单测覆盖命中、淘汰与"命中不送模型"
- [x] 3.6 实现进程级推理锁，OCR、YOLO 与 Florence-2 共用；限制 torch 线程数；设备选择 CUDA > MPS > CPU，CUDA 用 float16，其余 float32
- [x] 3.7 在 `tests/conftest.py` 加守护：测试中加载 OCR、YOLO 与 Florence-2 模型直接报错，并给出供测试使用的桩

## 4. 接入感知层

- [x] 4.1 `Item.source` 增加 `icon+ocr`、`icon`、`ax+icon`；`icon` 元素的 `role` 设为 `icon`；`item_criteria` 与 `base_state` 把无文字图标写成 `icon '<描述>'`
- [x] 4.2 改写 `perception.ocr()` 的管线顺序：读取区域 → OCR 行（收紧）与 YOLO → 过滤 → 融合 → 剩余文字行 `merge_blocks` 并段 → 描述无文字图标；输出整张截图坐标
- [x] 4.3 `merge_with_origins` 增加图标框与无障碍控件按重叠（≥0.5）配对、不要求文字匹配的规则，得到 `ax+icon`（label 为空时用图标文字或描述），保留无障碍引用；删除 `is_icon()`
- [x] 4.4 调整 `kept_by_budget` 的淘汰顺序：低置信度 `ocr` 先，其次低分 `icon`，无障碍元素不让位；补单测
- [x] 4.5 把 `OcrCache` 扩展为 `ParseCache`（OCR 行 + 图标框）：无分块变化时复用图标框，有变化时整区重新检测，文字仍按分块增量重读；更新 `tests/test_ocr_cache.py`
- [x] 4.6 扩展 `ReadAhead`：预读同时完成 OCR 与 YOLO，在推理锁下运行，采用条件不变（截图、应用、窗口、缩放一致）
- [ ] 4.7 用评测集重新校准 `merge_blocks` 阈值（以 RapidOCR + `hug_ink` 为基准），更新相关单测
- [x] 4.8 更新 `report.annotate`，按来源给图标元素单独配色；更新 `clicker-inspect` 的输出

## 5. 分类器标注图

- [x] 5.1 实现 `parse/marks.py`：用最终 items 在读取区域裁剪图上画框和编号，颜色按来源区分；缩放到最长边不超过 1568，不足 280 时等比放大或补边，总像素在限制内；编号在最终图中至少 14 像素高；输出 PNG；为规格中的尺寸场景写单测
- [ ] 5.2 `decide()` 在 `state` 中加 `screen_image`（每步新 UUID），通过 `extra_body` 发送 `assets`、通过 `extra_headers` 发送 `TypeSafe-Danger-Zone-Images: true`；`item` 问题说明加上"编号画在 `screen_image` 中元素框旁"；单测检查请求结构
- [ ] 5.3 实现图片被拒时不带图重试一次：只针对与图片相关的 `400`/`413`，在日志与 `run.json` 中记录；无关错误照常抛出；补单测
- [ ] 5.4 确认 `verify_typed()` 与浏览器路径的请求不带 `assets` 和图片请求头，补单测
- [ ] 5.5 `runner` 每步保存 `NNN-sent.png`；`render_payload` 写图片引用与宽高，不写 base64；更新 `docs/run-folder.md`

## 6. 评测与实测

- [ ] 6.1 在评测集上运行新管线并与基线对比；用评测结果决定 YOLO 输入尺寸（640 或 1280）（Florence-2 输入尺寸已在 2.5 定为 64 原样送入），把结论写回 `design.md`
- [ ] 6.2 确认达到合并门槛：`text` 召回率不低于基线，CER 不高于基线，`icon` 召回率高于基线；未达到则回到第 4 组调整
- [ ] 6.3 在本机实测若干任务，记录每步耗时与分类器 `input_tokens`（先提出确切命令，经批准后再运行）
- [ ] 6.4 跑一批 OSWorld 任务，对比改动前的成功率、每步耗时与 token（每次运行先提出确切命令，经批准后再运行）

## 7. 删除旧管线（单独提交）

- [ ] 7.1 从 `Desktop` 协议删除 `recognize_text`；`macos.py` 删 `ocrmac` 与 `OCR_LANGUAGES`，`windows.py` 删 `winocr`；更新 `tests/test_platform_adapter.py`
- [ ] 7.2 删除 `osworld/ocr.py`；`osworld.desktop.Desktop` 不再接收 `recognize_text`；`JevAgent`、`run_multienv_jev.py`、`scripts/osworld run-jev` 删 `--ocr`（传入时报错并说明已删除）；`run.json` 记录管线版本与权重提交哈希；更新 OSWorld 相关测试
- [ ] 7.3 从 `pyproject.toml` 删除 `ocrmac`、`winocr` 与 `rapidocr` 可选依赖组；删除 `ocr_rapid.py`；从 `tests/conftest.py` 删去它们的替身；更新 `uv.lock`
- [ ] 7.4 确认 `browser/` 没有引用任何新模块，浏览器路径测试全部通过

## 8. 文档

- [ ] 8.1 README：按实测更新"每个任务约 0.003 美元"与"每步约 1.5 秒"，写明安装体积与首次下载权重；加入截图内容发给 System One 的披露
- [ ] 8.2 `docs/how-a-step-works.md`：重写感知一节（融合、图标描述、缓存、标注图）
- [ ] 8.3 `docs/known-limits.md`：加入截图中可见输入内容会发给 System One、图片接口为 Dreamer-ready、图标描述是英文
- [ ] 8.4 `docs/osworld.md` 与 `docs/windows.md`：删除 `--ocr` 与 Windows OCR 的说明，加入预取权重的步骤
- [ ] 8.5 `OBSERVATIONS.md`：记下评测与实测得出的结论，每条一句
