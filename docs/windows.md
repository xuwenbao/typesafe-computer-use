# Windows (experimental)

`windows.py` provides the same adapter over UI Automation and Win32 `SendInput`, and `uv sync`
installs its packages in place of the macOS ones. It is untested on Windows: CI runs only its pure
rules, on macOS and Linux. Expect it to break, and please report what you see. The screen is read
by the same perception as on macOS, RapidOCR with OmniParser's icon detector and captioner, so no
Windows OCR language pack is needed. Fetch the model files once before the first run:

```powershell
uv run clicker-models fetch
```

No permission prompt is needed. What differs from macOS:

- An app is its process: `activate` matches the executable name exactly (`notepad.exe`), or a
  known browser by its product name (`Google Chrome` is `chrome.exe`). Window titles never count.
- A click reads the cursor back first, and refuses to press, ending the run, when the cursor did
  not reach the target (a UAC prompt or the lock screen has the input).
- The browser URL is read off the address bar, which Chrome and Edge show without the scheme.
- Only the primary monitor is captured. Command-[ (back) is Alt-Left.
- `clicker session "the goal"` runs jev in a child session so the console desktop stays usable.
  See [the isolated session](windows-session.md). It needs Pro, Enterprise, or Education.
- When a policy blocks the installed `clicker.exe`, run `uv run python -m yuanji_computer_use`
  with the same arguments, or `... yuanji_computer_use inspect` for `clicker-inspect`.

Other agents can use jev on Windows as MCP tools through the installed `clicker.exe` (`clicker mcp`).
`clicker-setup` carries the model files, so that install does not run `clicker-models fetch`; see
[helper and MCP server](helper.md).
