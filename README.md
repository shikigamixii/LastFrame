# LastFrame (Jellyfin)

Built with Claude Code but there is no AI baked into this project.

A self-hosted web dashboard for tracking and managing Jellyfin watch history across multiple users. Browse your libraries, see per-user watch progress, filter by genre or completion status, and automatically delete watched media after a configurable grace period.

## Features

- Per-user watch progress tracking for movies, seasons, and episodes
- User assignment — assign specific users to a series or movie so only their watch status counts
- Genre filter chips and "All Watched" toggle on the library grid
- Delete watched episodes, seasons, or movies directly from disk
- Auto-delete — automatically remove fully-watched media after a configurable grace period, with per-library and per-series/movie overrides
- Bulk assign users to multiple items at once
- Recent activity dashboard on the home screen
- Jellyfin webhook support for real-time watch event recording
- Browser-based first-time setup wizard — no terminal prompts required
- Admin login with bcrypt passwords and rate limiting

## Requirements

- A running Jellyfin server with an API key
- **Docker** (recommended) or Python 3.10+

---

## Installation

### Option A — Docker (recommended)

#### 1. Clone the repository

```bash
git clone -b feature/jellyfin https://github.com/shikigamixii/LastFrame.git ~/lastframe
cd ~/lastframe
```

#### 2. Create a `.env` file

```bash
# Required
echo "JELLYFIN_API_KEY=your_api_key_here" > .env
echo "FLASK_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env
echo "JELLYFIN_WEBHOOK_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(24))')" >> .env
echo "HOST_PORT=<CHOOSE YOUR PORT>" >> .env

# Optional — only needed if Jellyfin isn't on the same host
# echo "JELLYFIN_URL=http://<your-server-ip>:8096" >> .env
```

You can find or generate a Jellyfin API key under **Dashboard → API Keys** in your Jellyfin admin panel.

> **Note:** `JELLYFIN_API_KEY` and `JELLYFIN_WEBHOOK_SECRET` can also be entered through the setup wizard instead of `.env`. `FLASK_SECRET_KEY` is always required in `.env`.

#### 3. Start the container

```bash
docker compose up -d --build
```

#### 4. Complete setup in your browser

Navigate to `http://<your-server-ip>:<your port>` — the setup wizard will appear on first run. Create your admin account and confirm your Jellyfin connection, then you're in.

Data (`config.json`, `data.db`, `audit.log`) is stored in a named Docker volume (`lastframe_lastframe_data`) and persists across restarts and rebuilds.

---

### Option B — Synology NAS (Container Manager)

Tested on DSM 7.2 with **Container Manager** (Docker package on older DSM works the same way).

#### 1. Copy the project to your NAS

