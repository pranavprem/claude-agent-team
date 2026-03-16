#!/bin/bash
# Claude Code Agent Team Installer
# Installs agent personas, settings, and CLAUDE.md for agent team orchestration
#
# Usage: ./install.sh [--dry-run]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
AGENTS_DIR="$CLAUDE_DIR/agents"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "🔍 Dry run mode — no files will be modified"
    echo ""
fi

echo "🚀 Claude Code Agent Team Installer"
echo "===================================="
echo ""

# Check prerequisites
echo "📋 Checking prerequisites..."

if ! command -v claude &>/dev/null; then
    echo "❌ Claude Code CLI not found. Install it first:"
    echo "   npm install -g @anthropic-ai/claude-code"
    exit 1
fi

CLAUDE_VERSION=$(claude --version 2>/dev/null | head -1)
echo "   ✅ Claude Code: $CLAUDE_VERSION"

if ! command -v tmux &>/dev/null; then
    echo "   ⚠️  tmux not found. Install it for split-pane agent teams:"
    echo "      brew install tmux  (macOS)"
    echo "      apt install tmux   (Linux)"
    echo "   Agent teams will fall back to in-process mode without tmux."
else
    TMUX_VERSION=$(tmux -V 2>/dev/null)
    echo "   ✅ tmux: $TMUX_VERSION"
fi

echo ""

# Create directories
if [[ "$DRY_RUN" == false ]]; then
    mkdir -p "$AGENTS_DIR"
fi

# Install agent personas
echo "🤖 Installing agent personas..."
AGENTS=(architect architecture-reviewer code-reviewer developer tester retrospective)
for agent in "${AGENTS[@]}"; do
    src="$SCRIPT_DIR/$agent.md"
    dst="$AGENTS_DIR/$agent.md"
    if [[ -f "$dst" ]]; then
        echo "   ⚠️  $agent.md already exists — backing up to $agent.md.bak"
        if [[ "$DRY_RUN" == false ]]; then
            cp "$dst" "$dst.bak"
        fi
    fi
    echo "   📄 $agent"
    if [[ "$DRY_RUN" == false ]]; then
        cp "$src" "$dst"
    fi
done

echo ""

# Install CLAUDE.md
echo "📝 Installing CLAUDE.md..."
CLAUDE_MD="$CLAUDE_DIR/CLAUDE.md"
if [[ -f "$CLAUDE_MD" ]]; then
    echo "   ⚠️  CLAUDE.md already exists — backing up to CLAUDE.md.bak"
    if [[ "$DRY_RUN" == false ]]; then
        cp "$CLAUDE_MD" "$CLAUDE_MD.bak"
    fi
    echo ""
    echo "   Your existing CLAUDE.md has been backed up."
    echo "   The new CLAUDE.md includes agent team orchestration instructions."
    echo "   You may want to merge your custom settings from CLAUDE.md.bak."
fi
echo "   📄 CLAUDE.md → $CLAUDE_MD"
if [[ "$DRY_RUN" == false ]]; then
    cp "$SCRIPT_DIR/CLAUDE.md" "$CLAUDE_MD"
fi

echo ""

# Update settings.json
echo "⚙️  Updating settings.json..."
SETTINGS="$CLAUDE_DIR/settings.json"
if [[ -f "$SETTINGS" ]]; then
    echo "   ⚠️  settings.json already exists — backing up to settings.json.bak"
    if [[ "$DRY_RUN" == false ]]; then
        cp "$SETTINGS" "$SETTINGS.bak"
    fi
    
    # Try to merge agent team settings into existing config
    if command -v jq &>/dev/null; then
        echo "   🔧 Merging agent team settings into existing config..."
        if [[ "$DRY_RUN" == false ]]; then
            jq '. + {
                "env": ((.env // {}) + {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}),
                "teammateMode": "tmux"
            }' "$SETTINGS.bak" > "$SETTINGS"
        fi
    else
        echo "   ℹ️  jq not found — replacing settings.json (backup saved)"
        if [[ "$DRY_RUN" == false ]]; then
            cp "$SCRIPT_DIR/settings.json" "$SETTINGS"
        fi
    fi
else
    echo "   📄 settings.json → $SETTINGS"
    if [[ "$DRY_RUN" == false ]]; then
        cp "$SCRIPT_DIR/settings.json" "$SETTINGS"
    fi
fi

echo ""
echo "===================================="
echo "✅ Installation complete!"
echo ""
echo "📁 Files installed:"
echo "   ~/.claude/agents/architect.md"
echo "   ~/.claude/agents/architecture-reviewer.md"
echo "   ~/.claude/agents/code-reviewer.md"
echo "   ~/.claude/agents/developer.md"
echo "   ~/.claude/agents/tester.md"
echo "   ~/.claude/agents/retrospective.md"
echo "   ~/.claude/CLAUDE.md"
echo "   ~/.claude/settings.json"
echo ""
echo "🎯 Quick start:"
echo "   1. Start a tmux session:  tmux new -s dev"
echo "   2. Navigate to your project:  cd ~/your-project"
echo "   3. Run Claude Code:  claude"
echo "   4. Tell it:"
echo '      "Create an agent team to build <your feature>.'
echo '       Follow the development lifecycle in CLAUDE.md."'
echo ""
echo "📖 See README.md for full usage guide."
