# Spec Delta

## MODIFIED Requirements

### Requirement: 评测集覆盖面
仓库内提交的评测样本 SHALL 至少覆盖：中文界面、Retina 上的小号文字、纯图标工具栏、WAA 的 Windows 11 虚拟机桌面与 Windows 桌面。提交的样本 MUST NOT 含有个人信息；完整评测集可以放在 git 之外的本地目录，由评测工具按路径读取。

#### Scenario: 样本覆盖检查
- **WHEN** 列出仓库内评测样本的标签
- **THEN** 上述五类各至少有一张截图
