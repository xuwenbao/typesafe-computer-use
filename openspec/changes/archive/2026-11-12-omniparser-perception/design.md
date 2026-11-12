## Context

桌面路径每一步的感知在 `perception.perceive()`：

```
截图 ─▶ ocr_region（前台窗口 + 菜单栏）─▶ desktop.recognize_text（Vision / winocr / RapidOCR）
     ─▶ 置信度与回显过滤 ─▶ merge_blocks（行并段）─▶ to_items
无障碍树 ─▶ to_ax_items ─▶ merge_with_origins（重叠 + 文字匹配；is_icon 丢掉图标符号）
     ─▶ kept_by_budget（255 上限）─▶ reading_order 编号 ─▶ items
items ─▶ decide()：state.screen_items_in_reading_order + item Choice（纯文字）
```

现状中与本设计相关的约束：

- OCR 约占一步的三分之二。`OcrCache` 只重读变化的 256 像素分块；`ReadAhead` 在查询前台应用和窗口（OSWorld 上是取无障碍树）的同时，在另一个线程里先读截图，并保证同一时刻只有一个 OCR 在跑。
- `merge_blocks` 等几何阈值是按 Vision 的框调的；`ocr_rapid.hug_ink` 把 RapidOCR 外扩的框收回到墨迹范围，否则侧边栏条目会被并成一段。
- `Desktop` 协议要求每个平台适配层实现 `recognize_text`，`tests/test_platform_adapter.py` 按参数逐一核对。OSWorld 的 `Desktop` 接收一个 `recognize_text` 回调，由 `--ocr` 选择。
- `runner` 每步把截图、标注图（`report.annotate`）、`payload.txt` 和答案写入运行目录。
- `decide()` 不指定 `model`，SDK 默认发送 `jev-latest`。`MeteredClassifier` 已按回复记录 `input_tokens`。
- `tests/conftest.py` 拒绝一切非回环网络连接，Hugging Face 下载在测试中天然被挡住。
- OmniParser 仓库不是可安装的包；`icon_detect_v3`（YOLOv9-E，TorchScript）与 Florence-2 图标描述权重均为 MIT。`icon_detect_v3` 目前只在 `microsoft/OmniParser-v2.0` 的 Hugging Face PR #37 中。

动机见 `proposal.md`。

## Goals / Non-Goals

**Goals:**

- 桌面路径识别出无文字的图标和按钮，并带上可读的描述。
- 三个桌面平台（macOS、Windows、OSWorld）共用一条感知管线、一个 OCR 引擎、一套几何阈值。
- 分类器每一步都能看到屏幕：一张编号与 `item` 选项一致的标注图。
- 在删除旧管线之前，用离线评测证明新管线在召回率和文字准确率上不低于旧管线。
- 每步耗时和 token 的变化被测量并写进文档。

**Non-Goals:**

- 浏览器路径（`browser/`）：继续从 DOM 取元素，不带图。
- 对截图中的输入内容做涂黑或跳过（已接受的风险）。
- 以独立服务形式运行 OmniParser。
- 使用 System One 的 Box 问题类型做定位。
- 升级 `typesafe-sdk`（除非 `extra_body` 无法携带 `assets`）。
- 训练或微调任何模型。

## Decisions

### 1. 复制算法进 jev，新建 `parse/` 子包

```
typesafe_computer_use/parse/
  ocr.py      RapidOCR + hug_ink + reading_order（由 ocr_rapid.py 迁入）
  detect.py   YOLOv9-E TorchScript 推理（移植 OmniParser util/yolov9.py）
  fuse.py     图标与文字去重融合（移植 remove_overlap_new，纯函数）
  caption.py  Florence-2 图标描述 + 缓存
  marks.py    给分类器的编号标注图
  weights.py  权重定位、下载、版本固定
```

`perception.py` 保留编排（区域、缓存、与无障碍树合并、预算、编号），调用 `parse/`。

- 备选：依赖 OmniParser 仓库。否决：不是包，`requirements.txt` 拉进 gradio、paddle、easyocr、ultralytics（AGPL）等。
- 备选：独立服务。否决：用户选定进程内。

`detect.py` 只用 torch 与 torchvision（`batched_nms`），不引入 `ultralytics`。`fuse.py` 不依赖 torch，便于离线单测。

### 1a. 平台支持：Intel Mac 不支持新管线

