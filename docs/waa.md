# WindowsAgentArena

[WindowsAgentArena](https://github.com/microsoft/WindowsAgentArena) is a benchmark of real Windows
desktop tasks. Each task runs in a Windows 11 VM inside one container, and jev runs there as the
agent. The container is the only place the VM's HTTP service (`20.20.20.21:5000`) and QEMU's
screenshot socket (`localhost:7200`) are reachable, so the client runs in the container too, on
its own Python 3.12. One container, one VM, the tasks one after another.

The VM runs under QEMU and needs `/dev/kvm`. Docker is required. A WSL2 machine with KVM is a
machine this runs on. The golden image is about 30 GB and wants 8 GB of RAM and 8 CPU cores,
overridable with `WAA_RAM_SIZE` and `WAA_CPU_CORES`.

The container does not see this machine's `127.0.0.1`. When `WAA_PROXY`, or else `https_proxy`,
points at `127.0.0.1` or `localhost` (Clash Verge's usual port), `scripts/waa` forwards it
from the docker bridge to the loopback and points the container at `host.docker.internal`.
When that port is already taken on the bridge, as it is under WSL's mirrored networking, the
relay listens on another port there and still forwards to Clash.
`WAA_PROXY=off` uses no proxy. `NO_PROXY` includes `20.20.20.21`, so the VM's own service is
never sent through the proxy. The Windows VM itself is not given a proxy. `run` also mounts
`waa_overlay/on-logon.ps1` and `waa_overlay/keep_server.py` over `/shared`, so the arena server
starts without a window and comes back if it stops. `close_all` cannot close a window it does
not have.

```
scripts/waa setup                         # the pinned commit into .waa/, then the jev-winarena image
scripts/waa prepare-image [--iso PATH]    # the golden image in .waa/storage, from the evaluation ISO
scripts/waa run DOMAIN/ID...              # those tasks, one after another, in one VM
scripts/waa results [FOLDER]              # each task's score, steps, time and tokens, then the means
```

`setup` clones the commit pinned in `scripts/waa` into `.waa/WindowsAgentArena`, copies
`waa_overlay/` over the client, and builds `jev-winarena` on the pinned
`windowsarena/winarena-base` digest. It then imports `desktop_env.evaluators` and
`yuanji_computer_use.waa.agent` inside the image, and stops if either import fails. That import
runs in a container, so it needs approval. `.waa/` is gitignored. Changing jev or the overlay does
not need another build; changing the lock or the pinned commit does.

`prepare-image` needs the Windows 11 Enterprise Evaluation ISO (90 days, English, United States)
from the [Microsoft Evaluation Center](https://info.microsoft.com/ww-landing-windows-11-enterprise.html).
Put it at the checkout's `src/win-arena-container/vm/image/setup.iso`, or pass `--iso PATH`.
Without an ISO the command says so and starts nothing. The evaluation expires after 90 days; run
`prepare-image` again with a new ISO. The command publishes port 8006 (noVNC) and 3390 (RDP).

A task is `DOMAIN/ID`, a file under the checkout's `evaluation_examples_windows/examples/`.
`run chrome/<id> msedge/<id>` checks that both files exist, that `/dev/kvm` is there, and that
`TYPESAFE_API_KEY` is set in the environment or in `.env`. It refuses `--ocr`, `--envs` and
`--num-workers`. Keys (`TYPESAFE_API_KEY`, `OPENAI_API_KEY`, `CLICKER_*`) are passed into the
container as environment variables and are never printed. `max_steps`, WAA's own default of 15, or `WAA_MAX_STEPS` when a run sets it,
counts the action lists handed to the VM. A decision that hands nothing, a refused type or a stop
for review, does not spend one of them; those run against jev's usual step limit. `run` does not
revert the VM between tasks: WindowsAgentArena closes the apps and leaves the disk as it is.

Results land in `results/pyautogui/screenshot/jev/0/<domain>/<id>/`. `result.txt` is the score,
`traj.jsonl` is one line per action stamped `%Y%m%d@%H%M%S`, and `jev/run.json` is jev's own
record. Its `waa` field holds the pinned commit, the base image digest, the browser, `tree`
(`jev-uia`), and the tree fetches: how many, how many failed and why, how many were truncated,
and the mean and max of the seconds and the node counts. There is no `usage.json`; the tokens
are the ones in `run.json`. `results` averages the tasks that scored 1 or more separately from
the ones that scored below, and lists a task with no score without counting it.

Rerunning a task moves its earlier result to `results/archive/<time>/` first. With
`JEV_WAA_SAVE_TREE=1`, each observation's raw tree is saved in the run folder as
`obs-NNN-uia.json`, counting from `000`. `WAA_COMMIT` and `WAA_IMAGE_DIGEST`, when the run sets
them, are recorded on `run.json`. All three are read through `config.settings()`, like every other
variable jev uses.

A task WindowsAgentArena marks infeasible scores 0. jev ends a task with `DONE` and never with
`FAIL`, which is the only result that evaluator accepts.
