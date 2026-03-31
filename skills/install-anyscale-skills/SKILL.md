---
name: install-anyscale-skills
description: Installs selected Anyscale Agent Skills bundles from GitHub via `npx skills` into the project workspace, scoped to Cursor and Claude Code, with `.agents/skills/` as canonical. Prompts the user to choose bundles before any install. Requires npx; use install-npx first if missing. Covers symlink repair for `./skills/` and safe CLI usage. Use when invoking `/install-anyscale-skills`, installing Anyscale skill repos, or fixing broken `skills/` links after `skills add` or `skills update`.
---

# Install Anyscale skills (`/install-anyscale-skills`)

## Required workflow (do this first)

1. **Do not** run `npx skills add` until the user has chosen bundles.
2. **Prompt for selection**: If **AskQuestion** is available, offer a **multi-select** (or numbered checklist) using the **ID** column from the catalog below. Otherwise ask conversationally: *“Which bundles do you want? (IDs: workload, debug, infra, content-studio, optimization, ray-pr-review, template — or ‘all’).”*
3. Confirm the **project root** (`cd` target: workspace root where `.agents/skills/` should live).
4. For **each selected row**, run **one** install command per § [Install one bundle](#install-one-bundle).
5. After installs, verify **[Cursor `./skills/` symlinks](#cursor-skills-symlinks-common-bug)** if `./skills/` exists.

---

## Catalog (source of truth)

Update this table when new Anyscale skill repos are added or URLs change.

| ID | Name | Repository | Slash commands | Notes |
|----|------|------------|----------------|--------|
| `workload` | Anyscale Workload Agent Skills | `https://github.com/anyscale/anyscale-workload-agent` | `/ray-data`, `/ray-data-batch-embeddings`, `/ray-train`, `/ray-serve`, `/llm-serving` | POC/demo scaffolding (batch embeddings, serve LLMs, distributed training). Shared with customers for testing. |
| `debug` | Anyscale Debug Agent Skills | `https://github.com/anyscale/anyscale-debug-agent` | `/inspect`, `/run`, `/fix` | Failing Ray/Anyscale workloads, Pylon; workspaces/services, logs/metrics. Shared with customers for testing. |
| `infra` | Anyscale Infra Agent Skills | `https://github.com/anyscale/anyscale-infra-deployment-agent` | `/anyscale-kubernetes`, `/anyscale-gcp-vm`, `/anyscale-aws-vm` | E2E Anyscale on K8s (EKS/GKE/AKS) or VM; Terraform/Helm. Shared with customers for testing. |
| `content-studio` | Anyscale Content Studio Agent Skills | `https://github.com/anyscale/anyscale-content-studio-agent` | `/generate-reference-architecture`, `/generate-diagram`, `/generate-blog`, `/ray-anyscale-assistant` | Marketing/tech content, diagrams, Q&A. Internal. |
| `optimization` | Anyscale Optimization Agent Skills | `https://github.com/anyscale/anyscale-optimization-agent` | `/optimize` | Cost, throughput, bottlenecks, production readiness. Shared with FE for testing. |
| `ray-pr-review` | Anyscale ray-pr-review Agent Skills | `https://github.com/anyscale/ray-pr-review-agent` | *(see repo)* | Co-review GitHub PRs with summaries and suggested review order. Internal. |
| `template` | Anyscale Template Agent Skills | `https://github.com/anyscale/anyscale-template-agent` | `/anyscale-template` | Console templates (`.ipynb`) from source materials. Shared with CSG for testing. |

**“All”** means every row in the table (still confirm once before running one install per row).

---

## Prerequisites

- **Node.js + npm** with **`npx`**. If missing or outdated, follow **[install-npx](../install-npx/SKILL.md)** (`/install-npx`) first. `npx` ships with npm; there is no separate `npx` package.
- **Working directory**: project root (workspace root) where **`skills-lock.json`** and **`.agents/skills/`** belong.

---

## Install one bundle

From the **project root**, **one repository URL at a time** (all skills inside that repo, **only** Claude Code + Cursor):

```bash
cd /path/to/project/root
npx --yes skills add <REPO_URL> -y --agent claude-code cursor --skill '*'
```

Replace `<REPO_URL>` with the **Repository** cell for each selected catalog row. Examples: `https://github.com/org/repo.git` or GitHub shorthand `org/repo-name`.

| Flag | Meaning |
|------|--------|
| `-y` / `--yes` | Skip confirmation prompts |
| `--agent claude-code cursor` | Only those agents (change list if needed) |
| `--skill '*'` | All skills in that repo |

**Symlinks (default):** The CLI links into **`.agents/skills/<skill-name>/`**. Do **not** use **`--copy`** unless copies are explicitly required.

**Global install** (user-level): append **`-g`** / **`--global`**.

### Avoid

- **`skills add … --all`** without **`--agent`** — targets ~45 agent profiles and creates many dot-directories (e.g. **`.adal`**, **`.windsurf`**) under the project root.
- **`npx skills add <url>`** alone — interactive picker; use **`-y`**, **`--skill`**, and **`--agent`** as above.

### Optional: list skills in a repo without installing

```bash
npx --yes skills add <REPO_URL> --list
```

---

## Layout: single source of truth

| Path | Role |
|------|------|
| **`.agents/skills/<name>/`** | Real skill content (`SKILL.md`, …) |
| **`.claude/skills/<name>`** | Symlink into `.agents/skills/` |
| **`./skills/<name>`** | “Universal” Cursor links (see below) |

**`skills-lock.json`** at the project root records installs; version-control if the repo and paths allow.

---

## Cursor `./skills/` symlinks (common bug)

The CLI may create **`./skills/`** with targets like **`../../.agents/skills/...`**. From **`./skills/`**, **`../../`** escapes the project and **breaks**. **`.claude/skills/`** depth is usually fine.

After install or **`skills update`**, spot-check (adjust first skill name if needed):

```bash
test -e skills/bash-conventions || echo "fix symlinks"
```

**Repair** from project root:

```bash
mkdir -p skills
cd skills
for s in ../.agents/skills/*; do
  name=$(basename "$s")
  ln -sfn "../.agents/skills/$name" "$name"
done
```

Correct pattern: **`./skills/<name>` → `../.agents/skills/<name>`**.

---

## Remove skills (reset or switch source)

```bash
npx --yes skills remove --skill '*' --agent '*' -y
```

Then **`skills add`** again with the desired **`--agent`** list. Drop **`skills-lock.json`** only for a full reset when you accept regenerating it on next add.

---

## Discoverability

```bash
npx --yes skills --help
npx --yes skills list
```

---

## Summary checklist

- [ ] User chose which **IDs** (or **all**) from the catalog  
- [ ] `cd` to workspace root  
- [ ] One **`npx skills add …`** per selected repository  
- [ ] Confirm **`./skills/`** symlinks if that directory exists  