PyTorch 自 2.3 起不再发布 Intel Mac（`macosx_x86_64`）的安装包，最新版只有 Apple 芯片 Mac、Linux 与 Windows 的包。因此：

- `torch`、`torchvision`、`transformers` 及其配套依赖带环境标记 `not (sys_platform == 'darwin' and platform_machine == 'x86_64')`，Intel Mac 上不安装，`uv sync` 仍然成功。
- Linux 上 `torch`、`torchvision` 从 PyTorch 的 CPU 索引安装（`[tool.uv.sources]` 按平台指定），避免拉进几 GB 的 CUDA 包；OSWorld 主机与沙盒都是 CPU。
- `parse/` 的所有 torch 导入都放在函数里。Intel Mac 上导入 jev、跑离线测试都不受影响；只有真正要推理时才报错，错误信息写明平台不受支持。
- `clicker` 在桌面路径启动时先检查推理依赖是否可用，不可用就立即退出并说明原因，不等到第一步。

- 备选：改用 ONNX Runtime，模型在沙盒里转换后托管。否决：用户选择保留 torch。
- 备选：退回 torch 2.2.2（最后一个支持 Intel Mac 的版本）。否决：不支持 Python 3.13，要求 numpy 低于 2，与新版 transformers 不兼容。

### 2. 管线顺序：在"行"上融合，融合之后再并段

```
区域截图
 ├─▶ parse.ocr ─▶ 行（hug_ink 收紧）─▶ 置信度 / 回显 / 焦点输入框 过滤 ─┐
 └─▶ parse.detect ─▶ 图标框（YOLO 自身 NMS 后，再按"保留小框"去重）────┤
                                                                       ▼
                     parse.fuse：文字行落在图标内（≥80%）→ 并入图标作为其文字
                                 图标落在文字行内（≥80%）→ 丢掉图标
                                 其余图标 → 无文字图标
                                                                       ▼
            剩余文字行 ─▶ merge_blocks 并段     无文字图标 ─▶ parse.caption
                                                                       ▼
                               文字块 + 带字图标 + 带描述图标 ─▶ 与无障碍树合并
```

先融合再并段，是因为并段后的段落框很大，会把段落旁的小图标判成"落在文字内"而丢掉。融合阈值沿用 OmniParser 的 0.8，图标间去重的 IoU 沿用 0.7，坐标一律用截图像素，不做归一化。

- 备选：先 `merge_blocks` 再融合（与现有顺序一致）。否决：理由如上。
- 备选：完全照搬 OmniParser 的 `get_som_labeled_img`。否决：它把 OCR 当作输入、混合归一化坐标，还有下面第 5 条列出的两个边界缺陷。

`hug_ink` 保留在 `parse.ocr` 里，不再是"向 Vision 对齐的补丁"，而是唯一 OCR 引擎的框归一化。`merge_blocks` 阈值以 RapidOCR + `hug_ink` 的输出为基准，在评测集上重新校准。

### 3. 元素模型：扩展 `Item.source`，图标描述要能被区分

`Item` 不加字段，`source` 增加取值：

| source | 含义 | text 来源 | ocr_confidence |
|---|---|---|---|
| `ocr` | 文字块 | OCR | OCR 置信度 |
| `icon+ocr` | 图标框内有文字 | 框内 OCR 文字 | OCR 置信度最小值 |
| `icon` | 无文字图标 | Florence-2 描述 | YOLO 分数 |
| `ax` / `ax+ocr` | 不变 | 不变 | 不变 |
| `ax+icon` | 无障碍控件与图标框重合 | 控件 label；为空时用图标的文字或描述 | 不变 |

`icon` 项的 `role` 设为 `icon`，`item_criteria` 与 `base_state` 中写成 `icon 'gear, settings'`，让分类器知道这是描述而不是屏幕上的字。

`merge_with_origins` 增加一条：图标框与无障碍控件按 `box_overlap ≥ 0.5` 配对，不要求文字匹配（描述是英文、控件 label 可能是中文）。`is_icon()` 删除：它丢掉的图标符号现在由融合规则处理。`kept_by_budget` 的淘汰顺序为：`ocr` 低置信度 → `icon` 低分 → 其余；无障碍控件永不为文字或图标让位。

### 4. 增量识别与预读

