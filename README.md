# myagent

Claude Code skills and a small installer for working on [Ray](https://github.com/ray-project/ray): git worktrees, toolchain setup (`npx`, Claude Code CLI), and installing Anyscale skill bundles via `npx skills`.

## Install

From this directory:

```bash
./install.sh
```

For each skill directory under `skills/` that contains a `SKILL.md`, the script:

1. **Copies** a full tree of real files into **Cursor** skill dirs (hard copies — Cursor does not reliably load symlinked skill trees).
2. **Symlinks** **Claude Code** skill dirs to that same copy so there is a single source of truth on disk.

Default destinations:

| Tool | Path | Mechanism |
|------|------|-----------|
| **Cursor** | `$PROJECT_DIR/.cursor/skills/` | Hard copy (`cp -aL`) |
| **Cursor** (optional) | `$HOME/.cursor/skills/` | Same hard copy (disable with `SKIP_GLOBAL_CURSOR=1`) |
| **Claude** | `$PROJECT_DIR/.claude/skills/` | Symlink → `$PROJECT_DIR/.cursor/skills/<name>` |
| **Claude** | `$HOME/.claude/skills/` | Symlink → same absolute path as project `.cursor/skills/<name>` |

`PROJECT_DIR` defaults to **`$HOME/default`**. Override with **`PROJECT_DIR=/path/to/workspace`**.

Re-running is safe (idempotent). If **`~/.cursor/skills`** was a symlink, it is replaced by a directory of copied skills.

### Environment variables

| Variable | Meaning |
|----------|---------|
| **`PROJECT_DIR`** | Project root (default `$HOME/default`). |
| **`CLAUDE_SKILLS_DIR`** | Global Claude skills directory (default `$HOME/.claude/skills`). |
| **`GLOBAL_CURSOR_SKILLS`** | Global Cursor skills directory (default `$HOME/.cursor/skills`). |
| **`SKIP_GLOBAL_CURSOR=1`** | Only copy to `$PROJECT_DIR/.cursor/skills/`, not `$HOME/.cursor/skills`. |

```bash
# Examples
PROJECT_DIR=/path/to/workspace ./install.sh
SKIP_GLOBAL_CURSOR=1 ./install.sh
CLAUDE_SKILLS_DIR=/path/to/skills ./install.sh
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
| **`ray-lint`** | Run lint checks on the Ray codebase using `pre-commit` hooks (`ci/lint/lint.sh`). |

## Adding a skill

Add a directory under **`skills/`** with a **`SKILL.md`**, then run **`./install.sh`**:

```
skills/
└── my-new-skill/
    └── SKILL.md
```
