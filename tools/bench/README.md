# The Linux bench

All four suites against Home Assistant's official image, on Docker (a GitHub runner) or Apple's `container` (a Mac,
after `container system start`):

    tools/bench/run.sh [stable|beta|dev|VERSION] [--landlock applied|missing]

- Offline and graph run inside the image, on its own Home Assistant and Python, with pytest added
  (`requirements.txt`, none of it in the image).
- Each journey's Home Assistant runs from the image as the Supervisor starts Core (`ha.sh`): s6's `/init`, its config
  at `/config`, `/tmp` a noexec tmpfs, privileged with no seccomp or AppArmor profile, on the host's network.
  Playwright drives it from the host, where the panel suite runs too: Playwright's browsers don't run on the image's
  musl.
- `--landlock missing` answers Landlock's calls with ENOSYS, as HA OS's kernels do (`enosys.py`); `applied` leaves the
  kernel's own. The guard test checks the guard took that path.
- Not as under the Supervisor: there is none, so no `SUPERVISOR`/`HASSIO` variables. On `container` each Home
  Assistant is a small VM of its own: every capability rather than privileged, and its port published on 127.0.0.1
  rather than the host's network.

The journeys against HA OS itself, its kernel and Supervisor included, on an x64 Linux machine with KVM (a GitHub
runner; never on a Mac):

    tools/bench/haos.sh [stable|beta|dev]

It boots the channel's HA OS release in QEMU, lets its Supervisor install the channel's Home Assistant, runs the
journeys at phone size against it one at a time, and checks the logs (`logcheck.py`): every replay's guard skipped
Landlock, and no error is the improver's.

CI (`.github/workflows`): `tests.yml` runs both on every push and pull request, for all three channels;
`home-assistant.yml` runs a channel's again for each new Home Assistant version (`watch.py`). Stable gates: a
commit's newest Stable check is its pass (`tools/passed.py`), which `tools/deploy.sh` asks for. Beta and dev are smoke
tests. GitHub bills a private repository's runs, so there the tests skip, and the watcher polls only once the
repository variable `WATCH_HOME_ASSISTANT` is `on` (a scheduled run can't tell).
