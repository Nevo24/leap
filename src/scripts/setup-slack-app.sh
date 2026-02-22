#!/usr/bin/env bash
# Interactive Slack app setup wizard for ClaudeQ.
#
# Usage: setup-slack-app.sh <REPO_PATH>
#
# Creates a Slack app via App Manifest, collects tokens, validates
# them, and saves configuration to .storage/slack/config.json.

set -euo pipefail

REPO_PATH="${1:-.}"
STORAGE_DIR="$REPO_PATH/.storage/slack"
CONFIG_FILE="$STORAGE_DIR/config.json"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║     ClaudeQ Slack App Setup Wizard    ║"
echo "╚═══════════════════════════════════════╝"
echo ""

# Check if Slack is already configured
if [ -f "$CONFIG_FILE" ]; then
    echo -e "${GREEN}Slack integration is already configured.${NC}"
    echo ""
    read -p "Reconfigure from scratch? [y/N] " RECONFIG
    case "$RECONFIG" in
        [yY]*)
            echo ""
            echo "Continuing with fresh setup..."
            echo ""
            ;;
        *)
            echo ""
            echo -e "${GREEN}Keeping existing configuration.${NC}"
            exit 0
            ;;
    esac
fi

echo -e "${YELLOW}IMPORTANT: Use a personal Slack workspace, not a company one.${NC}"
echo -e "${YELLOW}Company workspaces often require admin approval to install apps.${NC}"
echo ""

# ── Step 1: Create the Slack App ────────────────────────────────────

MANIFEST='{
  "display_information": {
    "name": "ClaudeQ",
    "description": "Bidirectional Claude Code integration",
    "background_color": "#2c2c2c"
  },
  "features": {
    "app_home": {
      "home_tab_enabled": false,
      "messages_tab_enabled": true,
      "messages_tab_read_only_enabled": false
    },
    "bot_user": {
      "display_name": "ClaudeQ",
      "always_online": true
    }
  },
  "oauth_config": {
    "scopes": {
      "bot": [
        "chat:write",
        "chat:write.customize",
        "im:history",
        "im:read",
        "im:write",
        "reactions:write"
      ]
    }
  },
  "settings": {
    "event_subscriptions": {
      "bot_events": [
        "message.im"
      ]
    },
    "interactivity": {
      "is_enabled": false
    },
    "org_deploy_enabled": false,
    "socket_mode_enabled": true
  }
}'

echo "Step 1: Create the Slack App"
echo "────────────────────────────"
echo ""
echo "Opening Slack's app creation page in your browser..."
echo ""

open "https://api.slack.com/apps?new_app=1" 2>/dev/null || \
    echo "  Open this URL in your browser:"
echo "  https://api.slack.com/apps?new_app=1"

echo ""
echo -e "${YELLOW}Follow these steps carefully:${NC}"
echo ""
echo "  1. Choose  \"From a manifest\"  (not \"From scratch\")"
echo ""
echo "  2. Select your PERSONAL workspace from the dropdown"
echo "     (If you only see a company workspace, create a free"
echo "      personal workspace first at https://slack.com/create)"
echo ""
echo "  3. Choose the  \"JSON\"  tab, then REPLACE the entire"
echo "     contents with the manifest below:"
echo ""
echo -e "${GREEN}─── Copy everything between the lines ───${NC}"
echo "$MANIFEST"
echo -e "${GREEN}─── End of manifest ─────────────────────${NC}"
echo ""
echo "  4. Click  \"Next\"  to review, then click  \"Create\""
echo ""
echo "  5. You should now see your app's \"Basic Information\" page."
echo "     Click  \"Install to Workspace\"  →  \"Allow\""
echo ""
read -p "Press Enter when you've installed the app..."

# ── Step 2: Bot Token ─────────────────────────────────────────────

echo ""
echo "Step 2: Copy Bot Token"
echo "──────────────────────"
echo ""
echo "  1. In the left sidebar, click  \"OAuth & Permissions\""
echo ""
echo "  2. Under  \"OAuth Tokens\"  you should see a green"
echo "     \"Install to <workspace>\"  button — click it,"
echo "     then click  \"Allow\"  on the next screen."
echo ""
echo "     (If you already installed in Step 1, the token"
echo "      will be shown directly — skip to step 3)"
echo ""
echo "  3. After installing, the page will show a"
echo "     \"Bot User OAuth Token\"  — copy it"
echo "     (it starts with xoxb-)"
echo ""

while true; do
    read -p "Paste Bot Token: " BOT_TOKEN
    if [[ "$BOT_TOKEN" == xoxb-* ]]; then
        break
    fi
    echo -e "${RED}Token must start with 'xoxb-'. Try again.${NC}"
done

