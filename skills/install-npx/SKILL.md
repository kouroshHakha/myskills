---
name: install-npx
description: Ensures Node.js, npm, and npx are installed and upgraded to current releases on Linux (nvm workflow). npx ships with npm and is not installed separately. Use when `npx` or `npm` is missing, `command not found`, or before running `npx skills` / install-anyscale-skills on a fresh or minimal system.
---

# Install npx (via Node.js + npm)

**`npx`** is delivered with **`npm`** (since npm 5.2). There is no standalone `npx` package. To get a **current `npx`**, install a **current Node.js**, then optionally upgrade **npm** globally (which updates the bundled **`npx`**).

## Check first

```bash
command -v npx && npx --version
command -v node && node --version
command -v npm && npm --version
```

If **`npx`** is missing but **`npm`** exists, fix **`PATH`** (e.g. load **nvm** in this shell — see below). If both are missing, install Node.

## Recommended: nvm (Linux, user install, no sudo)

[nvm](https://github.com/nvm-sh/nvm) installs per-user under **`~/.nvm`** and is what npm’s docs often suggest to avoid permission issues.

### 1. Install nvm (pin a release tag from [releases](https://github.com/nvm-sh/nvm/releases))

```bash
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash
```

Reload the shell config or run:

```bash
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
```

### 2. Install latest Node and set default

```bash
nvm install node
nvm alias default node
```

### 3. Upgrade npm (and thus npx) to latest

```bash
npm install -g npm@latest
```

### 4. Verify

```bash
hash -r
which npx npm node
npx --version
npm --version
node --version
```

**New terminals:** `nvm` appends to **`~/.bashrc`**; open a new shell or **`source ~/.bashrc`** so **`npx`** is on **`PATH`**.

## If `curl` warns about `libcurl` (e.g. Anaconda on `PATH`)

Use the system curl:

```bash
/usr/bin/curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash
```

## Alternatives (not “latest” unless you pin versions)

- **Distro packages** (`apt install nodejs npm` on Ubuntu): often older; fine for quick smoke tests, not ideal for “latest npx”.
- **NodeSource / official binaries:** acceptable if your org standardizes on them; still run **`npm install -g npm@latest`** if you need newest **`npx`** behavior.

## After npx works

Use **`install-anyscale-skills`** (or **`npx skills add …`**) from the project root as documented there.
