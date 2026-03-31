#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(pwd -P)"
CONF="$PROJECT_DIR/worktree.conf"

# ---------- Check prerequisites ----------
if ! command -v uv &>/dev/null; then
    echo "Error: 'uv' is not installed. Install it: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

if ! command -v git &>/dev/null; then
    echo "Error: 'git' is not installed."
    exit 1
fi

# ---------- Load or create config ----------
if [[ -f "$CONF" ]]; then
    source "$CONF"
fi

# RAY_REPO: path to the main ray git repo
# RAY_PYTHON: path to the python interpreter with ray's deps installed
resolve_ray_repo() {
    local py="$1"
    local editable_loc
    editable_loc=$("$py" -m pip show ray 2>/dev/null | grep "^Editable project location:" | sed 's/^Editable project location: //') || return 1
    [[ -n "$editable_loc" ]] || return 1
    echo "$(cd "$editable_loc" && cd .. && pwd -P)"
}

if [[ -z "${RAY_REPO:-}" ]]; then
    # Try auto-detection via pip show ray
    if [[ -n "${RAY_PYTHON:-}" ]]; then
        RAY_REPO="$(resolve_ray_repo "$RAY_PYTHON")" || true
    elif RAY_REPO="$(resolve_ray_repo python3 2>/dev/null)"; then
        true
    elif [[ -n "${CONDA_PREFIX:-}" ]] && RAY_REPO="$(resolve_ray_repo "$CONDA_PREFIX/bin/python" 2>/dev/null)"; then
        true
    fi
fi

if [[ -z "${RAY_REPO:-}" ]]; then
    echo "Error: Cannot determine the Ray repo location."
    echo ""
    echo "Create $CONF with:"
    echo '  RAY_REPO=/path/to/ray'
    echo '  RAY_PYTHON=/path/to/python  # optional, defaults to python3'
    echo ""
    echo "Or set RAY_REPO as an environment variable."
    exit 1
fi

if [[ -z "${RAY_PYTHON:-}" ]]; then
    RAY_PYTHON="$(command -v python3)"
fi

MAIN_TREE="$(cd "$RAY_REPO" && pwd -P)"

# ---------- Resolve artifact directory ----------
if [[ -f "$MAIN_TREE/python/ray/_raylet.so" ]]; then
    ARTIFACT_DIR="$MAIN_TREE/python/ray"
else
    RAY_SITE=$("$RAY_PYTHON" -m pip show ray 2>/dev/null | grep "^Location:" | sed 's/^Location: //')
    if [[ -n "$RAY_SITE" && -f "$RAY_SITE/ray/_raylet.so" ]]; then
        ARTIFACT_DIR="$RAY_SITE/ray"
    else
        echo "Error: Cannot find compiled Ray artifacts (_raylet.so)."
        echo "They must exist either in '$MAIN_TREE/python/ray/' or in the pip-installed ray package."
        exit 1
    fi
fi

# Persist config for future runs
if [[ ! -f "$CONF" ]]; then
    cat > "$CONF" <<EOF
RAY_REPO="$MAIN_TREE"
RAY_PYTHON="$RAY_PYTHON"
EOF
    echo "==> Created $CONF"
fi

# ---------- Usage ----------
usage() {
    echo "Usage: $0 <name> [branch]"
    echo ""
    echo "  name    Short identifier for the worktree (e.g. fix-scheduling)"
    echo "  branch  Git branch name (default: wt/<name>)"
    echo ""
    echo "Creates a git worktree at $PROJECT_DIR/ray-<name> with its own uv venv."
    echo ""
    echo "Resolved paths:"
    echo "  Ray repo:     $MAIN_TREE"
    echo "  Project dir:  $PROJECT_DIR"
    echo "  Python:       $RAY_PYTHON"
    exit 1
}

[[ $# -lt 1 ]] && usage

NAME="$1"
BRANCH="${2:-wt/$NAME}"
WT="$PROJECT_DIR/ray-$NAME"

if [[ -d "$WT" ]]; then
    echo "Error: worktree '$WT' already exists."
    echo "Run: git -C '$MAIN_TREE' worktree list"
    exit 1
fi

echo "==> Creating worktree '$NAME' on branch '$BRANCH'..."
echo "    Ray repo: $MAIN_TREE"
echo "    Target:   $WT"
git -C "$MAIN_TREE" config --local extensions.worktreeConfig true 2>/dev/null || true
git -C "$MAIN_TREE" worktree add "$WT" -b "$BRANCH"

echo "==> Symlinking build artifacts from $ARTIFACT_DIR..."
rm -f "$WT/python/ray/_raylet.so"
ln -sf "$ARTIFACT_DIR/_raylet.so" "$WT/python/ray/_raylet.so"
rm -rf "$WT/python/ray/core"
ln -sf "$ARTIFACT_DIR/core" "$WT/python/ray/core"
rm -rf "$WT/python/ray/thirdparty_files"
ln -sf "$ARTIFACT_DIR/thirdparty_files" "$WT/python/ray/thirdparty_files"
rm -rf "$WT/python/ray/serve/generated"
ln -sf "$ARTIFACT_DIR/serve/generated" "$WT/python/ray/serve/generated"

echo "==> Hiding symlink noise from git status..."
git -C "$WT" ls-files python/ray/core/ python/ray/serve/generated/ | \
    xargs git -C "$WT" update-index --assume-unchanged
cat > "$WT/.worktree-gitignore" <<'GITIGNORE'
python/ray/thirdparty_files
.venv/
.worktree-gitignore
GITIGNORE
git -C "$WT" config --worktree core.excludesFile "$WT/.worktree-gitignore"

echo "==> Creating uv venv (python: $RAY_PYTHON, inheriting system packages)..."
uv venv --python "$RAY_PYTHON" --seed --system-site-packages "$WT/.venv"

echo "==> Registering worktree ray source in venv..."
PY_VER=$("$RAY_PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
SITE_PACKAGES="$WT/.venv/lib/python${PY_VER}/site-packages"
echo "$WT/python" > "$SITE_PACKAGES/ray-worktree.pth"

echo ""
echo "============================================"
echo " Worktree '$NAME' is ready!"
echo "============================================"
echo ""
echo "Activate in your terminal:"
echo "  source $WT/.venv/bin/activate"
echo ""
echo "Worktree path:"
echo "  $WT"
echo ""
echo "Git branch: $BRANCH"
echo "============================================"
