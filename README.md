# RTAD IDENTITY X Server

X Server is a private, lightweight Flask and SQLite personal control center. X Stream indexes local video files; it does not download films or require online metadata. The application defaults to localhost and does not start optional services automatically.

## Requirements

- Python 3.9 or newer (Python 3.14 was used in Codespaces verification).
- Flask, installed with `python -m pip install -r requirements.txt`.
- SQLite through Python's standard-library `sqlite3`.
- No Node.js, PHP, MySQL, Redis, Docker, Java, or cloud AI service is required for the core app.

## Start, stop, status, and tests

From the repository directory:

```sh
python -m pip install -r requirements.txt
python x_server.py
```

Open `http://127.0.0.1:5000`. On first run, create the one local account. Keep the server in the foreground; press **Ctrl+C in that Termux/Codespaces terminal** to stop it cleanly. Check whether it is responding from another terminal:

```sh
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:5000/login
```

The expected status is `200` (or a redirect if application state changes). Run automated tests:

```sh
python -m unittest discover -s tests -v
```

The development server is only intended for private, single-process use. It is not a public internet deployment server.

## Configuration and data locations

Defaults:

- Bind: `127.0.0.1:5000`.
- SQLite database and signing key: `data/xserver.sqlite3` and `data/session_secret`.
- Logs: `logs/`.
- Movie directory: `/storage/emulated/0/Movies` when accessible.
- Minecraft directory: `~/mc_server`; no JAR is selected unless explicitly configured.

Environment variables:

| Variable | Purpose |
|---|---|
| `XSERVER_DATA_DIR` | Directory for the database, signing key, and Minecraft PID record. Set before first start to place persistent data elsewhere. |
| `XSERVER_LOG_DIR` | Optional log directory; defaults to the repository's `logs/`. |
| `XSERVER_MOVIE_DIR` | Movie directory override. When set, it takes precedence over the saved Settings value. |
| `XSERVER_HOST` / `XSERVER_PORT` | Listener address and port. Host defaults to `127.0.0.1`; do not expose it publicly. |
| `XSERVER_HTTPS=1` | Enables secure-cookie/HSTS behavior only when a trusted TLS terminator actually serves HTTPS. Flask itself does not terminate TLS. |
| `TMDB_API_KEY` | Optional online artwork/details lookup. Local library, playback, list, and progress remain usable without it. Install `requests` separately only if enabling this option. |
| `XSERVER_MC_DIR` | Directory for an optional Minecraft server; defaults to `~/mc_server`. |
| `XSERVER_MC_JAR` | Explicit server JAR filename/path directly inside `XSERVER_MC_DIR`. Starting is unavailable without this setting and a valid JAR. |
| `XSERVER_MC_MAX_MEMORY_MB` | Java heap limit; defaults to 512 MiB and accepts 128–512 MiB. JVM/native overhead is additional. |

The Settings page saves the movie directory to SQLite when `XSERVER_MOVIE_DIR` is not set. Database table creation is additive (`CREATE TABLE IF NOT EXISTS`); existing user, My List, progress, metadata, and login-attempt records are not replaced. Do not delete `session_secret`: doing so invalidates existing signed sessions. Back up application data before manual configuration/data changes.

Change the account password while signed in from **Password** in the navigation. The current password is required; a successful change increments the local session version, revokes other signed-in sessions, and returns the current browser to login. There is no universal recovery code; local recovery requires a separately planned administrative procedure.

## X Stream

Supported file extensions are `.mp4`, `.mkv`, `.webm`, `.mov`, `.m4v`, and `.avi`. X Stream reports missing/inaccessible folders, displays actual scanned files, supports title search and sorting, My List, and persisted resume progress. Video is streamed in chunks with HTTP byte-range support; the server does not load the whole file into memory. Codec/container support depends on the browser; MKV and AVI are not natively playable in every browser.

On Termux, install Python, grant shared-storage access, keep the DB and key in Termux's private home storage, and set the media path:

```sh
pkg update
pkg install python
termux-setup-storage
mkdir -p "$HOME/.local/share/xserver"
chmod 700 "$HOME/.local/share/xserver"
export XSERVER_DATA_DIR="$HOME/.local/share/xserver"
export XSERVER_MOVIE_DIR=/storage/emulated/0/Movies
python -m pip install -r requirements.txt
python x_server.py
```

The storage permission prompt and Android 10 shared-storage behavior must be tested on the target phone. Do not assume Codespaces storage access predicts Termux access.

## Minecraft manager and diagnostics

`/minecraft` reports Java, the configured JAR, logs, and a process only when its PID, start time, and command line match the manager's recorded JAR. Start/stop/restart are authenticated and CSRF-protected. No server is auto-started, no software is downloaded, and only an explicitly configured JAR directly inside the configured server directory is eligible.

**A 512 MiB Java heap plus JVM/native memory is not a safe promise on a 2 GB phone.** The manager must remain stopped on the OPPO A15 unless real-device resource testing demonstrates adequate headroom. No real Minecraft server was launched in this implementation or test suite. Java status checks do not start a game server.

`/diagnostics` is a protected, read-only status view. No terminal emulator, shell route, or client-selected command execution is implemented.

## Backup, verification, and restore

The backup utility uses SQLite's online backup API, creates a unique timestamped destination outside the active data directory, preserves `session_secret` when present, uses restrictive permissions, and validates the resulting database. It never overwrites a backup:

```sh
python scripts/data_tools.py backup \
  --data-dir "${XSERVER_DATA_DIR:-./data}" \
  --backup-dir "$HOME/xserver-backups"
python scripts/data_tools.py verify "$HOME/xserver-backups/xserver-<timestamp>"
```

Restore only creates a **new destination directory** and refuses to overwrite an existing one. Use a separate location for rehearsal:

```sh
python scripts/data_tools.py restore \
  "$HOME/xserver-backups/xserver-<timestamp>" \
  "$HOME/xserver-restore-test"
XSERVER_DATA_DIR="$HOME/xserver-restore-test" python x_server.py
```

Stop the app before any operator-managed production cutover. Preserve the old data directory and shell/environment configuration as a rollback copy; this utility does not copy environment variables or secrets beyond the app's session signing key, and it never replaces the active database. Confirm the restored instance works on the new data path before deciding whether to switch configuration.

## Termux and private access

In Termux, install Python using the Termux package manager, install the Flask dependency from `requirements.txt`, run `termux-setup-storage`, and start with `python x_server.py`. Keep the process in a foreground Termux session and account for Android battery/background restrictions. Put `XSERVER_DATA_DIR` and any optional configuration in a private Termux shell startup file with restrictive permissions; do not paste API keys into commands that may be saved in shell history.

The default listener is loopback-only. LAN/VPN/Tor access is not configured or implemented. A future private-access deployment requires a separately reviewed TLS/VPN/reverse-proxy boundary, firewall restrictions, secure cookies, and on-host tests; never expose Flask's development server directly to the public internet. No Onion Service or OPPO A15 compatibility claim is made.

## References and licensing

The remaining content under `references/` is kept isolated and is not imported into the application. Cine App reuse/redistribution rights remain unresolved; its source is not copied. No terminal emulator or shell backend is included. Pterodactyl is not a dependency. Review each upstream license and file-level notices before any future reuse.

## Verification status

The host-side automated suite uses temporary databases/media and verifies setup/login, throttling, CSRF, protected routes, schema/data persistence, scanning and failure handling, range playback, My List/progress, diagnostics, Minecraft status without starting a game server, and backup/restore to an isolated directory. OPPO A15/Android 10/Termux startup, mobile browser codecs, performance, networking, and memory remain unverified and require the device.
