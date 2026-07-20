# LastFrame

Built with Claude Code but there is no AI baked into this project.

A self-hosted web dashboard for tracking and managing watch history across
multiple users. Browse your libraries, see per-user watch progress, filter by
genre or completion status, and automatically delete watched media after a
configurable grace period.

**LastFrame talks to Plex and Jellyfin — either one, or both at the same
time.** A single instance aggregates libraries, users, activity and watch
history across every configured server, and each server can be toggled on or
off independently at any time from Settings.

---

## Features

- Works with **Plex, Jellyfin, or both simultaneously** — toggle either
  provider on/off without losing configuration
- Per-user watch progress tracking for movies, seasons, and episodes
- User assignment — assign specific users to a series or movie so only their
  watch status counts
- Genre filter chips and "All Watched" toggle on the library grid
- Delete watched episodes, seasons, or movies directly from disk
- Auto-delete — automatically remove fully-watched media after a configurable
  grace period, with per-library and per-series/movie overrides
- Bulk assign users to multiple items at once
- Recent activity dashboard on the home screen
- Real-time watch event recording via webhooks (Plex webhooks require Plex Pass)
- Browser-based first-time setup wizard — no terminal prompts required
- Admin login with bcrypt passwords and rate limiting

## Requirements

- A running **Plex Media Server** (with a valid Plex Token) and/or a
  **Jellyfin server** (with an API key) — at least one
- **Docker** (recommended) or Python 3.10+

---

## Quick start (Docker)

#### 1. Clone the repository

```bash
git clone -b develop https://github.com/shikigamixii/LastFrame.git ~/lastframe
cd ~/lastframe
```

#### 2. Create your `.env`

`FLASK_SECRET_KEY` is always required. Everything else (server URLs, tokens,
API keys, webhook secret) can be provided here **or** entered later in the
browser setup wizard — you only need to configure the provider(s) you use.

```bash
# Required
echo "FLASK_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > .env
echo "HOST_PORT=<choose your port number>" >> .env

# One secret per webhook endpoint (they can share the same value)
echo "PLEX_WEBHOOK_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(24))')" >> .env
echo "JELLYFIN_WEBHOOK_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(24))')" >> .env

# Plex (optional — omit if you only use Jellyfin)
# echo "PLEX_TOKEN=your_plex_token_here" >> .env
# echo "PLEX_URL=https://<your-server>.plex.direct:32400" >> .env

# Jellyfin (optional — omit if you only use Plex)
# echo "JELLYFIN_API_KEY=your_api_key_here" >> .env
# echo "JELLYFIN_URL=http://<your-server-ip>:8096" >> .env
```

- Find your **Plex Token**: [Finding an authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
- Find/generate a **Jellyfin API key** under **Dashboard → API Keys**.

> Any credential left out of `.env` can be entered in the setup wizard instead.

#### 3. Start it

```bash
docker compose up -d --build
```

Open `http://<your-host>:<HOST_PORT>` and complete the first-time setup:
create an admin account, then configure Plex, Jellyfin, or both.

---

## Webhooks

Real-time watch events keep per-user watch state accurate. Each provider has
its own endpoint (both protected by the webhook secret — append `?secret=…`):

| Provider  | Webhook URL                                   | Where to add it |
| --------- | --------------------------------------------- | --------------- |
| Plex      | `http://<host>:<port>/api/webhook/plex`       | Plex → Settings → Webhooks (requires Plex Pass) |
| Jellyfin  | `http://<host>:<port>/api/webhook/jellyfin`   | Jellyfin → Dashboard → Webhooks plugin |

Both URLs are shown (with a copy button) in **Settings → Webhooks** once the
corresponding provider is enabled.

---

## How multi-provider works

- Items and accounts from each server are internally namespaced (`px_…` for
  Plex, `jf_…` for Jellyfin) so ids from the two servers never collide.
- External provider ids (TMDb/IMDb/TVDb) are shared, so the same title on both
  servers is still recognised as the same title for assignments.
- List views (libraries, users, search, recent activity) aggregate across every
  enabled provider; per-item actions (watch status, delete, auto-delete) are
  routed to the server that owns the item.
- Turning a provider off in Settings stops all querying of that server and
  hides its libraries and users, without deleting any stored configuration.

---

## Enabling / disabling a provider later

Go to **Settings → Media Servers** and toggle a provider on or off. To add a
provider you didn't configure at setup, set its credentials via environment
variables (and restart) or through the config API, then enable it.

## Notes

- Plex: enable **"Allow media deletion"** in Plex Settings → Troubleshooting for
  deletes to remove files from disk.
- Plex managed (Plex Home) users don't emit webhooks; LastFrame polls Plex play
  history to keep their watch state current (`PLEX_HISTORY_POLL_INTERVAL`).