- OCR：保留 `OcrCache` 的分块增量逻辑，只是引擎换成 `parse.ocr`。缓存从"OCR 行"扩展为"OCR 行 + 图标框"，改名 `ParseCache`。
- YOLO：区域内没有分块变化时直接复用上一步的图标框；有变化时对整个区域重跑。不对裁剪块做增量检测，因为 YOLO 在裁剪图上的结果和整图不一致，拼接得不偿失。
- `ReadAhead` 扩展为预先完成 OCR 与 YOLO 两步；Florence-2 不预读（依赖融合结果）。
- 同一时刻只有一个推理在跑：OCR、YOLO、Florence-2 共用一把进程级锁，沿用 `ReadAhead` "先等上一个读完"的做法。

### 5. 图标描述：Florence-2 的移植与缓存

- 裁剪 → 缩放到 64×64 → 提示词 `<CAPTION>` → 贪心解码，最多 20 个新 token。与 OmniParser 一致，保证描述与其训练分布相符。
- 缓存：键是 64×64 裁剪图 RGB 字节的 SHA-256，值是描述文本。贪心解码是确定性的，所以同一图标永远同一描述。缓存属于一次运行，上限 1024 条，按最近最少使用淘汰。
- 每步只描述缓存未命中的图标，按批推理（默认每批 32，可调）。
- 修掉 OmniParser 的两个缺陷：裁剪失败时不静默跳过，而是给该图标一个空描述占位，保证描述与图标一一对应；没有无文字图标时不调用模型。
- 设备：CUDA > MPS > CPU；CUDA 用 float16，其余 float32。
- 输入尺寸：64×64 原样送入（`do_resize=False`，OmniParser 的 CUDA 路径），视觉塔每边缩小 32 倍，得到 2×2 网格加 1 个池化 token，共 5 个图像 token。沙箱实测（6 核 x86 CPU）：原样送入 4 个图标约 1 秒，描述为短标签（"Find"、"Close"、"Add"）；放大到 768×768 每个图标约 20 秒，描述为整句。后者在 CPU 上不可用，故固定为原样送入。
- 加载方式（任务 2.5 的结论）：用 `transformers` 自带的 `Florence2ForConditionalGeneration`，不执行任何远程代码。微软的远程代码在 transformers 5.22 上加载即报错（`Florence2LanguageConfig` 缺 `forced_bos_token_id`）；原生类又不认 OmniParser 检查点的旧配置格式。做法是：配置、处理器与分词器取自 MIT 许可的 `florence-community/Florence-2-base-ft`（固定提交），权重仍是 OmniParser 的 `icon_caption/model.safetensors`，加载时按 `caption.RENAMES` 改名后 `strict=True` 装入，一个不缺一个不多。三处需要特别处理：
  - 词表：检查点 51289 个 token，原生类 51328 个（含图像占位符 51289）。嵌入与输出层用零补齐，生成时用 `suppress_tokens` 屏蔽补齐的 token。
  - 输出层：检查点的 `lm_head` 与共享嵌入不相同（单独训练），所以关闭权重绑定；编码器与解码器的 `embed_tokens` 指向共享嵌入。
  - `image_projection` 以转置形式存储；`final_logits_bias` 全为零，原生类没有这一项，丢弃。
- 已知现象：对空白区域（检测误报）会输出无意义文字（例如 SVG 路径片段）。融合阶段需要过滤这类图标，在第 4 组集成时处理。

### 6. 权重：首次使用时下载，版本固定

- `parse/weights.py` 用 `huggingface_hub` 下载到默认 Hugging Face 缓存，每个文件固定到具体提交哈希：`microsoft/OmniParser-v2.0` 的 `icon_detect_v3/model.pt` 与 `icon_caption/model.safetensors`（两者校验 SHA-256），以及 `florence-community/Florence-2-base-ft` 的配置、处理器与分词器文件。
- 模型在第一次使用时加载（`functools.cache`），导入 `parse/` 不加载任何东西，与 `ocr_rapid._engine` 的做法一致。
- 提供预取入口（例如 `clicker-models fetch`），`scripts/osworld setup` 调用它，避免第一步卡在下载上。运行中发现权重缺失时下载并打一行日志；遵守 `HF_HUB_OFFLINE`。
- 不使用 `trust_remote_code`：Florence-2 用 `transformers` 原生类加载（见决策 5）。
- YOLO 的 TorchScript 关闭 profiling executor：它在前几次调用里反复重新优化计算图，沙箱中首次检测 24 秒（关闭后 12–14 秒），之后几次也更慢且不稳定；关闭后稳定在约 2 秒。

### 7. 平台适配层：删除 `recognize_text`

