#!/bin/bash
# Copy this file and fill in your values before running.
# Or use Docker (see README) — no manual configuration needed.
cd ~/lastframe
source ~/lastframe/venv/bin/activate

# Persist the Flask secret key across restarts so sessions/cookies remain valid.
# Generated once on first run and stored in ~/lastframe/.flask_secret_key.
SECRET_KEY_FILE="$HOME/lastframe/.flask_secret_key"
if [ ! -f "$SECRET_KEY_FILE" ]; then
    (umask 077 && python3 -c 'import secrets; print(secrets.token_hex(32))' > "$SECRET_KEY_FILE")
fi
export FLASK_SECRET_KEY="$(cat "$SECRET_KEY_FILE")"

export PORT=8080

# Optional — can also be set via the setup wizard in the browser
# export JELLYFIN_URL="http://127.0.0.1:8096"
# export JELLYFIN_API_KEY="your_api_key_here"
# export JELLYFIN_WEBHOOK_SECRET="your_secret_here"

exec python3 app.py
