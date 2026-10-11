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
- Recently Added — a triage list of newly added movies and series; assigning
  users to a title (or deleting it) takes it off the list
- Recent activity dashboard on the home screen
- Real-time watch event recording via webhooks (Plex webhooks require Plex Pass)
- Console page — live in-app logs with level and text filters
- Browser-based first-time setup wizard — no terminal prompts required
- Admin login with bcrypt passwords and rate limiting

## Requirements

- A running **Plex Media Server** (with a valid Plex Token) and/or a
  **Jellyfin server** (with an API key) — at least one
- **Docker** (recommended) or Python 3.10+

### Supported library types

Only **Movies** and **TV Shows** libraries can be monitored.

- **Jellyfin:** the library's content type must be **Movies** or **Shows**.
  *Mixed Movies and Shows* libraries, libraries with no content type, and
  Music / Books / Photos / Home Videos / Music Videos libraries are not
  supported. They appear greyed out in **Settings → Select Libraries to
  Monitor** with the reason. Jellyfin doesn't let you change an existing
  library's content type, so to use one with LastFrame, create a new Movies
  or Shows library pointing at the same folders.
- **Plex:** Movie and TV Show libraries are supported; Music and Photo
  libraries are not listed.

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
# echo "JELLYFIN_URL=https://<your-jellyfin-host>" >> .env
```

- Find your **Plex Token**: [Finding an authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
- Find/generate a **Jellyfin API key** under **Dashboard → API Keys**.

> Any credential left out of `.env` can be entered in the setup wizard instead.
> A **webhook secret is required** to finish setup — either set it in `.env`
> as above, or enter one (at least 16 characters) in the wizard.

#### 3. Set the data folder

`docker-compose.yml` stores LastFrame's database, config and logs in a host
folder. The default is a Synology path:

```yaml
    volumes:
      - /volume1/docker/lastframe/data:/data
```

If you're not on a Synology NAS, change the left-hand side to a folder that
exists on your machine (for example `./data:/data`). Keep `:/data` unchanged.

LastFrame runs as an unprivileged user (not root), so that user must own the
data folder. By default it runs as `1000:1000`. Either give the folder to
that user:

```bash
sudo chown -R 1000:1000 /volume1/docker/lastframe/data
```

or tell LastFrame to run as the folder's current owner by adding its user and
group IDs to `.env` (find them with `ls -ln` on the folder's parent):

```bash
echo "PUID=1026" >> .env   # example: a Synology DSM user
echo "PGID=100" >> .env    # example: Synology's "users" group
```

> **Upgrading from a version that ran as root:** files created by the old
> container are owned by root, so the new one can't read them. Run the
> `chown` above once (or set `PUID`/`PGID`), then restart. If the folder
> isn't usable, LastFrame stops with a message naming the user it runs as
> and the exact command to fix it — it never starts with missing settings.

#### 4. Start it

```bash
docker compose up -d --build
```

Open `http://<your-host>:<HOST_PORT>` and complete the first-time setup:
create an admin account, then configure Plex, Jellyfin, or both.

---

## Running without Docker

```bash
cd ~/lastframe
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./start.sh
```

`start.sh` generates and keeps a Flask secret key, then starts the app with
gunicorn on port 8080. Credentials can be set with the commented-out
`export` lines in `start.sh` or entered in the setup wizard. Without
`DATA_DIR` set, data is stored in the app folder.

---

## Webhooks

Real-time watch events keep per-user watch state accurate. Each provider has
its own endpoint:

| Provider  | Webhook URL                                   | Where to add it |
| --------- | --------------------------------------------- | --------------- |
| Plex      | `https://<host>/api/webhook/plex`             | Plex → Settings → Webhooks (requires Plex Pass) |
| Jellyfin  | `https://<host>/api/webhook/jellyfin`         | Jellyfin → Dashboard → Plugins → Webhook |

Both URLs are shown (with a copy button) in **Settings → Webhooks** once the
corresponding provider is enabled.

### The webhook secret

Every webhook request must carry the secret, either:

- as a query parameter: `.../api/webhook/plex?secret=<your secret>`, or
- as a request header: `X-Webhook-Secret: <your secret>`