SSH in and clone into a folder under your `docker` share (create the share first in **Control Panel → Shared Folder** if it doesn't exist):

```bash
ssh admin@<your-nas-ip>
cd /volume1/docker
sudo git clone https://github.com/kricha04/LastFrame.git lastframe
cd lastframe
```

If you don't have SSH enabled, you can also upload the repo as a zip via **File Station** and extract it to `/volume1/docker/lastframe`.

#### 2. Create the data folder

```bash
sudo mkdir -p /volume1/docker/lastframe/data
sudo chmod 777 /volume1/docker/lastframe/data
```

This is where `config.json`, `data.db`, and `audit.log` will live. Bind-mounting it (instead of using a named Docker volume) means you can see and back up the data through File Station and Hyper Backup.

#### 3. Create a `.env` file in `/volume1/docker/lastframe`

```bash
sudo tee .env > /dev/null <<EOF
JELLYFIN_API_KEY=your_api_key_here
FLASK_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
JELLYFIN_WEBHOOK_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
# Required — choose any free port that doesn't conflict with DSM's 8080.
HOST_PORT=<choose your port number>
# Optional — only needed if Jellyfin isn't reachable at the default
# JELLYFIN_URL=http://<your-jellyfin-ip>:8096
EOF
```

> **Heads up:** DSM's web UI uses port 8080 by default, so pick any free port for `HOST_PORT` that isn't already in use on your NAS.

#### 4. Start the container

A Synology-specific compose file (`docker-compose.synology.yml`) ships with the repo. It uses a bind mount to `/volume1/docker/lastframe/data` and publishes on whichever `HOST_PORT` you set in `.env`.

**Option 1 — Shell (simplest, no merge conflicts on update):**

```bash
cd /volume1/docker/lastframe
sudo docker compose -f docker-compose.synology.yml up -d --build
```

**Option 2 — Container Manager GUI:**

1. Open **Container Manager → Project → Create**
2. **Project name:** `lastframe`
3. **Path:** `/volume1/docker/lastframe`
4. **Source:** *Create docker-compose.yml*
5. Paste the contents of `docker-compose.synology.yml` (open it in File Station or `cat docker-compose.synology.yml` over SSH) into the text editor that appears, then click **Next**.
6. Click **Next** through the environment screen (your `.env` is already picked up)
7. Click **Done** to build and start

#### 5. Open the dashboard

Browse to `http://<your-nas-ip>:<HOST_PORT>` (whichever port you chose in `.env`). The setup wizard appears on first run.

#### Updating on Synology

```bash
cd /volume1/docker/lastframe
sudo git pull
sudo docker compose -f docker-compose.synology.yml up -d --build
```

(If you used Container Manager, re-paste the contents into the project, or click **Build** in **Container Manager → Project → lastframe**.) Your data in `/volume1/docker/lastframe/data` is preserved.

---

### Option C — Manual (Python)

#### 1. Clone the repository

```bash
git clone -b feature/jellyfin https://github.com/shikigamixii/LastFrame.git ~/lastframe
cd ~/lastframe
```

#### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### 3. Configure environment variables

```bash
export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export PORT=8080

# Optional — can be configured in the setup wizard instead
export JELLYFIN_URL="http://127.0.0.1:8096"
export JELLYFIN_API_KEY="your_api_key_here"

# Required (here or in the setup wizard) — protects the webhook endpoint
export JELLYFIN_WEBHOOK_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
```

#### 4. Run the app

```bash
gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 60 app:app
```

Or use the included start script (edit it first with your values):

```bash
chmod +x start.sh
./start.sh
```

> The bundled `Dockerfile` and `start.sh` both use **gunicorn** with one worker and a thread pool — the auto-delete sweep needs to run in a single process so it doesn't race on SQLite. For quick local development you can still run `python3 app.py` directly.

#### 5. Complete setup in your browser

Navigate to `http://<your-server-ip>:PORT` — the setup wizard appears on first run.

---

## Running as a systemd service (optional)

Create `/etc/systemd/system/lastframe.service`:

```ini
[Unit]
Description=LastFrame Jellyfin Dashboard
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/home/your_user/lastframe
Environment=JELLYFIN_URL=http://127.0.0.1:8096
Environment=JELLYFIN_API_KEY=your_api_key_here
Environment=JELLYFIN_WEBHOOK_SECRET=your_webhook_secret_here
Environment=PORT=8080
Environment=FLASK_SECRET_KEY=your_secret_key_here
ExecStart=/home/your_user/lastframe/venv/bin/gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 4 --timeout 60 app:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable lastframe
sudo systemctl start lastframe
```

---

## Serving over HTTPS (optional)

LastFrame ships configured for plain HTTP on your LAN, which is the documented
setup above. If you put it behind a reverse proxy with TLS (Caddy, nginx,
Traefik, etc.), set `SESSION_COOKIE_SECURE=1` in your environment so the
session cookie carries the `Secure` flag:

```bash
echo "SESSION_COOKIE_SECURE=1" >> .env
```

Leave it unset (or `0`) for HTTP access — otherwise browsers drop the session
cookie and the setup wizard / login will fail CSRF validation.

---

## Jellyfin Webhook Setup

Webhooks let LastFrame record watch events in real time without waiting for a manual history import. **The webhook endpoint is always authenticated** — `JELLYFIN_WEBHOOK_SECRET` is required at setup and the endpoint rejects requests that don't carry the matching secret.

1. Install the **Webhook** plugin in Jellyfin (Dashboard → Plugins → Catalog)
2. Add a new webhook pointing to: `http://<your-server-ip>:PORT/api/webhook/jellyfin?secret=<your_webhook_secret>`
3. Enable both notification types: **Playback Stop** (counts a real watch when playback reached the completion threshold) and **User Data Saved** (counts the manual "Mark as Played" toggle in Jellyfin)

Without webhooks, LastFrame can still import existing watch history using **Settings → Import Jellyfin History**.

---

## Auto-Delete

LastFrame can automatically delete media after all assigned users have watched it and a grace period has elapsed.

- Enable per-library in **Settings → Auto-Delete Libraries**
- Or enable per-series/movie from the item's detail page
- Set grace period (days) and minimum delay after completion (minutes) in Settings
- Use **Run sweep now** to trigger an immediate check

---

## Updating

### Docker

```bash
git pull
docker compose up -d --build --build
```

Your data volume is preserved automatically.

### Manual / systemd

```bash
git pull
sudo systemctl restart lastframe
```