- `Desktop` 协议删掉 `recognize_text`；`macos.py` 删 `ocrmac`，`windows.py` 删 `winocr`，`tests/test_platform_adapter.py` 随之更新。
- OSWorld：`osworld/ocr.py` 删除；`osworld.desktop.Desktop` 不再接收 `recognize_text`；`JevAgent`、`run_multienv_jev.py`、`scripts/osworld run-jev` 删 `--ocr`。`run.json` 里原先记录的 OCR 后端，改为记录感知管线版本和权重提交哈希。
- `tests/conftest.py`：删掉 `ocrmac`、`winocr` 的替身；新增守护，让 `parse.detect`、`parse.caption`、`parse.ocr` 的模型加载在测试中直接报错，需要它们的测试自己打桩。网络守护已挡住下载，这一条是为了让测试不偷偷加载几百 MB 的模型。

### 8. 给分类器的标注图

- **画什么**：用最终的 items（合并、裁剪、重新编号之后）画，不用 OmniParser 自带的标注图，因为只有最终编号才与 `item` 选项一致。每个 item 画框和编号，颜色按来源区分（无障碍控件、文字、图标）。
- **画在哪**：裁剪到 `ocr_region`（前台窗口加菜单栏），所有 item 都在其中，同样的像素预算下分辨率更高。
- **尺寸**：最长边缩到不超过 1568；任一边不足 280 时等比放大，仍不足则补边；总像素在 78,400 到 2,458,624 之间。编号字号和线宽按缩放后的尺寸计算，保证最终图里编号至少约 14 像素高。
- **格式**：PNG（屏幕文字边缘比 JPEG 清楚；1568×980 的截图 PNG 远低于 16 MiB）。
- **怎么发**：`state` 增加 `"screen_image": <uuid>`，`extra_body={"assets": {<uuid>: {"type": "image", "format": "png", "data_base64": ...}}}`，`extra_headers={"TypeSafe-Danger-Zone-Images": "true"}`。引用每步新生成 UUID。`item` 问题的说明加一句"每个 item 的编号画在 `screen_image` 中它的框旁边"。
- **哪些调用带图**：只有 `decide()`。`verify_typed()` 只看字段值，不带；浏览器路径不带；writer 与 answer 模型原本就有自己的截图，不变。
- **记录**：运行目录多存一张 `NNN-sent.png`（实际发出的图）；`payload.txt` 只写引用和尺寸，不写 base64。每步的 `input_tokens` 照常由 `MeteredClassifier` 记录。
- **图片被拒时**：`400`/`413` 且原因与图片相关时，这一步不带图重试一次，并在日志和 `run.json` 里记下。理由是 Dreamer-ready 接口随时可能变，一次接口变动不该让所有运行直接失败。
  - 备选：直接报错停止。更"诚实"，但会让每次 API 变动都打断所有运行。

### 9. 离线评测

- **评测集**：每张截图配一份 `labels.json`，列出可点击目标：框、类型（`text` / `icon`）、可见文字（文字目标）。来源是以往运行目录里的 `*-raw.png`、OSWorld 结果截图，以及用户提供的失败案例；覆盖中文、小字、纯图标工具栏、Linux 桌面、Windows。
- **度量**：
  - 目标召回率：有一个 item 的中心落在目标框内即算命中，分 `text`、`icon` 两类统计。
  - 文字准确率：命中的文字目标上的字符错误率（CER）。
  - 每张图耗时：分 OCR、YOLO、描述三段。
- **工具**：`scripts/perception-eval`，对一个目录跑当前管线并输出 JSON 报告；可以对比两份报告。只读图片文件，不截屏。
- **基线**：在任何删除之前，用旧管线跑出基线报告并提交到仓库。macOS 基线要调用 Vision OCR（经平台适配层），按 `AGENTS.md` 每次运行需要批准。
- **合并门槛**：新管线 `text` 召回率不低于基线，CER 不高于基线，`icon` 召回率显著高于基线（基线几乎为零）。耗时如实报告，不设门槛，但要写进 README。

## Risks / Trade-offs

