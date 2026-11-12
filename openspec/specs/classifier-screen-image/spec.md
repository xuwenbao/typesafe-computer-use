# classifier-screen-image

## Purpose

分类器请求如何携带屏幕标注图。Purpose 细节待补。

## Requirements

### Requirement: 每一步的决策请求携带标注图
桌面路径每一步的 `decide()` 请求 SHALL 携带一张屏幕标注图：在 `state` 中以键 `screen_image` 引用一次，图片本身放在请求顶层的 `assets` 中，并带请求头 `TypeSafe-Danger-Zone-Images: true`。引用 SHALL 是每步新生成的 UUID，且 MUST NOT 出现在请求的其他文本中。

#### Scenario: 请求结构
- **WHEN** 桌面路径执行一步决策
- **THEN** 请求的 `state.screen_image` 是一个 UUID，`assets` 中恰好有一项以该 UUID 为键、`type` 为 `image`、`format` 为 `png`，请求头包含 `TypeSafe-Danger-Zone-Images: true`

#### Scenario: 每步引用不同
- **WHEN** 连续执行两步决策
- **THEN** 两步的 `screen_image` 引用不同

### Requirement: 标注图的内容与编号
标注图 SHALL 用本步最终的元素列表（合并、预算淘汰与重新编号之后）绘制：每个元素画出框和编号，编号 MUST 与 `item` 问题中该元素的选项键一致。框的颜色 SHALL 按来源区分无障碍控件、文字与图标。`item` 问题的说明 SHALL 告诉模型编号画在 `screen_image` 中元素框的旁边。

#### Scenario: 编号与选项一致
- **WHEN** 某元素在 `item` 问题中的选项键为 "17"
- **THEN** 标注图中该元素的框旁画着 "17"

#### Scenario: 被淘汰的元素不画
- **WHEN** 某个 OCR 文字块在预算淘汰中被丢弃
- **THEN** 标注图中没有它的框

### Requirement: 标注图的范围、尺寸与格式
标注图 SHALL 裁剪到本步的读取区域（前台窗口加菜单栏条），并缩放到符合 System One 的限制：最长边不超过 1568 像素；任一边不足 280 像素时等比放大，仍不足则补边；总像素在 78,400 到 2,458,624 之间。编号字号与线宽 SHALL 按缩放后的尺寸计算，使最终图中编号高度不小于 14 像素。格式 SHALL 为 PNG。

#### Scenario: Retina 截图被缩小
- **WHEN** 读取区域为 2880×1800 像素
- **THEN** 发送的图片为 1568×980 像素

#### Scenario: 窄小的区域被补到下限
- **WHEN** 读取区域为 1000×200 像素
- **THEN** 发送的图片两边都不小于 280 像素，且原内容未变形

#### Scenario: 编号清晰可读
- **WHEN** 图片被缩小到原来的一半
- **THEN** 最终图中每个编号的高度不小于 14 像素

### Requirement: 只有桌面决策带图
图片 SHALL 只随桌面路径的 `decide()` 发送。`verify_typed()`、浏览器路径的所有请求 MUST NOT 携带图片或图片请求头。writer 与 answer 模型的输入不变。

#### Scenario: 输入校验不带图
- **WHEN** 执行一次 `verify_typed()`
- **THEN** 请求中没有 `assets`，也没有 `TypeSafe-Danger-Zone-Images` 请求头

#### Scenario: 浏览器路径不带图
- **WHEN** 浏览器路径执行一步决策
- **THEN** 请求中没有 `assets`

### Requirement: 图片被拒时不带图重试
当带图的决策请求返回 `400` 或 `413`，且原因与图片或图片开关有关时，系统 SHALL 不带图重试这一步一次，并在日志与 `run.json` 中记录拒绝原因和重试。与图片无关的错误 MUST 照常抛出，不做此重试。

#### Scenario: 图片被拒后重试
- **WHEN** 带图请求返回 `400`，原因是图片尺寸不合规
- **THEN** 同一步以不带图的请求重试一次，日志中有一行说明拒绝原因

#### Scenario: 无关错误不重试
- **WHEN** 带图请求返回 `401` 认证失败
- **THEN** 错误照常抛出，不发起不带图的重试

### Requirement: 记录与可追溯
每一步 SHALL 把实际发送的标注图保存到运行目录（`NNN-sent.png`）。`NNN-payload.txt` SHALL 记录图片引用与尺寸，MUST NOT 包含 base64 数据。每次回复的 `input_tokens` SHALL 照常计入分类器用量。

#### Scenario: 运行目录内容
- **WHEN** 一步决策完成
- **THEN** 运行目录中有 `NNN-sent.png`，`NNN-payload.txt` 中有 `screen_image` 的引用与宽高，但没有 base64 字符串

### Requirement: 截图内容的披露
文档 SHALL 写明：截图中可见的内容，包括输入框中的值，会随每一步发送给 System One；系统不做涂黑或跳过。

#### Scenario: 文档中有披露
- **WHEN** 阅读 `docs/known-limits.md` 与 README
- **THEN** 两处都写明截图中可见的输入内容会发给 System One
