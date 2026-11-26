# Layout

```
yuanji_computer_use/
  platforms/      the one way to the machine
    adapter.py    `desktop`: windows.py on Windows, macos.py everywhere else;
                  `Desktop` names what both provide
    macos.py      the macOS adapter: Quartz, AX, AppleScript
    windows.py    the Windows adapter (experimental): UI Automation, SendInput
    ax_walk.py    the bounded accessibility-tree walk, shared by both adapters
  perception/
    pipeline.py   capture, the read region and the changed-tile cache, what the
                  models read and what is kept: fusion, block merging, goal-echo
                  filter, icon captions, the accessibility item source, and the
                  merge of the two
    eval.py       `clicker-perception-eval`: recall and character error on labeled captures
  parse/          the perception's models, each loaded on first use
    ocr.py        RapidOCR lines, boxes hugged to their ink
    detect.py     OmniParser's YOLOv9 icon detector, as TorchScript
    caption.py    OmniParser's Florence-2 icon captioner, cached by crop
    fuse.py       icon boxes and OCR lines into one element per thing
    marks.py      the numbered picture System One sees
    weights.py    the pinned model files and `clicker-models`
    runtime.py    the one inference lock, the device, and why a machine cannot run them
  dates.py        date parsing and "in N days" hints
  llm/
    classifier.py state, criteria, the three-Choice request, the Noul check
    writer.py     the writer model, structured replies, URL validation, the answer
                  with its focus or question
    openai_writer.py the writer's requests on an OpenAI-compatible endpoint
    calls.py      requests counted per model, at the two clients
  actions.py      one handler per action, each returning a history line
  runner.py       `RunConfig`, `run`, and the writing of `run.json`
  agents/         the roles: host, policy, writer, and driver (see CONTRIBUTING.md)
  report.py       logging, annotated screenshots, payload dump
  timing.py       phase stopwatches, the timing line, run summary
  cli.py          `clicker` and `clicker-inspect`
  helper/         jev as MCP tools for other agents, Windows only (see [helper](helper.md))
    cli.py        `clicker`: mcp, status, and setup; each subcommand imports only what it runs
    setup.py      `clicker setup`: print or write one host's MCP config, and copy the jev skill
    front.py      `clicker mcp`: the MCP server; the first tool call loads the desktop, in this process
    service.py    the tools: run_goal, looks with a state_id, and the primitives
    desk_lock.py  this user's desktop mutex, so two `clicker mcp` processes take turns  (platform)
    approvals.py  which apps may be driven: never, or click only; everything else is driven
    guard.py      the desktop every tool call runs on: the gate and the client's cancel on each input
    home.py       %LOCALAPPDATA%\jev: .env, runs, the log
  waa/            jev as a WindowsAgentArena agent (see [WindowsAgentArena](waa.md))
    agent.py      `JevAgent`: WAA's reset() and predict(), jev's loop on a worker thread
    desktop.py    the adapter over WAA's observation: screenshots in, pyautogui code out
    tree.py       the foreground UIA tree, fetched from the VM and read with ax_walk
    results.py    each task's score, steps, time, and tokens, for `scripts/waa results`
  browser/        the browser backend (see [browser backend](browser-backend.md)), opt-in and independent of platforms/macos.py
    cdp.py        the only module that touches the browser        (platform adapter)
    perceive.py   DOM collection: ordered elements, click points, occlusion
    decide.py     browser action set, two-Choice request, answer serialization
    act.py        real Input events, observe-until-changed
    runner.py     step loop, provenance, run folder
    report.py     run folder writing and offline replay
    bench.py      `clicker-bench`: DOM vs RapidOCR, the step loop, replay
tests/            pure logic: dates, merging, reading order, echo filter, config,
                  decisions, the tree walk against a fake tree; for the browser
                  backend, parsing, action filtering, change detection, replay
  world.py        a simulated computer: pages, controls, fields, and what each action
                  does to them, driven by the real step loop with a policy as classifier
  test_scenarios.py
                  tasks of increasing difficulty on that computer, L1 upward; a failure
                  here says the architecture cannot do that task
waa_overlay/      what `scripts/waa setup` copies into WindowsAgentArena: the agent module
                  and the runner that builds it
packaging/windows/ the PyInstaller spec for clicker.exe, its entry script, and the Inno Setup script
scripts/          `sandbox`, `waa`, and `build-helper.ps1` (builds clicker-setup.exe, models included)
```

A Linux port adds a third adapter over xdotool and AT-SPI, and one line in
`platforms/adapter.py`; the perception already runs there. The tree walk takes its children, attributes, and actions as callables,
so only those three bindings change per platform. Nothing else knows which OS it is running on.

The browser backend replaces `platforms/macos.py` with `browser/cdp.py` instead. Both are opt-in and
independent: a browser task never needs Screen Recording permission, and a canvas-only task
still wants the desktop perception.
