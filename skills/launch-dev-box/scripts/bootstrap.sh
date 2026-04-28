#!/usr/bin/env bash
# Runs on the dev box (over ssh). Idempotent.
# Args: $1 = github username, $2 = logical box name
set -euo pipefail

GH_USER="${1:?github username required}"
BOX_NAME="${2:?box name required}"

echo "==> bootstrap start (gh_user=$GH_USER, name=$BOX_NAME)"

# Wait for cloud-init to finish (Ubuntu first-boot work).
sudo cloud-init status --wait >/dev/null 2>&1 || true

# 1. uv
if [[ ! -x ~/.local/bin/uv ]]; then
    echo "==> installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi
UV=~/.local/bin/uv

# 2. Python 3.12 toolchain
$UV python install 3.12 >/dev/null

# 3. PATH patch (idempotent)
if ! grep -q "HOME/.local/bin" ~/.bashrc; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
fi

# 4. Pre-populate github.com host key (fresh Ubuntu has no known_hosts entries)
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! grep -q "^github.com " ~/.ssh/known_hosts 2>/dev/null; then
    echo "==> adding github.com to ~/.ssh/known_hosts"
    ssh-keyscan -t rsa,ecdsa,ed25519 github.com 2>/dev/null >> ~/.ssh/known_hosts
fi

# 5. Verify agent-forwarded GitHub auth (private-repo clones fail silently otherwise).
# `ssh -T git@github.com` always exits 1 (GitHub closes the channel after auth);
# capture stderr and grep instead of relying on the pipe exit code under pipefail.
auth_msg=$(ssh -T -o BatchMode=yes git@github.com </dev/null 2>&1 || true)
if ! echo "$auth_msg" | grep -q "successfully authenticated"; then
    echo ""
    echo "ERROR: github SSH auth failed:"
    echo "  $auth_msg"
    echo "  Likely your local ssh-agent has no key registered with GitHub."
    echo "  On your laptop, run: ssh-add --apple-use-keychain ~/.ssh/id_ed25519"
    echo "  (or whichever key is attached to your GitHub account)"
    echo "  Then re-run launch from your laptop — it's idempotent."
    exit 1
fi
echo "==> github auth OK ($(echo "$auth_msg" | head -1))"

# 6. Clone ray fork + upstream
if [[ ! -d ~/ray ]]; then
    echo "==> cloning ray fork ($GH_USER/ray)"
    cd ~ && git clone "git@github.com:${GH_USER}/ray.git"
    cd ~/ray
    git remote add upstream git@github.com:ray-project/ray.git
    git fetch upstream --quiet
else
    echo "==> ~/ray already exists, skipping clone"
fi

# 5. Venv + ray nightly + setup-dev (only on fresh venv to avoid stomping symlinks)
VENV=~/ray/.venv
if [[ ! -d "$VENV" ]]; then
    echo "==> creating venv"
    # Run from $HOME (not ~/ray) so uv doesn't try to parse ray's
    # bazel-flavored pyproject.toml and emit a "TOML parse error" warning.
    cd ~ && $UV venv -p 3.12 "$VENV" >/dev/null

    WHEEL_URL="https://s3-us-west-2.amazonaws.com/ray-wheels/latest/ray-3.0.0.dev0-cp312-cp312-manylinux2014_x86_64.whl"
    echo "==> installing ray nightly with [default,serve,llm]"
    $UV pip install --python "$VENV/bin/python" --quiet \
        "ray[default,serve,llm] @ $WHEEL_URL"

    # Pin checkout to wheel's commit so custom_types.py / proto enums match.
    WHEEL_COMMIT=$("$VENV/bin/python" -c 'import ray; print(ray.__commit__)')
    echo "==> pinning ~/ray to wheel commit ${WHEEL_COMMIT:0:10}"
    git -C ~/ray fetch upstream --quiet
    git -C ~/ray checkout "$WHEEL_COMMIT" --quiet

    # Editable: symlink subpackages back into the checkout. Skip dashboard so
    # the wheel's prebuilt React app stays intact.
    echo "==> setup-dev.py --skip dashboard"
    "$VENV/bin/python" python/ray/setup-dev.py -y --skip dashboard >/dev/null
else
    echo "==> venv already exists, skipping ray install"
fi

# 6. Claude Code
if [[ ! -x ~/.local/bin/claude ]]; then
    echo "==> installing Claude Code"
    curl -fsSL https://claude.ai/install.sh | bash >/dev/null
fi

# 7. GPU smoke test (DLAMI ships drivers, just verify)
if command -v nvidia-smi >/dev/null 2>&1; then
    echo "==> GPU(s):"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | sed 's/^/    /'
fi

# 8. Final smoke check
echo "==> import ray"
cd ~ && "$VENV/bin/python" -c "
import ray
print('  ray', ray.__version__, ray.__commit__[:10])
ray.init(num_cpus=1, log_to_driver=False, include_dashboard=False)
ray.shutdown()
print('  OK')
" 2>&1 | tail -3 || echo "  WARNING: smoke test failed but bootstrap otherwise complete"

cat <<EOF

================================================================
ready.

 ssh in:        ssh $BOX_NAME   (after Include line in ~/.ssh/config)
 activate venv: source ~/ray/.venv/bin/activate
 claude:        claude    (one-time interactive OAuth)

repo:  ~/ray  (origin = $GH_USER/ray, upstream = ray-project/ray)
HEAD:  $(git -C ~/ray rev-parse --short HEAD)
================================================================
EOF
