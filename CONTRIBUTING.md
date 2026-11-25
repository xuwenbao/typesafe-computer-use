# Contributing

Bug reports with a run folder attached are the most useful thing you can send.
`runs/<timestamp>/` holds the capture, the exact payload, and every probability,
so a stall can be replayed offline with `--image`.

## Ground rules

- Keep the action set mutually exclusive. Two options that mean the same thing
  split the vote and read as low confidence.
- The classifier picks; code decides facts. Anything the model would have to
  compute (dates, URL validity, whether a field is focused) is computed in code
  and handed over as state.
- Free text only ever comes from `writer.py`, with a structured reply and a
  code-side guard.
- The writer never picks an action. When the classifier stops it may name a focus
  or ask the user, and the classifier takes every step from there. Watch the
  `calls:` line: a change that moves work to the writer should say why.
- Once the classifier has stopped short of the goal for good, `driver.py` picks actions
  itself, one per reply, from a whitelist code checks before anything reaches the machine.
  Only safety checks refuse; a check on state that may be stale takes the action and notes it.
  `CLICKER_DRIVER` defaults to on; `false` leaves the stop where it is. A new driver action
  goes in that whitelist, in both adapters' key tables, and in `tests/test_driver.py`; it
  never types a credential. Shortcuts other than Q, W, H, and M are allowed. The classifier's
  own double-click is `open_item`, and only while the frontmost app is not the browser.
- Platform calls live in the adapters, `macos.py` and `windows.py`, only. Everything else
  reaches them through `platform_adapter.desktop`, and a new adapter call goes into the
  `Desktop` protocol and both adapters together. The accessibility-tree walk is shared, in
  `ax_walk.py`.
- Configuration lives in `config.Settings` and is read only through `config.settings()`, which
  keeps one copy for the process. `load_dotenv` and `config.reset_settings()` drop it. A new
  variable is a field there, not another `os.environ.get`. `CLICKER_*` is the prefix; a variable
  that already has a name of its own (`TYPESAFE_API_KEY`, `ANTHROPIC_BASE_URL`, `JEV_WAA_SAVE_TREE`,
  `WAA_COMMIT`, `WAA_IMAGE_DIGEST`) keeps it. `.env` is copied into the environment without
  overriding what is already set, because the Anthropic and TypeSafe SDKs read their own variables
  from there. Tests refresh the copy themselves: `tests/conftest.py` drops it around each test and
  after every `monkeypatch.setenv` or `delenv`, so change the environment through `monkeypatch`.
- Never add a path that types a password.

## Where the loop lives

`runner.py` holds `RunConfig`, `run`, and the writing of `run.json`. The rest of a
run is the agents. `agents/status.py` is the status table (`HOST`, `POLICY`,
`WRITER`, `DRIVER`, `FINAL_ANSWER`, `FINISH`). `agents/base.py` creates an agent
by name. `agents/host.py`, `agents/policy.py`, `agents/writer.py`, and
`agents/driver.py` are the roles. `agents/session.py` is the session.
`agents/strategies.py` is the four stages of one classifier step
(`PerceiveStrategy`, `DecideStrategy`, `ActStrategy`, `MemoryStrategy`).
`agents/middleware.py` is `Timing`, `StepFiles`, `SessionReport`, `StepLog`, and
`StepPipeline`. The takeover model SDK loads only inside `agents/driver.py`,
when a takeover starts.

## A task the loop cannot do

Write it as a scenario in `tests/test_scenarios.py`: a page graph on the simulated
computer in `tests/world.py`, a policy standing in for the classifier, and assertions on
the outcome, the final page, and the actions the world received. Leave it failing under
`xfail` until the loop can do it, then fix the loop rather than the scenario. The
scenarios run the real `runner.run`, so a failure there is an architecture finding, not
a harness one. Every stop rule in the loop was found or fixed that way.

## Before a pull request

```
uv run ruff check . && uv run ruff format .
uv run pytest -q
```

CI runs the same on macOS, and the tests again on Linux: they are pure logic, and
`tests/conftest.py` stands in for the platform modules where they cannot be installed.

Add a replay-based note to the PR when a change alters what the model sees:
which run folder, which step, what the decision was before and after.
