# myagent

Claude Code skills and a small installer for working on [Ray](https://github.com/ray-project/ray): git worktrees, toolchain setup (`npx`, Claude Code CLI), and installing Anyscale skill bundles via `npx skills`.

## Install

From this directory:

```bash
./install.sh
```

For each skill directory under `skills/` that contains a `SKILL.md`, the script creates symlinks in:

- **`~/.claude/skills/`** (global — override with **`CLAUDE_SKILLS_DIR`**)
- **`$PROJECT_DIR/.claude/skills/`** (project — **`PROJECT_DIR`** defaults to **`$HOME/default`**)

Re-running is safe (idempotent).

```bash
# Examples
CLAUDE_SKILLS_DIR=/path/to/skills ./install.sh
PROJECT_DIR=/path/to/workspace ./install.sh
```

## Configuration

Edit **`worktree.conf`** (used by the **ray-worktree** skill and worktree scripts):

```
RAY_REPO=/path/to/ray          # Local Ray clone (required)
RAY_PYTHON=/path/to/python3    # Python with Ray deps (optional; default python3)
```

If **`RAY_REPO`** is omitted, scripts may auto-detect Ray from an editable install.

## Skills

| Skill | Purpose |
|--------|---------|
| **`ray-worktree`** | Create, use, and remove Ray git worktrees with per-worktree venvs (`worktree.conf`). |
| **`install-npx`** | Install/upgrade Node, npm, and `npx` (e.g. via nvm) when the toolchain is missing. |
| **`install-claude-code`** | Install the `claude` CLI (`claude.ai/install.sh`), fix **`PATH`** in **`~/.bashrc`**, verify. |
| **`install-anyscale-skills`** | Install chosen Anyscale skill repos with `npx skills` (catalog + prompts; Claude + Cursor paths). |

## Adding a skill

Add a directory under **`skills/`** with a **`SKILL.md`**, then run **`./install.sh`**:

```
skills/
└── my-new-skill/
    └── SKILL.md
```
