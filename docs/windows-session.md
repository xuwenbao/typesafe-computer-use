# Isolated session

`clicker session` runs a task in a Windows child session. The console desktop keeps its own
input and focus. jev's screenshots and keystrokes stay in the child session. The parent only
shows a Remote Desktop window onto that session and relays a named pipe. That window is the
picture-in-picture: a normal window whose client area is 1280 by 800, not fullscreen and not kept
in front of other windows, with the remote desktop control hosted as a child of that window. The
child session's own desktop is also 1280 by 800, the same size as that client area. The title bar
and borders sit outside it. A larger desktop makes the control present its own view outside that
window. The control's `EnableHardwareMode` is turned off so it uses the GDI presenter; its DirectX
presenter would paint a surface over the whole monitor. While jev is running, the window shows a
mask that reads 智能体执行中, with the action jev is taking underneath. Before the classifier takes
a step, a host step opens or focuses the application the goal needs, by starting its executable or
bringing its window forward, without clicking the desktop or using the Start menu. A Notepad document is a
text area, so typing into it is not refused. The child reports its outcome and then exits, so a
half-closed proxy connection cannot keep the window open. Closing the window cancels the run. The
Start menu's search results are a WebView2 page, and that page does not render in this session, so
a takeover opens an app from the Run dialog instead. The same action taken four times in a row
stops the takeover even when each press lands on a screen that no longer matches the one before it.

The child-session logon uses `CLICKER_SESSION_USERNAME` and `CLICKER_SESSION_PASSWORD`. When
both are set, the controller stores them in Credential Manager as `TERMSRV/localhost`, gives
that password to the Remote Desktop control, and turns its credential prompt off. A sign-in
prompt that belongs to an app inside the session is still yours to answer; the controller never
types it.

```powershell
uv run clicker session "save the report and close Word"
```

This is "running the project on this machine." Each command needs its own approval.

## What has to be true

The command exits, and does not run jev on the console desktop, when any of these fail:

- The edition is Pro, Enterprise, or Education. Home has no Remote Desktop host.
- Remote Desktop can connect to `localhost`.
- The logon is not a smart card. A smart card will not sign in to the child session by itself.
- No child session is already running. Windows allows one.

jev started for the session (`JEV_SESSION=1`) exits before any screenshot or input if it finds
itself still in the console session. On startup it binds to that session's interactive desktop
(`WinSta0\Default`). Without that binding, reading the cursor and sending input are refused.

## What the two sides say

The pipe is created for the current user and rejects remote clients. Each message is one line
of JSON. The `type` is one of `task`, `log`, `progress`, `question`, `answer`, `cancel`, or
`done`. A question from the run is printed on the controller; the reply you type is the
answer. Cancelling aborts the run the same way Ctrl-C does.

## Limits that the code does not work around

- The clipboard is shared with the console session.
- The same Office app cannot be open in both sessions. Close it on the console desktop first.
- Apps that start at logon start again inside the child session.
- Do not reboot the machine while the picture-in-picture window is open.
- A cloud PC joined to Entra ID is not a supported host.
