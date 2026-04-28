---
name: launch-dev-box
description: Launch, list, ssh, and shut down personal EC2 dev boxes for Ray work. Each box comes pre-cloned with the user's Ray fork (origin) + ray-project upstream and a Python 3.12 venv with Ray nightly + serve extras editable. GPU instances (g/p families) auto-pick the AWS Deep Learning AMI; CPU instances get plain Ubuntu 22.04. Use when the user wants to spin up a Ray dev box, list what's running, ssh in by name, or shut one down. AWS-tag-driven (no terraform, no local state).
---

# launch-dev-box

Lean Ray dev box manager. Discovery is by AWS tag (`Type=ray-devbox` + `Owner=<user>`); no local state files for instance bookkeeping.

## Commands

All commands are subcommands of `scripts/launch_devbox.py` in this skill dir.

```sh
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
$SKILL_DIR/scripts/launch_devbox.py launch --name NAME --type INSTANCE_TYPE
$SKILL_DIR/scripts/launch_devbox.py list
$SKILL_DIR/scripts/launch_devbox.py ssh NAME [-- <extra ssh args>]
$SKILL_DIR/scripts/launch_devbox.py shutdown NAME [-y]
```

| Command | What it does |
|---|---|
| `launch` | Picks AMI (Deep Learning for g/p GPU instance types, Ubuntu 22.04 otherwise), creates the EC2 instance + 200 GB root EBS, runs the bootstrap over SSH (clones Ray fork + upstream, sets up Python 3.12 venv with Ray nightly editable). |
| `list` | Queries AWS by tag and prints all the user's ray dev boxes (running, pending, stopped). Regenerates `~/.config/launch-dev-box/ssh_config`. |
| `ssh` | Looks up IP from AWS at call time, refreshes the ssh_config aggregator, execs ssh. |
| `shutdown` | Terminates the instance and deletes the security group it created. |

## One-time setup

1. **Config**: first run of any command auto-creates `~/.config/launch-dev-box/config.json`. Fields: `aws_profile`, `aws_region`, `github_user`, `pubkey_path`, `owner`. Defaults are derived (AWS_PROFILE env, `gh api user`, `~/.ssh/id_*.pub`, `$USER`). Edit if needed.
2. **SSH aggregator**: add this line once to `~/.ssh/config` so VS Code / Cursor / `ssh <name>` work from anywhere:
   ```
   Include ~/.config/launch-dev-box/ssh_config
   ```
3. **AWS auth**: ensure `aws sts get-caller-identity --profile <your-profile>` works. If it fails with `ForbiddenException`, clear `~/.aws/sso/cache/` and `aws sso login --profile <your-profile>`.

## What the bootstrap installs on the box

- `uv` (`~/.local/bin/uv`) and Python 3.12
- `~/ray` clone with `origin` = user's fork, `upstream` = `ray-project/ray`
- `~/ray/.venv` with Ray nightly + `[default,serve,llm]` extras (pulls torch, transformers, etc.), editable (`setup-dev.py --skip dashboard`)
- Checkout pinned to `ray.__commit__` (matches the wheel's compiled bindings)
- Claude Code (`~/.local/bin/claude`) — user runs `claude` once to OAuth-log-in
- GPU instances: NVIDIA drivers + CUDA + PyTorch come from the Deep Learning AMI (no extra install)

## Examples

Launch a CPU dev box:
```sh
$SKILL_DIR/scripts/launch_devbox.py launch --name ray-cpu --type m7i.4xlarge
```

Launch a 4×L4 GPU dev box:
```sh
$SKILL_DIR/scripts/launch_devbox.py launch --name ray-4xl4 --type g6.12xlarge
```

List + ssh:
```sh
$SKILL_DIR/scripts/launch_devbox.py list
ssh ray-cpu                              # works from anywhere if Include line is in ~/.ssh/config
```

Tear down:
```sh
$SKILL_DIR/scripts/launch_devbox.py shutdown ray-cpu -y
```

## Notes / gotchas

- **First SSH after launch**: bootstrap runs as part of `launch`. If something fails mid-bootstrap, just re-run `launch` — it's idempotent (re-uses the existing box if found by name).
- **GPU drivers**: skipping our own driver install entirely; we use AWS Deep Learning AMI for GPU instances which ships drivers + CUDA + PyTorch preinstalled. No reboot dance.
- **Costs**: g6.12xlarge ≈ $5/hr, t3a/m7i.xlarge ≈ $0.20/hr. Always `shutdown` when done.
- **GitHub fork**: assumes the user's Ray fork at `github.com/<github_user>/ray`. If it doesn't exist, the bootstrap fails on the clone step with a clear error.
- **Region**: defaults to `us-west-2`. Override per-launch with `--region`.
