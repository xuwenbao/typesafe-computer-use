# Run folder

Every run writes `runs/<timestamp>/` so a stall can be replayed and fixed offline:

| file | contents |
|---|---|
| `run.log`, `run.json` | everything printed; goal, outcome (`done`, `nothing helps`, `low confidence`, `stalled`, `stuck`, `step limit`, `dry run`, `aborted`, `crashed`), `answer` and `goal_achieved`, seconds, `calls` (requests, share and seconds per model), `usage` (requests and uncached input, cached input, and output tokens per model id), `handoffs` (step, why the classifier stopped, the focus given), `questions` and replies, every action, config, `timing` (mean and max seconds per phase, with `steps_timed`), `perception` (the pipeline version, and the commit and SHA-256 of each model file it reads), and `image_refusals` (each step System One refused the screen picture on and was asked again without it, with its error) |
| `step-NNN-review.json` | what the writer made of a stop on that step: each answer, focus or question, your reply, and whether the run was handed back |
| `answer-raw.png` | the capture the answer was read from, when an action made the last step's capture stale |
| `step-NNN-raw.png` | the capture |
| `step-NNN.png` | items numbered in blue, accessibility ones orange, icons magenta, the chosen one red, the focused field green |
| `step-NNN-sent.png` | the picture sent to System One as `screen_image`: the read region, fitted to its size limits, with every item boxed and numbered |
| `step-NNN-payload.txt` | the exact `state` and criteria sent to TypeSafe, with the picture named by its reference, size and bytes (its pixels are in `step-NNN-sent.png`), then every item with source, role, box, click point, confidence, then the off-screen controls |
| `step-NNN-answers.json` | every probability the classifier returned, the off-screen controls it was offered, the actions already tried on that screen, the idle and repeat counts the stop rules stood at, plus `timing` for that step |

Each step also logs what it cost, so a slow phase is obvious:

```
  timing: capture 0.31s  screenshot 0.28s  app 0.01s  window 0.02s  field 0.01s  url 0.01s  ocr 0.31s (22% of screen)  detect 0.40s  caption 0.12s  ax 0.06s  decide 0.21s  act 0.05s  total 1.47s
```

`capture` covers the four round trips under it; `detect` is left out when the screen had not changed since the last step and its icons were reused, `caption` when every icon carried text, and `act` when the step did not act.

Replay a saved capture as if it were live, without touching the screen:

```
uv run clicker "same goal" --image runs/<ts>/step-003-raw.png --app "Google Chrome" --url "https://example.com/"
```
