---
name: ray-lint
description: Run lint checks on the Ray codebase using ci/lint/lint.sh. Use when linting Ray code, checking formatting, or fixing lint errors in a Ray repo or worktree.
---

# Ray Lint Checks

All lint checks are run via `ci/lint/lint.sh` from the Ray repo root. The script dispatches to named functions; the primary one is `pre_commit`.

## Quick Start

From the Ray repo root (or worktree root):

```bash
bash ci/lint/lint.sh pre_commit
```

This runs every pre-commit hook defined in `.pre-commit-config.yaml` across all files.

## Available Targets

`ci/lint/lint.sh` accepts a function name as its argument:

| Target | What it does |
|--------|-------------|
| `pre_commit` | Runs all standard pre-commit hooks (ruff, black, mypy, clang-format, shellcheck, cpplint, buildifier, prettier, eslint, trailing-whitespace, end-of-file-fixer, check-ast, check-json, check-toml, docstyle, check-import-order, check-cpp-files-inclusion) |
| `pre_commit_pydoclint` | Runs the pydoclint hook only |
| `clang_format` | C++ formatting via clang-format |
| `code_format` | Runs `ci/lint/format.sh --all-scripts` |
| `semgrep_lint` | Semgrep static analysis |
| `banned_words` | Checks for banned words |
| `doc_readme` | Validates Python package metadata (restructuredtext) |
| `dashboard_format` | Dashboard formatting checks |
| `copyright_format` | Copyright header checks |
| `bazel_team` | Validates Bazel test team ownership |
| `bazel_buildifier` | Bazel BUILD file formatting |
| `pytest_format` | Pytest format checks |

## Typical Workflow

1. Make code changes
2. Run the lint checks:

```bash
bash ci/lint/lint.sh pre_commit
```

3. Several hooks auto-fix files (ruff, black, clang-format, trailing-whitespace, end-of-file-fixer, buildifier, prettier) — review and stage the fixes
4. Re-run to confirm clean

## Worktree Usage

When working in a Ray worktree (`ray-<name>/`), activate the venv first:

```bash
source ray-<name>/.venv/bin/activate
cd ray-<name>
bash ci/lint/lint.sh pre_commit
```
