# myagent

Cursor agent skills for working on the [Ray](https://github.com/ray-project/ray) codebase using git worktrees.

## Install

```bash
./install.sh
```

This symlinks each skill in `skills/` into `~/.cursor/skills/` so Cursor discovers them automatically. The install is idempotent — safe to re-run at any time.

To install to a custom location, set `CURSOR_SKILLS_DIR`:

```bash
CURSOR_SKILLS_DIR=/path/to/skills ./install.sh
```

## Configuration

Edit `worktree.conf` with paths for your machine:

```
RAY_REPO=/path/to/ray          # Local Ray repo clone (required)
RAY_PYTHON=/path/to/python3    # Python with Ray deps installed (optional, defaults to python3)
```

If omitted, the scripts will attempt to auto-detect `RAY_REPO` from an editable pip install of Ray.

## Skills

| Skill | Description |
|---|---|
| `ray-worktree` | Create and manage Ray git worktrees for parallel development |

## Adding a new skill

Create a directory under `skills/` with a `SKILL.md`, then re-run `./install.sh`:

```
skills/
└── my-new-skill/
    └── SKILL.md
```
