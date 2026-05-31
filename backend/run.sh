#!/bin/bash
# Run script for VoiceMate backend
# Sources DeepSeek API key from Hermes config automatically

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_ENV="$HOME/.hermes/.env"

# Load Hermes .env (only DEEPSEEK_API_KEY)
if [ -f "$HERMES_ENV" ]; then
    DEEPSEEK_API_KEY=$(grep -E '^DEEPSEEK_API_KEY=' "$HERMES_ENV" | head -1 | cut -d= -f2-)
    export DEEPSEEK_API_KEY
fi

# Use local venv when present; otherwise fall back to the active Python.
if [ -x "$SCRIPT_DIR/venv/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/venv/bin/python"
else
    PYTHON="${PYTHON:-python3}"
fi

exec "$PYTHON" "$SCRIPT_DIR/server.py" "$@"
