# PocketBook2Craft

Sync reading highlights from PocketBook Cloud to Craft.do, running as a Docker
container — on Unraid, your Mac, or anywhere Docker runs.

## Features

- One Craft document per book in a configurable folder, with the cover image
  at the top
- Book metadata in the header: author, publisher, year, ISBN, language
- Each highlight rendered as a colored quote block in Craft (yellow / green /
  blue / purple / red / orange, matching what you marked in PocketBook)
- Choice of quote decoration style: **Focus** (vertical bar), **Block**
  (surround box), or both — set in the web UI
- Optional `#author` and `#publisher` tags for Craft tag filtering
- **Incremental sync**: only new highlights get pushed on subsequent runs.
  Existing books gain new highlights appended — never duplicated.
- **Recovery-aware**: if a Craft document is deleted manually, the next sync
  recreates it. If sync state is wiped, books are re-linked to existing Craft
  docs by title instead of duplicated.
- Small web UI for login, manual sync, and a recent-runs log
- Per-user file storage via `/config` volume; secrets at chmod 600

## Quick start

### Docker Compose

```bash
git clone https://github.com/brighterrealities/PocketBook2Craft.git
cd PocketBook2Craft
docker compose up -d
open http://localhost:8080
```

In the web UI: visit **Accounts**, log in to PocketBook (email → pick shop →
password), then paste your Craft API URL + key. Click **Sync now** on the
status page.

### Unraid (Community Apps)

1. Install `PocketBook2Craft` from **Community Apps**
2. Set a unique `/config` host path, a port, and your timezone
3. Open the WebUI link in the Docker tab, sign in to both services

### Running two instances (e.g. one per household member)

Each container is fully independent. On Unraid:

1. Install the app once from Community Apps as usual
2. **Docker tab → Add Container**
3. At the top, pick `PocketBook2Craft` from the Template dropdown
4. Change three fields:
   - **Name** → e.g. `PocketBook2Craft-Jane`
   - **WebUI port** → e.g. `8081`
   - **Config path** → e.g. `/mnt/user/appdata/pb2craft-jane`
5. Apply

Both containers run side by side, each with their own PocketBook + Craft
connections, sync state, and schedule.

Docker Compose equivalent:

```yaml
services:
  pb2craft-holger:
    image: ghcr.io/brighterrealities/pocketbook2craft:latest
    ports: ["8080:8080"]
    volumes: ["./config-holger:/config"]
    environment:
      TZ: Europe/Berlin
      PB2C_SYNC_INTERVAL_MINUTES: 60
    restart: unless-stopped

  pb2craft-jane:
    image: ghcr.io/brighterrealities/pocketbook2craft:latest
    ports: ["8081:8080"]
    volumes: ["./config-jane:/config"]
    environment:
      TZ: Europe/Berlin
      PB2C_SYNC_INTERVAL_MINUTES: 60
    restart: unless-stopped
```

## Setting up the Craft connection

In Craft → **Imagine** tab → **API** → create a new "All Documents" connection
with the **API key** access mode. Craft shows you an API URL and a key.

In the PocketBook2Craft web UI, paste:

- **API URL**: `https://connect.craft.do/links/<your-id>/api/v1`
- **API token**: the key Craft shows
- **Folder name**: `PocketBook Imports` (or whatever you want — it'll be
  created automatically)

Click **Save & test**. The app does a read-only `GET /folders` to verify the
token before persisting.

## Configuration

All settings can be configured via the web UI. Environment variables seed
defaults on first boot:

| Variable | Default | Purpose |
|---|---|---|
| `PB2C_CONFIG_DIR` | `/config` | Where credentials + sync state live |
| `PB2C_WEB_PORT` | `8080` | Port the web UI listens on |
| `PB2C_SYNC_INTERVAL_MINUTES` | `60` | Auto-sync interval. `0` disables the scheduler (manual sync only) |
| `PB2C_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `TZ` | (none) | Timezone for log timestamps and the dashboard's "last sync" display |
| `PB2C_CRAFT_API_URL` | (none) | Optional seed for Craft API URL — UI value overrides on save |
| `PB2C_CRAFT_API_TOKEN` | (none) | Same — Craft API token seed |
| `PB2C_CRAFT_FOLDER_NAME` | `PocketBook Imports` | Default folder name on first boot |

PocketBook credentials are entered only through the web UI (they require the
shop-discovery flow which can't be seeded from env vars).

## How sync works

1. Fetches all books from PocketBook Cloud
2. For each book with highlights:
   - Fetches every highlight, merges split multi-page highlights, filters
     bookmark markers
   - Looks up local state: is this book already linked to a Craft doc?
     - **No, and folder is empty** → create a new Craft doc, upload cover,
       add all highlights as colored quote blocks
     - **No, but doc with matching title exists in Craft** → re-link without
       re-uploading (recovery from lost state)
     - **Yes** → check the doc still exists; append only new highlights
       (incremental). If the doc was deleted in Craft, recreate it.
3. Records the result in the local SQLite state + run log

The sync is single-flighted: clicking "Sync now" while a scheduled run is
in progress doesn't queue a second one.

## Architecture

```
PocketBook Cloud API  ──►  sync orchestrator  ──►  Craft.do API
                                  │
                                  ▼
                          SQLite state
                          (/config volume)
```

- Python 3.12, FastAPI, httpx, APScheduler
- Auth: `Authorization: Bearer <token>` for Craft; password-grant + refresh
  tokens for PocketBook Cloud
- Per-book Craft doc created via `POST /documents`, content inserted via
  `POST /blocks` (one text block per highlight with `<highlight color>` inline)
- Covers uploaded via `POST /upload` (with one retry + fresh TLS connection on
  transient failures)

## Local development

```bash
docker compose up --build
```

Or without Docker:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest          # 184 tests, ~2s
.venv/bin/python -m pb2craft.main   # serves on :8080
```

Persistent state lives in `./config/` (gitignored).

## Origin

Rewritten in Python from
[PocketBook2Capacities](https://github.com/sneakinhysteria/PocketBook2Capacities),
the Mac menu-bar predecessor. The CFI parser, highlight merger, and bookmark
filter are direct ports of the Swift originals; the Craft client is new.

## License

MIT
