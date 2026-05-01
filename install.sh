#!/usr/bin/env bash
# hookbus-publisher-hermes — one-shot installer for Hermes + HookBus publisher.
#
# What this does (idempotent):
#   1. Clones NousResearch/hermes-agent into ~/hermes-agent if missing
#   2. Creates a Python venv at ~/hermes-agent/venv
#   3. Installs hermes itself + requirements.txt + python-dotenv (defensive)
#   4. Copies this plugin\s __init__.py + plugin.yaml into ~/.hermes/plugins/hookbus-publisher/ (user plugin dir hermes actually scans)
#   5. Scaffolds ~/hermes-agent/.env from .env.example if missing
#   6. Prompts for MINIMAX_API_KEY if not present
#   7. Installs ~/.local/bin/hermes so the normal `hermes` command launches
#      the installed Hermes runtime with the HookBus plugin available.
#
# Env overrides (all optional):
#   HERMES_DIR          default ~/hermes-agent
#   HOOKBUS_URL         default http://localhost:18800/event
#   HOOKBUS_TOKEN       if set, written to .env
#   HOOKBUS_FAIL_MODE   default closed (fail safe)
#   MINIMAX_API_KEY     if set, written to .env non-interactively

set -euo pipefail

HERMES_DIR="${HERMES_DIR:-$HOME/hermes-agent}"
PLUGIN_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKBUS_URL="${HOOKBUS_URL:-http://localhost:18800/event}"
HOOKBUS_FAIL_MODE="${HOOKBUS_FAIL_MODE:-closed}"
BIN_DIR="$HOME/.local/bin"
HERMES_SHIM="$BIN_DIR/hermes"

say() { printf "\033[1;32m[hermes-install]\033[0m %s\n" "$*" >&2; }
warn() { printf "\033[1;33m[hermes-install]\033[0m %s\n" "$*" >&2; }
die() { printf "\033[1;31m[hermes-install] error:\033[0m %s\n" "$*" >&2; exit 1; }

# 1. Clone if needed
if [[ ! -d "$HERMES_DIR/.git" ]]; then
    say "cloning NousResearch/hermes-agent -> $HERMES_DIR"
    git clone --quiet --depth 1 https://github.com/NousResearch/hermes-agent.git "$HERMES_DIR"
fi

# 2. venv
if [[ ! -d "$HERMES_DIR/venv" ]]; then
    say "creating venv at $HERMES_DIR/venv"
    python3 -m venv "$HERMES_DIR/venv"
fi

# 3. Install deps — defensively, not relying on pyproject.toml alone
source "$HERMES_DIR/venv/bin/activate"
pip install -q --upgrade pip
pip install -q -e "$HERMES_DIR"
pip install -q python-dotenv     # defensive: hermes needs dotenv at import time
if [[ -f "$HERMES_DIR/requirements.txt" ]]; then
    pip install -q -r "$HERMES_DIR/requirements.txt"
fi

# 4. Plugin install
# hermes plugin discovery scans ~/.hermes/plugins/<name>/ and ./.hermes/plugins/<name>/.
# It does NOT scan $HERMES_DIR/plugins/, so installing there leaves the plugin dark.
HERMES_USER_PLUGIN_DIR="$HOME/.hermes/plugins/hookbus-publisher"
mkdir -p "$HERMES_USER_PLUGIN_DIR"
cp "$PLUGIN_SRC/__init__.py" "$HERMES_USER_PLUGIN_DIR/"
cp "$PLUGIN_SRC/plugin.yaml" "$HERMES_USER_PLUGIN_DIR/"
say "plugin installed at $HERMES_USER_PLUGIN_DIR/"

# 5. .env scaffold
if [[ ! -f "$HERMES_DIR/.env" ]]; then
    if [[ -f "$HERMES_DIR/.env.example" ]]; then
        cp "$HERMES_DIR/.env.example" "$HERMES_DIR/.env"
    else
        touch "$HERMES_DIR/.env"
    fi
fi

# Write HookBus settings (idempotent: replace if present)
for var in HOOKBUS_URL HOOKBUS_FAIL_MODE; do
    sed -i "/^${var}=/d" "$HERMES_DIR/.env"
done
{
    echo "HOOKBUS_URL=$HOOKBUS_URL"
    echo "HOOKBUS_FAIL_MODE=$HOOKBUS_FAIL_MODE"
} >> "$HERMES_DIR/.env"

if [[ -n "${HOOKBUS_TOKEN:-}" ]]; then
    sed -i "/^HOOKBUS_TOKEN=/d" "$HERMES_DIR/.env"
    echo "HOOKBUS_TOKEN=$HOOKBUS_TOKEN" >> "$HERMES_DIR/.env"
    say "HOOKBUS_TOKEN persisted in .env"
else
    warn "HOOKBUS_TOKEN not set — bus auth will 401 until you add it to $HERMES_DIR/.env"
    warn "Read with: docker exec hookbus cat /root/.hookbus/.token"
fi

# 6. MiniMax key
if ! grep -qE "^MINIMAX_API_KEY=.+" "$HERMES_DIR/.env"; then
    if [[ -n "${MINIMAX_API_KEY:-}" ]]; then
        echo "MINIMAX_API_KEY=$MINIMAX_API_KEY" >> "$HERMES_DIR/.env"
        say "MINIMAX_API_KEY written to .env from environment"
    elif [[ -t 0 ]]; then
        read -rsp "[hermes-install] MINIMAX_API_KEY (https://api.minimaxi.chat, hidden): " K
        echo
        [[ -n "$K" ]] && { echo "MINIMAX_API_KEY=$K" >> "$HERMES_DIR/.env"; say "MINIMAX_API_KEY saved"; } || warn "no key provided"
    else
        warn "no MINIMAX_API_KEY in env and not on a TTY — edit $HERMES_DIR/.env manually"
    fi
fi

say "install complete."
say "Plugin:      $HERMES_USER_PLUGIN_DIR/"
say "Bus target:  $HOOKBUS_URL"

mkdir -p "$BIN_DIR"
if [ -e "$HERMES_SHIM" ] && ! grep -q "HookBus-managed hermes shim" "$HERMES_SHIM" 2>/dev/null; then
    warn "$HERMES_SHIM already exists and is not HookBus-managed; leaving it unchanged."
    warn "Run directly with: $HERMES_DIR/hermes chat"
else
    cat > "$HERMES_SHIM" <<EOF
#!/usr/bin/env bash
# HookBus-managed hermes shim. Runs the installed Hermes runtime.
set -euo pipefail
HERMES_DIR="$HERMES_DIR"
cd "\$HERMES_DIR"
if [ -f "\$HERMES_DIR/venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "\$HERMES_DIR/venv/bin/activate"
fi
exec "\$HERMES_DIR/hermes" "\$@"
EOF
    chmod 755 "$HERMES_SHIM"
    say "installed normal-command shim at $HERMES_SHIM"
fi

say "Start chat:  hermes chat"
