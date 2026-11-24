## 1. clicker setup

- [x] 1.1 `helper/setup.py`：预览、JSON 合并与 `.bak`、Codex 追加、Claude Code 命令、`--skill`、`--key`
- [x] 1.2 `helper/cli.py`：`setup` 子命令，函数内导入 `helper.setup`

## 2. 技能与提示词

- [x] 2.1 `skills/jev-install`：触发条件、安装流程、宿主路径、故障排查
- [x] 2.2 `skills/jev`：使用方式与十个工具
- [x] 2.3 `docs/agent-install.md`，并在 README 与 `docs/helper.md` 链接

## 3. 打包

- [x] 3.1 `clicker.iss` 把 `skills\jev` 装到 `{app}\skills\jev`
- [x] 3.2 `build-helper.ps1` 写出 `clicker-setup-<版本>.sha256`

## 4. 测试

- [x] 4.1 `tests/test_helper_setup.py`、cli 与导入边界
- [x] 4.2 `openspec validate agent-install --strict`，ruff 与离线 pytest
