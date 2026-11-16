## 1. 用户目录与工具

本次只做 Windows。`jev mcp` 在自己的进程里操作屏幕，所有工具都直接动作，并且不向用户或智能体发问。所有测试都在离线测试里运行，不触及本机；在本机运行 `jev mcp` 的验证，都要先得到维护者批准。

- [x] 1.1 `helper/home.py`：`JEV_HOME`，否则 `%LOCALAPPDATA%\jev`；`.env`、`runs`、`state` 的路径。程序本身不在这里，而在 `%LOCALAPPDATA%\Programs\jev`
- [x] 1.2 `helper/desk_lock.py`：本用户的命名互斥量 `Local\jev-desk-<SID>`，非阻塞获取，`WAIT_OBJECT_0` 与 `WAIT_ABANDONED` 都算拿到，同一线程释放。验证：`tests/test_no_real_machine.py` 拒绝 `open_mutex`
- [x] 1.3 `helper/approvals.py`：永远拒绝、只点；其余应用不问直接操作；宿主智能体识别；`Denied` 是 `Abort`。验证：`tests/test_helper_approvals.py`
- [x] 1.4 `helper/guard.py`：`GuardedDesktop` 让每个输入过门禁，取消事件接到 `check_abort`，拒绝打开文件。验证：同上
- [x] 1.5 `helper/service.py`：十个工具、参数校验、进程内锁之后再拿桌面互斥量、`busy`、`state_id` 与 `stale_state`、`not_found`、`missed`、`unsupported`。`ToolFailure` 是普通异常。`run_goal` 固定真动作且 `ask=None`，`driver_steps` 与 `clicker` 相同。验证：`tests/test_helper_service.py`，含两把锁的顺序和异常路径上的释放

## 2. MCP 与命令

- [x] 2.1 `helper/front.py`：`Local` 在第一次工具调用时把 stdout/stderr 指到 `jev.log`、读 `.env`、创建 `Service` 与门禁。模块顶层不导入平台与模型。取消时直接 `service.cancel`。`status` 打印版本、可执行文件、用户目录、exe 旁边有没有 `models\`。验证：`tests/test_helper_front.py`
- [x] 2.2 `helper/cli.py`：子命令只有 `mcp` 和 `status`。`helper`、`stop`、`install` 都不是命令。验证：`tests/test_helper_cli.py`
- [x] 2.3 导入边界：`mcp` 路径只导入 `helper.front`；`helper.front` 在模块层不触及平台与模型；只有 `desk_lock` 导入 `win32event`。验证：`tests/test_import_boundaries.py`
- [x] 2.4 `tests/conftest.py` 守护 `desk_lock.open_mutex`，并在 `tests/test_no_real_machine.py` 里逐项断言

## 3. 打包与安装

- [x] 3.1 `packaging/windows/jev.spec`（`console=True`，收集 FastMCP）、`packaging/windows/jev_entry.py`（exe 旁边有 `models\` 时离线读取）与 `scripts/build-helper.ps1`：安装程序内含模型
- [x] 3.2 `packaging/windows/jev.iss`：按用户安装，升级和卸载靠 `CloseApplications` 关掉仍在运行的 `jev.exe`，没有 `jev stop`
- [x] 3.3 `docs/helper.md`；更新 `README.md`、`docs/layout.md`、`docs/windows.md`、`AGENTS.md`

## 4. 在 Windows 上验证

- [ ] 4.1 在 Windows 上运行 `scripts\build-helper.ps1`，补齐 PyInstaller 漏掉的隐藏导入，记录包体积与冷启动时间。确认原生库没有把字节写进 MCP 的 stdout。这一步要先得到维护者批准
