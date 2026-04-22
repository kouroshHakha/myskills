---
name: worktree-ray
description: git worktrees management. Instructions on how to manage Ray git worktrees for parallel development and code review. Use when creating, activating, or removing Ray worktrees, or when working on Ray source code in a worktree-based workflow.
---

# Ray Worktree Workflow

This skill provides git worktree management so multiple agents can work on the Ray codebase in parallel. Each worktree gets its own branch, directory, and Python virtual environment.

## Locating Scripts

The scripts live alongside this SKILL.md. Resolve the skill directory first:

```bash
SKILL_DIR="$(dirname "$(readlink -f "$0")")"
# or, if running interactively:
SKILL_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
```

When invoked by an agent, the scripts are at:
- `<skill-dir>/scripts/create-worktree.sh`
- `<skill-dir>/scripts/remove-worktree.sh`

Both scripts operate on the **current working directory** — run them from your project root (where `worktree.conf` and `ray-*/` directories live).

## Prerequisites

Assume these are met. If something goes wrong, verify they hold:

1. **Ray repo**: A local clone of Ray with compiled C++ artifacts (`_raylet.so`) available either via editable install, bazel build, or a system-installed ray package.
2. **Python environment**: conda/venv/system with Ray's dependencies installed.
3. **Test deps** pre-installed in the base environment:
   ```
   pip install -r <ray-repo>/python/requirements/base-test-requirements.txt
   pip install -r <ray-repo>/python/requirements/llm/llm-test-requirements.txt
   pip install -r <ray-repo>/python/requirements/serve/serve-requirements.txt
   ```
4. **uv** (`https://docs.astral.sh/uv/`)
5. **git** with worktree support

## Configuration

Scripts read `worktree.conf` in the current working directory. Auto-created on first run if Ray can be auto-detected, or create manually:

```
RAY_REPO=/path/to/ray
RAY_PYTHON=/path/to/python
```

- `RAY_REPO`: path to the main Ray git repo (required)
- `RAY_PYTHON`: Python interpreter with Ray's deps (optional, defaults to `python3`)

## How It Works

Worktree venvs use `--system-site-packages`, inheriting the base environment. A `.pth` file adds the worktree's `python/` to `sys.path`, so `import ray` resolves to the worktree source — no `pip install -e` needed.

CLI entry points (`ray`, `serve`, `tune`) are generated as shim scripts in `.venv/bin/` with a shebang pointing to the worktree's own Python. Without these shims, the `ray`/`serve`/`tune` commands would resolve to the parent environment's copies, which have a hardcoded shebang that bypasses the `.pth` file and silently runs stale code.

Compiled artifacts (`_raylet.so`, `core/`, `thirdparty_files/`, `serve/generated/`) are symlinked from the main tree or installed package.

## Layout

- **Worktrees**: `<cwd>/ray-<name>/`
- **Per-worktree venv**: `<cwd>/ray-<name>/.venv/`
- **Config**: `<cwd>/worktree.conf`

## Usage

All `create-worktree.sh` / `remove-worktree.sh` commands run from the **project root** (the directory containing `worktree.conf` and `ray-*/` directories).

### Discover available worktrees

```
cd ray; git worktree list 
```

### Greenfield: new branch from scratch

Use when starting fresh work (new feature, bug fix, experiment) that is **not** tied to an existing PR or remote branch.

```
<skill-dir>/scripts/create-worktree.sh <name> [branch]
```

- `name` — short identifier (e.g. `fix-scheduling`). Worktree is created at `ray-<name>/`.
- `branch` — optional local branch name (default: `wt/<name>`).

Example:

```
<skill-dir>/scripts/create-worktree.sh fix-scheduling
# creates ray-fix-scheduling/ on branch wt/fix-scheduling
```

### Existing PR: iterate on a remote branch

Use when you need to work on a branch that **already exists on a remote or local** — typically the head branch of an open PR. The `--track` flag starts the worktree at that ref and configures `git push` to update the remote branch directly, even though the local branch has a different name.

```
git -C <ray-repo> fetch <remote> <branch>
<skill-dir>/scripts/create-worktree.sh --track <remote>/<branch> <name>
```

- `<remote>` — the remote that has push access (usually `origin` for your fork).
- `<branch>` — the remote branch to track (e.g. the head branch of a PR).
- `<name>` — short identifier. Local branch defaults to `wt/<name>`.

Example:

```
git -C /path/to/ray fetch origin my-feature
<skill-dir>/scripts/create-worktree.sh --track origin/my-feature pr123
# creates ray-pr123/ on local branch wt/pr123, tracking origin/my-feature
# git push from ray-pr123/ updates origin/my-feature directly
```

### Activate the venv

```
source ray-<name>/.venv/bin/activate
```

### Remove a worktree

```
<skill-dir>/scripts/remove-worktree.sh <name> [--delete-branch]
```

## Rules

- **Always activate the venv** before running Python code, tests, or pip commands.
- **Work within your worktree** — edit files under `ray-<name>/`. Never edit the main Ray tree.
- **Run git from the worktree** directory, not the main tree.
- **Run tests from the worktree** with the venv activated:
  ```
  source ray-<name>/.venv/bin/activate
  cd ray-<name>
  python -m pytest python/ray/tests/test_foo.py
  ```
- **Do not rebuild C++** — worktrees share compiled `.so` files via symlinks. Only make Python-level changes.
