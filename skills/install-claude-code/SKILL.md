---
name: install-claude-code
description: Installs the Claude Code CLI with the official install script, ensures ~/.bashrc loads the CLI on new shells, and verifies `claude` works. Use when setting up the `claude` command or `/install-claude-code`.
---

# Install Claude Code CLI

## 1. Install

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

If `curl` complains about `libcurl` (e.g. Anaconda on `PATH`), use:

```bash
/usr/bin/curl -fsSL https://claude.ai/install.sh | bash
```

## 2. Update `~/.bashrc`

The script usually appends a **PATH** (or init) block to **`~/.bashrc`**. Open **`~/.bashrc`** and confirm that block exists. If **`claude`** is still not found after a new terminal, add what the installer printed (often something like putting **`~/.local/bin`** on **`PATH`**) to **`~/.bashrc`**, save, then reload:

```bash
source ~/.bashrc
```

## 3. Verify

```bash
command -v claude
claude --version
```

Optional: run **`claude`** in a project directory and complete login when prompted.
