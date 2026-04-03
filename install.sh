#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_SRC="$SCRIPT_DIR/skills"
GLOBAL_CLAUDE_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
PROJECT_CLAUDE_DIR="$PROJECT_DIR/.claude/skills"
PROJECT_CURSOR_DIR="$PROJECT_DIR/.cursor/skills"
GLOBAL_CURSOR_DIR="${GLOBAL_CURSOR_SKILLS:-$HOME/.cursor/skills}"

if [[ ! -d "$SKILLS_SRC" ]]; then
    echo "Error: No skills/ directory found at $SKILLS_SRC"
    exit 1
fi

# Replace a path that is a symlink with an empty directory (Cursor needs a real dir).
ensure_real_dir() {
    local d="$1"
    if [[ -L "$d" ]]; then
        echo "  note: $d was a symlink — replacing with a directory"
        rm "$d"
    fi
    mkdir -p "$d"
}

# Hard-copy one skill tree into a cursor skills root (resolved files, not symlinks).
copy_skill_into_cursor_root() {
    local src="$1" dst_root="$2" name="$3"
    mkdir -p "$dst_root"
    rm -rf "${dst_root:?}/$name"
    mkdir -p "$dst_root/$name"
    # dereference if src is a symlinked dir
    cp -aL "${src}/." "$dst_root/$name/"
}

# Symlink for Claude: link_path -> target (target may be relative or absolute).
link_claude_to_target() {
    local target="$1"
    local linkpath="$2"
    local label="$3"

    if [[ -L "$linkpath" ]]; then
        local cur
        cur="$(readlink -f "$linkpath")"
        local want
        want="$(readlink -f "$target")"
        if [[ "$cur" == "$want" ]]; then
            echo "  ok:   $label (already linked)"
            return 0
        fi
        echo "  update: $label (repointing symlink)"
        rm "$linkpath"
    elif [[ -e "$linkpath" ]]; then
        echo "  skip: $label ($linkpath exists and is not a symlink — refusing to overwrite)"
        return 1
    fi

    ln -sfn "$target" "$linkpath"
    echo "  link: $label -> $target"
}

echo "Installing myskills from $SKILLS_SRC"
echo ""

# --- Cursor: hard copies (real files; Cursor does not follow symlink trees reliably) ---
CURSOR_ROOTS=("$PROJECT_CURSOR_DIR")
if [[ "${SKIP_GLOBAL_CURSOR:-}" != "1" ]]; then
    CURSOR_ROOTS+=("$GLOBAL_CURSOR_DIR")
fi

for root in "${CURSOR_ROOTS[@]}"; do
    ensure_real_dir "$root"
done

installed_names=()
for skill_dir in "$SKILLS_SRC"/*/; do
    [[ -d "$skill_dir" ]] || continue
    skill_name="$(basename "$skill_dir")"

    if [[ ! -f "$skill_dir/SKILL.md" ]]; then
        echo "  skip: $skill_name (no SKILL.md)"
        continue
    fi

    source_abs="$(cd "$skill_dir" && pwd -P)"

    for root in "${CURSOR_ROOTS[@]}"; do
        echo "  cursor copy: $skill_name -> $root/"
        copy_skill_into_cursor_root "$source_abs" "$root" "$skill_name"
    done
    installed_names+=("$skill_name")
done

echo ""
echo "Claude Code: symlinking .claude/skills -> .cursor/skills (per skill) ..."

mkdir -p "$PROJECT_CLAUDE_DIR" "$GLOBAL_CLAUDE_DIR"
project_cursor_abs="$(cd "$PROJECT_CURSOR_DIR" && pwd -P)"

# Absolute targets so symlink identity checks are reliable (Cursor copy is the single source).
for name in "${installed_names[@]}"; do
    target_abs="$project_cursor_abs/$name"
    link_claude_to_target "$target_abs" "$PROJECT_CLAUDE_DIR/$name" "project:$name"
    link_claude_to_target "$target_abs" "$GLOBAL_CLAUDE_DIR/$name" "global:$name"
done

# --- worktree.conf to project dir ---
echo ""
echo "Installing worktree.conf to $PROJECT_DIR ..."
conf_src="$SCRIPT_DIR/worktree.conf"
if [[ -f "$conf_src" ]]; then
    link_claude_to_target "$(cd "$(dirname "$conf_src")" && pwd -P)/worktree.conf" "$PROJECT_DIR/worktree.conf" "worktree.conf"
else
    echo "  skip: worktree.conf (not found in $SCRIPT_DIR)"
fi

echo ""
echo "Done."
echo "  Cursor (hard copy): ${CURSOR_ROOTS[*]}"
echo "  Claude (symlinks):  $PROJECT_CLAUDE_DIR/* and $GLOBAL_CLAUDE_DIR/* -> $project_cursor_abs/*"