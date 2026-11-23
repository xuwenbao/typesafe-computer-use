# jev 的工具

十个工具都作用在真机上。失败是一条以 `kind` 开头的工具错误。

| 工具 | 参数 | 作用 |
|---|---|---|
| `run_goal` | `goal`；可选 `steps`、`handoffs`、`min_confidence` | 把一句目标交给 jev 自己的循环，直到结束 |
| `inspect_screen` | 可选 `goal` | 只读屏：元素编号、角色、屏幕坐标、`state_id`、带标记的截图 |
| `get_app_state` | 可选 `app`（如 `notepad.exe`，或 `Google Chrome`） | 把应用带到前台，再按 `inspect_screen` 读屏 |
| `click` | 可选 `state_id`、`element`，或 `x`、`y`；`button` 为 `left` 或 `right`；`count`（2 为双击） | 点一个元素，或点一个屏幕坐标 |
| `type_text` | `text`；可选 `state_id`、`element` | 输入文字；给了元素就先点它 |
| `set_value` | `state_id`、`element`、`value` | 直接替换无障碍元素的值，不逐键输入 |
| `press_key` | `key`；可选 `command`（按住 Ctrl） | 按一个键 |
| `scroll` | `lines`（负数向下）；可选 `state_id`、`element` | 在元素上或前台窗口上滚动 |
| `activate` | `app` | 把应用窗口带到前台 |
| `frontmost_app` | 无 | 前台应用和它的进程号 |

`press_key` 的 `key`：`return`、`tab`、`escape`、`delete`、`backspace`、`space`、`up`、`down`、`left`、`right`、`pageup`、`pagedown`、`home`、`end`、`start`、`f2`、`f5`、一个字母或一个数字。

## `kind`

| kind | 含义 |
|---|---|
| `forbidden` | 这个应用不能这样操作 |
| `busy` | 已有一个调用占着桌面 |
| `stale_state` | `state_id` 已经过期，这次没有输入 |
| `invalid_arguments` | 参数不合法 |
| `missed` | 点到了别的地方 |
| `aborted` | 调用被取消或急停 |
| `not_found` | 找不到这个应用 |
| `unsupported` | 这里做不到这个动作 |
| `not_configured` | 缺少密钥或配置 |
| `not_supported` | 这台机器跑不了这次感知 |
