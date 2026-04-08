#!/usr/bin/env bash
# launch.sh — Start the Neo agent system in a tmux session.
#
# Architecture:
#   - Window 0 (relay):  slack-relay.py — single Socket Mode connection to Slack
#   - Window N (per-session): Claude Code with MCP channel server
#
# Each Claude Code session runs perpetually with --dangerously-skip-permissions.
# Messages arrive via the channel server (HTTP from relay), not filesystem.
#
# Usage:
#     ./launch.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.yaml"
ENV_FILE="$SCRIPT_DIR/.env"
VENV_DIR="$SCRIPT_DIR/.venv"
SESSION_NAME="neo"
LAUNCH_DELAY=3  # seconds between Claude Code launches

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

die() { echo "Error: $*" >&2; exit 1; }
info() { echo "[neo] $*"; }

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

command -v tmux >/dev/null 2>&1 || die "tmux is not installed"
[[ -f "$CONFIG_FILE" ]] || die "config.yaml not found at $CONFIG_FILE"
[[ -f "$ENV_FILE" ]] || die ".env not found at $ENV_FILE"
[[ -d "$VENV_DIR" ]] || die "Python venv not found at $VENV_DIR"
command -v claude >/dev/null 2>&1 || die "claude CLI is not installed"

source "$VENV_DIR/bin/activate"
source "$ENV_FILE"

# Read tokens from .env
SLACK_BOT_TOKEN=$(grep -E '^SLACK_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
SLACK_APP_TOKEN=$(grep -E '^SLACK_APP_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
[[ -n "$SLACK_BOT_TOKEN" ]] || die "SLACK_BOT_TOKEN not found in .env"
[[ -n "$SLACK_APP_TOKEN" ]] || die "SLACK_APP_TOKEN not found in .env"

# ---------------------------------------------------------------------------
# Kill existing session
# ---------------------------------------------------------------------------

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    info "Killing existing '$SESSION_NAME' tmux session..."
    tmux kill-session -t "$SESSION_NAME"
fi

# ---------------------------------------------------------------------------
# Parse sessions from config.yaml
# ---------------------------------------------------------------------------

SESSION_DATA=$(
    python -c "
import yaml, json, sys
with open('$CONFIG_FILE') as f:
    config = yaml.safe_load(f)
result = []
for name, sess in config.get('sessions', {}).items():
    result.append({
        'name': name,
        'path': sess['path'],
        'channel': sess['channel'],
        'port': sess['port'],
    })
json.dump(result, sys.stdout)
"
) || die "Failed to parse config.yaml"

NUM_SESSIONS=$(echo "$SESSION_DATA" | python -c "import json,sys; print(len(json.load(sys.stdin)))")

# ---------------------------------------------------------------------------
# Resolve Slack channel IDs
# ---------------------------------------------------------------------------

info "Resolving Slack channel IDs..."
CHANNEL_JSON=$(python "$SCRIPT_DIR/resolve_channels.py") || die "Failed to resolve channel IDs"
info "Channel map: $CHANNEL_JSON"

# ---------------------------------------------------------------------------
# Create tmux session with relay
# ---------------------------------------------------------------------------

info "Creating tmux session '$SESSION_NAME'..."
tmux new-session -d -s "$SESSION_NAME" -n "relay" -x 200 -y 50

# Start the slim HTTP relay
tmux send-keys -t "$SESSION_NAME:relay" \
    "source '$VENV_DIR/bin/activate' && source '$ENV_FILE' && cd '$SCRIPT_DIR' && python slack-relay.py" Enter

info "Started slack-relay.py in 'relay' window"

# Give relay time to connect to Slack and start routing
sleep 3

# ---------------------------------------------------------------------------
# Launch Claude Code for each session
# ---------------------------------------------------------------------------

for i in $(seq 0 $((NUM_SESSIONS - 1))); do
    NAME=$(echo "$SESSION_DATA" | python -c "import json,sys; d=json.load(sys.stdin)[$i]; print(d['name'])")
    PROJECT_PATH=$(echo "$SESSION_DATA" | python -c "import json,sys; d=json.load(sys.stdin)[$i]; print(d['path'])")
    CHANNEL=$(echo "$SESSION_DATA" | python -c "import json,sys; d=json.load(sys.stdin)[$i]; print(d['channel'])")
    PORT=$(echo "$SESSION_DATA" | python -c "import json,sys; d=json.load(sys.stdin)[$i]; print(d['port'])")

    # Look up Slack channel ID
    CHANNEL_ID=$(echo "$CHANNEL_JSON" | python -c "import json,sys; m=json.load(sys.stdin); print(m.get('$CHANNEL', ''))")
    if [[ -z "$CHANNEL_ID" ]]; then
        info "WARNING: No channel ID for '$CHANNEL' — skipping '$NAME'"
        continue
    fi

    # Write .mcp.json into the project directory
    python -c "
import json, sys
config = {
    'mcpServers': {
        'neo-slack': {
            'command': sys.argv[1],
            'args': [sys.argv[2], '--port', sys.argv[3], '--channel-id', sys.argv[4]],
            'env': {
                'SLACK_BOT_TOKEN': sys.argv[5],
                'SLACK_APP_TOKEN': sys.argv[6],
            }
        }
    }
}
with open(sys.argv[7], 'w') as f:
    json.dump(config, f, indent=2)
" "$VENV_DIR/bin/python" "$SCRIPT_DIR/neo-slack-channel.py" "$PORT" "$CHANNEL_ID" "$SLACK_BOT_TOKEN" "$SLACK_APP_TOKEN" "$PROJECT_PATH/.mcp.json"

    # Ensure settings.local.json enables the MCP server
    SETTINGS_DIR="$PROJECT_PATH/.claude"
    mkdir -p "$SETTINGS_DIR"
    cat > "$SETTINGS_DIR/settings.local.json" <<SETTINGSEOF
{
  "enabledMcpjsonServers": ["neo-slack"],
  "enableAllProjectMcpServers": true
}
SETTINGSEOF

    # Create tmux window and launch Claude Code
    tmux new-window -t "$SESSION_NAME" -n "$CHANNEL"
    tmux send-keys -t "$SESSION_NAME:$CHANNEL" \
        "cd '$PROJECT_PATH' && claude --dangerously-skip-permissions --dangerously-load-development-channels server:neo-slack --model opus" Enter

    # Claude may prompt for acknowledgment — auto-accept after a delay
    sleep 5
    tmux send-keys -t "$SESSION_NAME:$CHANNEL" Enter

    info "Launched Claude Code for '$NAME' (channel: $CHANNEL, port: $PORT)"

    sleep "$LAUNCH_DELAY"
done

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

info ""
info "Neo system launched!"
info "  Relay:    tmux attach -t $SESSION_NAME:relay"
info "  Sessions: tmux attach -t $SESSION_NAME:<channel>"
info "  Stop:     ./stop.sh"
info ""