- [每步变慢：RapidOCR 跑在 CPU 上，再加 YOLO 和 Florence-2] → 保留分块增量与预读；屏幕不变时复用图标框；描述按哈希缓存；实测后更新 README 的"每步约 1.5 秒"。
- [Retina 截图缩到 640 送 YOLO，小图标只剩几个像素，可能漏检] → 评测集里专门放 Retina 工具栏；检测输入尺寸做成可调参数，由评测决定 640 还是 1280。
- [RapidOCR 在某些文字上不如 Vision，例如 Retina 上的小号英文] → 合并门槛要求文字召回率和 CER 不劣于基线；不达标就不合并。
- [Florence-2 只看 64×64 裁剪图，没有上下文，描述是英文] → 分类器同时看到标注图，可从图上补回上下文；图标项标记为 `icon`，与屏幕文字区分。
- [每步带图，分类器输入 token 成倍增加，冲击"每个任务约 0.003 美元"] → 只带一张裁剪后的标注图，放在 `state` 里只放一次；实测后更新 README。
- [图片接口是 Dreamer-ready，格式和准确率会变] → 发送逻辑集中在一个函数；图片被拒时不带图重试一次并记录。
- [截图里可见的输入内容（密码、验证码等）随每步发给 System One] → 已接受的风险；写进 `docs/known-limits.md` 和 README。
- [依赖变重：torch、transformers 加几百 MB 权重，Windows 和 OSWorld 环境各装一份] → 权重首次使用时下载、可预取；Linux 用 CPU 版 torch；文档写明磁盘与安装时间。
- [Intel Mac 失去桌面路径，包括维护者目前的开发机] → 已接受；启动时明确报错；新管线的实测在沙盒或 Apple 芯片 Mac 上进行；浏览器路径不受影响。
- [原生类加载依赖一张改名表，transformers 升级可能改动原生类的参数名] → `strict=True` 加载，名字一旦对不上立即报错；升级 transformers 时在沙箱里重跑描述冒烟测试。
- [`icon_detect_v3` 只在一个未合并的 PR 里，PR 可能变动或被关闭] → 固定提交哈希；在仓库里记下文件的 SHA-256，下载后校验。
- [图标框与无障碍控件重复，两个选项指向同一次点击，分散投票] → 按重叠度配对合并为 `ax+icon`；评测时检查重复率。
- [torch 在预读线程里与主线程争 CPU] → 一把进程级推理锁；限制 torch 线程数。
- [旧管线删掉后就无法再对比] → 基线报告在删除前生成并提交；删除放在单独的提交里，必要时可以只回滚这一步。

## Migration Plan

1. **评测先行**：建评测集与 `scripts/perception-eval`；用旧管线跑出基线报告并提交（macOS 那次运行需要批准）。
2. **新管线并入**：新增 `parse/`，`perceive()` 改走新管线；`decide()` 带标注图；更新单测与 `conftest.py` 守护。这一步旧的 `recognize_text` 还在，但不再被感知层调用。
3. **评测与实测**：在评测集上对比基线，达到门槛后，再跑 OSWorld 和本机实测（每次运行需要批准），记录耗时与 token。
4. **删除旧管线**（单独提交）：删 `ocrmac`、`winocr`、`Desktop.recognize_text`、`osworld/ocr.py`、`--ocr`；`rapidocr` 改为必需依赖；更新脚本与文档（README、`docs/how-a-step-works.md`、`docs/osworld.md`、`docs/windows.md`、`docs/known-limits.md`）。

回滚：在步骤 4 之前，回滚步骤 2 的提交即可回到旧管线；步骤 4 之后，按提交逆序回滚。

## Open Questions

- YOLO 的检测输入尺寸：640（OmniParser 默认）还是 1280，或按截图分辨率自适应？由评测集上的 `icon` 召回率与耗时决定。
- 描述器首次加载约 25 秒（先随机初始化整个模型再覆盖权重）。可改为在 meta 设备上构建再 `assign=True` 装入，待第 4 组集成后视实际影响决定。
- 评测集放在哪里：截图可能含个人信息且体积大。倾向于提交一小份脱敏样本，完整集放在 git 之外的本地目录。

已确认（不再是开放问题）：

- `typesafe-sdk==0.6.0` 的 `prepare_system_one` 用 `body.update(extra_body)` 合并，`assets` 会原样出现在请求 JSON 顶层，不必升级 SDK。
- `decide()` 不指定 `model` 时，SDK 发送 `jev-latest`（可由 `TYPESAFE_DEFAULT_MODEL` 覆盖）；按文档 Jev 1.13 与 1.14 都接受图片。若环境变量把模型钉在更早的版本，图片请求会被拒，届时走第 8 条的"不带图重试"并在日志里写明原因。
