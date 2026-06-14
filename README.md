# LastFrame

A self-hosted web dashboard for tracking and managing watch history across multiple users. Browse your libraries, see per-user watch progress, filter by genre or completion status, and automatically delete watched media after a configurable grace period.

LastFrame supports two media server backends, each maintained on its own branch.

---

## Choose your backend

### Jellyfin

```bash
git clone -b feature/jellyfin https://github.com/shikigamixii/LastFrame.git ~/lastframe
```

See the [`feature/jellyfin` README](https://github.com/shikigamixii/LastFrame/blob/feature/jellyfin/README.md) for full setup instructions (Docker quickstart, webhook setup, auto-delete configuration).

### Plex

```bash
git clone -b feature/plex https://github.com/shikigamixii/LastFrame.git ~/lastframe
```

See the [`feature/plex` README](https://github.com/shikigamixii/LastFrame/blob/feature/plex/README.md) for full setup instructions (Docker quickstart, webhook setup, auto-delete configuration). Plex webhook support requires Plex Pass.

---

## Features (both backends)

- Per-user watch progress tracking for movies, seasons, and episodes
- User assignment — assign specific users to a series or movie so only their watch status counts
- Genre filter chips and "All Watched" toggle on the library grid
- Delete watched episodes, seasons, or movies directly from disk
- Auto-delete — automatically remove fully-watched media after a configurable grace period, with per-library and per-series/movie overrides
- Bulk assign users to multiple items at once
- Recent activity dashboard on the home screen
- Real-time watch event recording via webhooks
- Browser-based first-time setup wizard — no terminal prompts required
- Admin login with bcrypt passwords and rate limiting

## Requirements

- A running Jellyfin server (with an API key) **or** Plex Media Server (with a valid Plex Token)
- **Docker** (recommended) or Python 3.10+

---

This branch (`main`) is a landing page only. All install and configuration steps live on the backend-specific branches above.