Plex can't send custom headers, so Plex always uses `?secret=`. For Jellyfin,
the header is preferred because it keeps the secret out of URLs and server
access logs.

Which secret applies:

- `PLEX_WEBHOOK_SECRET` / `JELLYFIN_WEBHOOK_SECRET` (environment), if set, are
  used for that provider.
- Otherwise both providers use the single secret entered in the setup wizard.

If your media server reaches LastFrame over the internet, use an `https://`
address (for example through a reverse proxy) so the secret isn't sent in
plain text.

### Jellyfin webhook plugin setup

1. Install the **Webhook** plugin from the Jellyfin plugin catalog and restart
   Jellyfin.
2. In the plugin settings, add a **Generic Destination**.
3. **Webhook URL:** your Jellyfin webhook URL from the table above.
4. **Notification types:** enable **Playback Stop** and **User Data Saved**.
   LastFrame ignores all other types.
5. **Item types:** enable **Movies** and **Episodes**.
6. Enable **Send All Properties (ignores template)** so every field LastFrame
   needs is included.
7. Under **Request Headers**, add `X-Webhook-Secret` with your secret as the
   value (or append `?secret=<your secret>` to the URL instead).

LastFrame records a watch when a **Playback Stop** event reports the item was
played to completion, or a **User Data Saved** event marks it as played. It
reads `NotificationType`, `UserId`, `ItemType`, `ItemId`,
`PlayedToCompletion` / `Played`, and the `Provider_tmdb` / `Provider_tvdb` /
`Provider_imdb` ids. If you use a custom template instead of "Send All
Properties", it must include these fields.

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

Go to **Settings → Media Servers**. Each provider has:

- an on/off toggle, and
- fields for its server URL and token / API key. Leave the token or key box
  blank to keep the current one.

To add a provider you didn't configure at setup, enter its URL and token / API
key there, click Save, then reopen Settings and turn it on. Credentials set
through environment variables take precedence and can't be changed from
Settings.

---

## Optional environment variables

With Docker, only variables listed under `environment:` in
`docker-compose.yml` reach the container. `SESSION_COOKIE_SECURE` is already
listed (set it in `.env`); add the others there yourself if you need them.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SESSION_COOKIE_SECURE` | off | Set to `1` when LastFrame is served over HTTPS |
| `TRUSTED_PROXY_HOPS` | `0` | Number of reverse proxies in front of LastFrame; set to `1` behind a single proxy so client IPs and HTTPS are detected correctly |
| `PLEX_HISTORY_POLL_INTERVAL` | `300` | Seconds between Plex play-history polls (for users that don't send webhooks) |
| `RECENTLY_ADDED_SWEEP_INTERVAL` | `1800` | Seconds between Recently Added scans |

---

## Troubleshooting & logs

- **Console page:** the **🖥 Console** button in the sidebar shows the app's
  recent logs (last 2000 lines, cleared on restart), filterable by level:
  Verbose (every request), Debug (timings and media-server calls), Info,
  Warning and Error.
- **Container log:** `docker logs -f lastframe` (or the Log tab in your
  container manager).
- **Log file:** `audit.log` in the data folder — logins, deletions,
  auto-delete actions and errors.

Tokens and API keys are masked as `[REDACTED]` in all logs.

Common problems:

- **A library doesn't appear in Settings** — check its type; see
  [Supported library types](#supported-library-types).
- **Jellyfin returns 401 / nothing loads with a recent Jellyfin version** —
  update LastFrame; current versions authenticate with the
  `Authorization: MediaBrowser Token="…"` header that newer Jellyfin requires.
- **Watches aren't recorded in real time** — check the webhook URL, the
  secret, and (for Jellyfin) the notification types above. The Console
  shows each incoming webhook request.

---

## Notes

- Plex: enable **"Allow media deletion"** in Plex Settings → Troubleshooting for
  deletes to remove files from disk.
- Plex managed (Plex Home) users don't emit webhooks; LastFrame polls Plex play
  history to keep their watch state current (`PLEX_HISTORY_POLL_INTERVAL`).