# ── Step 3: App-Level Token ───────────────────────────────────────

echo ""
echo "Step 3: Generate App-Level Token"
echo "────────────────────────────────"
echo ""
echo "  1. In the left sidebar, click  \"Basic Information\""
echo ""
echo "  2. Scroll down to  \"App-Level Tokens\""
echo "     Click  \"Generate Token and Scopes\""
echo ""
echo "  3. A dialog will appear with two sections:"
echo ""
echo "     Token Name:  type  socket  (this is just a label,"
echo "                  you can name it anything)"
echo ""
echo "     Scopes:      click the  \"Add Scope\"  button below"
echo "                  the Token Name, then select"
echo "                  connections:write  from the dropdown"
echo ""
echo "  4. Click  \"Generate\""
echo ""
echo "  5. Copy the token shown (starts with xapp-)"
echo ""

while true; do
    read -p "Paste App Token: " APP_TOKEN
    if [[ "$APP_TOKEN" == xapp-* ]]; then
        break
    fi
    echo -e "${RED}Token must start with 'xapp-'. Try again.${NC}"
done

# ── Step 4: Your Slack Member ID ─────────────────────────────────

echo ""
echo "Step 4: Your Slack Member ID"
echo "────────────────────────────"
echo ""
echo -e "  ${YELLOW}IMPORTANT: Use the SAME workspace where you installed"
echo -e "  the ClaudeQ app! If you have multiple workspaces,"
echo -e "  switch to the correct one first.${NC}"
echo ""
echo "  1. Open the Slack app (desktop or web)"
echo "     Make sure you're on the workspace where ClaudeQ is installed"
echo ""
echo "  2. Click your profile picture (bottom-left of the sidebar)"
echo "     then click  \"Profile\""
echo ""
echo "  3. In the profile panel that opens on the right,"
echo "     click the  \"...\"  (more actions) button"
echo "     then click  \"Copy member ID\""
echo ""

while true; do
    read -p "Paste your Member ID (starts with U): " SLACK_USER_ID
    if [[ "$SLACK_USER_ID" == U* ]]; then
        break
    fi
    echo -e "${RED}Member ID must start with 'U'. Try again.${NC}"
done

# ── Step 5: Validate ──────────────────────────────────────────────

echo ""
echo "Step 5: Validating tokens..."
echo ""

# Validate bot token and open DM channel with user
VENV_PATH=$(cat "$REPO_PATH/.storage/venv-path" 2>/dev/null || echo "")
if [ -n "$VENV_PATH" ] && [ -f "$VENV_PATH/bin/python3" ]; then
    PYTHON="$VENV_PATH/bin/python3"
else
    PYTHON="python3"
fi

VALIDATION=$($PYTHON -c "
import json
from slack_sdk import WebClient

client = WebClient(token='$BOT_TOKEN')
try:
    resp = client.auth_test()
    team_id = resp.get('team_id', '')
    # Open DM channel with the actual user (not the bot)
    dm = client.conversations_open(users=['$SLACK_USER_ID'])
    channel_id = dm['channel']['id']
    print(json.dumps({'ok': True, 'channel_id': channel_id, 'team_id': team_id}))
except Exception as e:
    print(json.dumps({'ok': False, 'error': str(e)}))
" 2>&1)

IS_OK=$(echo "$VALIDATION" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('ok', False))")

if [ "$IS_OK" != "True" ]; then
    ERROR=$(echo "$VALIDATION" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('error', 'Unknown error'))")
    echo -e "${RED}Validation failed: $ERROR${NC}"
    echo "Please check your tokens and try again."
    exit 1
fi

USER_ID="$SLACK_USER_ID"
CHANNEL_ID=$(echo "$VALIDATION" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['channel_id'])")
TEAM_ID=$(echo "$VALIDATION" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('team_id', ''))")

echo -e "${GREEN}✓ Bot token valid${NC}"
echo "  User ID: $USER_ID"
echo "  DM Channel: $CHANNEL_ID"
echo "  Team ID: $TEAM_ID"

# ── Step 6: Save config ──────────────────────────────────────────

mkdir -p "$STORAGE_DIR"

cat > "$CONFIG_FILE" << EOF
{
  "bot_token": "$BOT_TOKEN",
  "app_token": "$APP_TOKEN",
  "user_id": "$USER_ID",
  "dm_channel_id": "$CHANNEL_ID",
  "team_id": "$TEAM_ID"
}
EOF

echo ""
echo -e "${GREEN}╔═══════════════════════════════════════╗${NC}"
echo -e "${GREEN}║       Slack app configured!           ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════╝${NC}"
echo ""
echo "To start the bot:  cq --slack"
echo "To enable Slack on a session:  !slack on"
echo ""
