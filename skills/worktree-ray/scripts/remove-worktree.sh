#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(pwd -P)"
CONF="$PROJECT_DIR/worktree.conf"

# ---------- Load config ----------
if [[ -f "$CONF" ]]; then
    source "$CONF"
fi

if [[ -z "${RAY_REPO:-}" ]]; then
    echo "Error: RAY_REPO is not set. Create $CONF or set it as an env var."
    exit 1
fi

MAIN_TREE="$(cd "$RAY_REPO" && pwd -P)"

# ---------- Usage ----------
usage() {
    echo "Usage: $0 <name> [--delete-branch]"
    echo ""
    echo "  name             Short identifier used when creating the worktree"
    echo "  --delete-branch  Also delete the git branch after removing the worktree"
    echo ""
    echo "Available worktrees:"
    git -C "$MAIN_TREE" worktree list
    exit 1
}

[[ $# -lt 1 ]] && usage

NAME="$1"
DELETE_BRANCH=false
[[ "${2:-}" == "--delete-branch" ]] && DELETE_BRANCH=true

WT="$PROJECT_DIR/ray-$NAME"

if [[ ! -d "$WT" ]]; then
    echo "Error: worktree '$WT' does not exist."
    echo ""
    echo "Available worktrees:"
    git -C "$MAIN_TREE" worktree list
    exit 1
fi

BRANCH=$(git -C "$WT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")

echo "==> Removing git worktree..."
git -C "$MAIN_TREE" worktree remove "$WT" --force

if [[ "$DELETE_BRANCH" == true && -n "$BRANCH" && "$BRANCH" != "HEAD" ]]; then
    echo "==> Deleting branch '$BRANCH'..."
    git -C "$MAIN_TREE" branch -D "$BRANCH"
fi

echo ""
echo "Worktree '$NAME' removed."
