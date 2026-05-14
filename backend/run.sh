#!/bin/bash
# Run script for VoiceMate backend
# Sources DeepSeek API key from Hermes config automatically

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_VENV="/usr/local/lib/hermes-agent/venv"
HERMES_ENV="$HOME/.hermes/.env"

# Load Hermes .env (only DEEPSEEK_API_KEY)
if [ -f "$HERMES_ENV" ]; then
    DEEPSEEK_API_KEY=$(grep -E '^DEEPSEEK_API_KEY=' "$HERMES_ENV" | head -1 | cut -d= -f2-)
    export DEEPSEEK_API_KEY
fi

# Activate Hermes venv
source "$HERMES_VENV/bin/activate"

# Start server
exec python3 "$SCRIPT_DIR/server.py" "$@"
