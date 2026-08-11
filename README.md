# PerKan (Simple Flask Kanban)

A minimal web-based Kanban board powered by Flask and a single JSON data file.

> This is a fork of [sweenig/perkan](https://github.com/sweenig/perkan) with two-way Google
> Calendar sync added — see the [Google Calendar Sync](#google-calendar-sync) section below.
> All credit for the original board/project/list functionality goes to Stuart Weenig.

## Python

1. python -m venv venv
2. venv\Scripts\activate (Windows) or source venv/bin/activate
3. pip install --break-system-packages -r requirements.txt
4. python app.py

Open http://127.0.0.1:5000

## Docker

- Run with the local `data` directory (recommended — run from the `perkan` folder):

`docker run -p 5000:5000 -v "$(pwd)/data:/app/data" perkan`

- Docker Compose (recommended):

From the `perkan` directory:

`docker compose up --build -d`

The service runs Gunicorn with conservative settings (2 workers, 4 threads, 60s timeout).

## Google Calendar Sync

PerKan has no user accounts, so this connects the *whole board* to a single Google account —
there's no per-card "which calendar" choice, just one shared connection.

**What it does:**

- Give a card a due date and it's pushed to Google Calendar as an event (30 minutes long, or
  all-day if you check "All day").
- Editing or deleting the card updates or deletes the linked event.
- A background job polls Google roughly once a minute (configurable) and pulls changes back:
  edits to an event's title/description/time are copied onto the linked card, and events
  created directly in Google Calendar are imported as new cards.
- If an event is deleted on the Google side, PerKan **unlinks** it from the card rather than
  deleting the card — a task disappearing because someone declined or deleted a calendar
  invite would be a bad surprise. Deleting the card in PerKan does delete the calendar event.
- Conflicts (edited in both places since the last sync) are resolved last-write-wins, compared
  by timestamp.

**Setup:**

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable the
   **Google Calendar API**, and create an **OAuth 2.0 Client ID** (Web application type).
2. Add an authorized redirect URI matching `GOOGLE_REDIRECT_URI` below (default
   `http://127.0.0.1:5000/auth/google/callback`).
3. Set these environment variables (e.g. in a `.env` file, which is already gitignored):

   | Variable | Default | Purpose |
   |---|---|---|
   | `GOOGLE_CLIENT_ID` | *(required)* | OAuth client ID |
   | `GOOGLE_CLIENT_SECRET` | *(required)* | OAuth client secret |
   | `GOOGLE_REDIRECT_URI` | `http://127.0.0.1:5000/auth/google/callback` | Must match the Cloud Console redirect URI exactly |
   | `GOOGLE_CALENDAR_ID` | `primary` | Which calendar to sync |
   | `GCAL_TIMEZONE` | `UTC` | IANA timezone (e.g. `America/New_York`) used to interpret card due-date/times. Set this to your actual timezone or events will show at the wrong time. |
   | `GCAL_SYNC_INTERVAL_SECONDS` | `60` | How often to poll for calendar-side changes |
   | `GCAL_IMPORT_NEW_EVENTS` | `true` | Whether events created directly in Google Calendar become new cards |
   | `GCAL_DEFAULT_COLUMN` | `todo` | Column newly-imported events land in |
   | `GCAL_SYNC_WINDOW_DAYS` | `90` | How far ahead to look for events on the first (non-incremental) sync |

4. Start the app and open **Settings → 📅 Calendar → Connect Google Calendar**.

**Note on multiple Gunicorn workers:** each worker runs its own polling thread. They throttle
themselves against duplicate work via the shared token file, but if you want a strictly single
poller, run with `--workers 1` when Calendar sync is enabled.