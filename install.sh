#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_SRC="$SCRIPT_DIR/skills"
GLOBAL_SKILLS_DIR="${CURSOR_SKILLS_DIR:-$HOME/.cursor/skills}"
PROJECT_DIR="${PROJECT_DIR:-$HOME/default}"
PROJECT_SKILLS_DIR="$PROJECT_DIR/.cursor/skills"

if [[ ! -d "$SKILLS_SRC" ]]; then
    echo "Error: No skills/ directory found at $SKILLS_SRC"
    exit 1
fi

# Idempotent symlink: link_file <source_abs> <target_path> <label>
link_file() {
    local source_abs="$1" target="$2" label="$3"

    if [[ -L "$target" ]]; then
        local existing
        existing="$(readlink -f "$target")"
        if [[ "$existing" == "$source_abs" ]]; then
            echo "  ok:   $label (already linked)"
            return 0
        fi
        echo "  update: $label (repointing symlink)"
        rm "$target"
    elif [[ -e "$target" ]]; then
        echo "  skip: $label ($target exists and is not a symlink — refusing to overwrite)"
        return 1
    fi

    ln -sf "$source_abs" "$target"
    echo "  link: $label -> $source_abs"
}

# Install skills to a target directory, returns count via global variable
install_skills_to() {
    local dst="$1"
    local count=0

    mkdir -p "$dst"

    for skill_dir in "$SKILLS_SRC"/*/; do
        [[ -d "$skill_dir" ]] || continue
        local skill_name
        skill_name="$(basename "$skill_dir")"

        if [[ ! -f "$skill_dir/SKILL.md" ]]; then
            echo "  skip: $skill_name (no SKILL.md)"
            continue
        fi

        local source_abs
        source_abs="$(cd "$skill_dir" && pwd -P)"

        if link_file "$source_abs" "$dst/$skill_name" "$skill_name"; then
            count=$((count + 1))
        fi
    done

    echo "  ($count skill(s) installed)"
}

echo "Installing skills to $GLOBAL_SKILLS_DIR (global) ..."
install_skills_to "$GLOBAL_SKILLS_DIR"

echo ""
echo "Installing skills to $PROJECT_SKILLS_DIR (project) ..."
install_skills_to "$PROJECT_SKILLS_DIR"

# --- Install worktree.conf to project dir ---
echo ""
echo "Installing worktree.conf to $PROJECT_DIR ..."
conf_src="$SCRIPT_DIR/worktree.conf"
if [[ -f "$conf_src" ]]; then
    link_file "$(cd "$(dirname "$conf_src")" && pwd -P)/worktree.conf" "$PROJECT_DIR/worktree.conf" "worktree.conf"
else
    echo "  skip: worktree.conf (not found in $SCRIPT_DIR)"
fi

echo ""
echo "Done."
