#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_SRC="$SCRIPT_DIR/skills"
SKILLS_DST="${CURSOR_SKILLS_DIR:-$HOME/.cursor/skills}"

if [[ ! -d "$SKILLS_SRC" ]]; then
    echo "Error: No skills/ directory found at $SKILLS_SRC"
    exit 1
fi

mkdir -p "$SKILLS_DST"

installed=0
for skill_dir in "$SKILLS_SRC"/*/; do
    [[ -d "$skill_dir" ]] || continue
    skill_name="$(basename "$skill_dir")"

    if [[ ! -f "$skill_dir/SKILL.md" ]]; then
        echo "  skip: $skill_name (no SKILL.md)"
        continue
    fi

    target="$SKILLS_DST/$skill_name"
    source_abs="$(cd "$skill_dir" && pwd -P)"

    if [[ -L "$target" ]]; then
        existing="$(readlink -f "$target")"
        if [[ "$existing" == "$source_abs" ]]; then
            echo "  ok:   $skill_name (already linked)"
            installed=$((installed + 1))
            continue
        fi
        echo "  update: $skill_name (repointing symlink)"
        rm "$target"
    elif [[ -e "$target" ]]; then
        echo "  skip: $skill_name ($target exists and is not a symlink — refusing to overwrite)"
        continue
    fi

    ln -sf "$source_abs" "$target"
    echo "  link: $skill_name -> $source_abs"
    installed=$((installed + 1))
done

echo ""
echo "Installed $installed skill(s) to $SKILLS_DST"
