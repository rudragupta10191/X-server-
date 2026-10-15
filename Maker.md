# RTAD X Server — Architecture and Implementation Record

## Product architecture

X Server is a single-process Python/Flask application with SQLite persistence and server-rendered HTML/CSS/vanilla JavaScript. `x_server.py` remains the primary application and has been extended incrementally. The phone's required runtime is Python, Flask/Werkzeug, SQLite from Python's standard library, and a browser. There is no required Node.js, PHP, MySQL, Redis, Docker, cloud AI, or game server.

| Module | Boundary and actual status |
|---|---|
| X Stream | Scans only the configured local directory and approved extensions. Opaque SHA-256-derived keys identify files; resolved paths are checked against the configured directory. Files stream in bounded chunks, including single HTTP byte ranges. User state uses the existing SQLite `my_list` and `progress` records. Browser codec support remains platform-dependent. |
| Identity and sessions | Existing single local identity (`users.id=1`) and password hashes remain in the same SQLite DB. Flask signs the browser session with the preserved per-instance `data/session_secret`. Setup is first-run only; login throttling is keyed to `remote_addr`. Mutating browser routes require session authorization and CSRF validation. Logout and password change are POST-only. An additive session-version value in `settings` invalidates all old cookies after a password change. |
| Diagnostics | Authenticated read-only health/status information. It is not a terminal emulator and has no command execution or arbitrary file-read endpoint. |
| Minecraft | Independent optional manager. Java is checked but not launched by status requests. A server JAR must be selected by `XSERVER_MC_JAR`, directly under configured `XSERVER_MC_DIR`; an existing accepted EULA is required. Process identity is checked against PID, process start time, and JAR command-line argument. Stop/restart only signal a verified manager process. |
| Persistence and config | Default DB/key: `data/xserver.sqlite3`, `data/session_secret`; default logs: `logs/`. `XSERVER_DATA_DIR` and `XSERVER_LOG_DIR` can relocate them. Movie folder defaults to `/storage/emulated/0/Movies`; Settings persists it in a new additive `settings` table unless `XSERVER_MOVIE_DIR` overrides it. |
| Backup | `scripts/data_tools.py` uses SQLite's online backup interface. It verifies `PRAGMA integrity_check` and can restore only to a new, separate destination. It includes the session key if available; it does not export environment configuration. |

## Data and security boundaries

- Keep user files, database, signing key, configuration, and backups outside Git. The app does not log passwords, session values, or the TMDB key.
- A new DB and session-key file are created with restrictive permissions; the session key is forced to mode `0600`. Existing user rows/tables are not dropped, replaced, or migrated destructively.
- Schema initialization uses `CREATE TABLE IF NOT EXISTS` for current structures and adds only the `settings` table. Existing `users`, `my_list`, `progress`, `metadata`, and `login_attempts` tables remain in place.
- Passwords use Werkzeug's secure password hash helpers. Setup enforces 10–256 characters; password changes verify the current password and require a new login afterward.
- Login failure attempts are stored by remote IP for a five-minute window. The trusted client IP is Flask's direct `remote_addr`; do not deploy behind a proxy that supplies untrusted forwarding headers.
- CSRF tokens are tied to the signed session and are checked on every state-changing form/API route. Do not add state-changing GET routes.
- Session-version checks run on protected requests; when the password changes the version increments, so existing browser sessions are rejected on their next protected request. Legacy sessions without a version remain compatible until the first password change.
- Cookies are HttpOnly and SameSite=Lax. Secure cookies and HSTS are enabled only when `XSERVER_HTTPS=1` is explicitly set behind real HTTPS termination.
- Responses include nosniff, frame-denial, referrer, permissions, and CSP headers. Inline style/script allowances remain necessary for the existing server-rendered interface.
- Default bind is `127.0.0.1`. The built-in Flask server is not a public production server. No remote access, public tunnel, VPN, or Tor Onion Service is configured.
- The Minecraft manager is not a security boundary for untrusted administrators; it can execute an explicitly configured trusted Java JAR with the app user's OS permissions. Do not expose its control page publicly.

## Runtime and resource constraints

- Core dependencies: Flask/Werkzeug; SQLite is built into Python. `requests` is optional only when TMDB is configured; Java is optional only for Minecraft.
- Video transfer is streamed in 1 MiB chunks, not read wholesale into Python memory. Web browser decode buffers are outside that guarantee.
- The Minecraft heap defaults to 512 MiB and is capped at 512 MiB; native JVM memory and Android system/browser use are additional. A server is not recommended on a 2 GB OPPO A15 and is not auto-started.
- No OPPO A15 measurements or compatibility claims are made from Codespaces.

## Implementation and verification status

| Stage | Status | Evidence / boundary |
|---|---|---|
| Baseline and preservation | Complete for this execution | Baseline `main` commit `a5701ff`; no workspace runtime database/key was present; no existing account data was overwritten. |
| Core and authentication | Implemented and tested on host | Setup, login, bad-password throttling, protected route, CSRF/logout, password change, security headers, DB persistence. |
| X Stream | Existing implementation preserved; covered by tests | Actual files and formats, missing/inaccessible directory errors, path-key rejection, Range response, My List, progress, and missing-file handling. |
| Minecraft manager | Manager controls/status hardened; tests avoid game-server launch | Requires explicit JAR; real running status must match `/proc` record; forged/unverifiable PID does not cause signaling. A real Minecraft process was not launched. |
| Terminal/diagnostics | Read-only diagnostics implemented; terminal deferred | No terminal emulator or command-execution route. |
| Backup/restore | Utility and isolated rehearsal test implemented | SQLite online backup, integrity verification, restore into a new temporary location; no production restore performed. |
| Host tests | Passing | `python -m unittest discover -s tests -v`: 15 passed; compile check passed. Includes real stop/restart persistence for session, list, and progress using temporary data. |
| OPPO A15 / Termux | Pending | Requires physical target device and Android shared-storage/runtime tests. |

### Codespaces measurement

The locally started Flask server reported **41,916 kB RSS** after startup and one first-run page request. Environment: Linux 6.8 Azure x86_64, glibc 2.39, Python 3.14.2, Flask 3.1.3. This measures only the Python server at idle; it excludes browser/video decoding and is not an OPPO A15, Termux, or 2 GB device benchmark.

## References and licensing

The following source trees remain isolated and untouched under `references/`:

- `references/cine-app/`: no root `LICENSE`, `NOTICE`, or `COPYING` file was identified; reuse and redistribution rights are unresolved. No source or assets have been copied.
- `references/panel/`: inspected root `LICENSE.md` is MIT. No Pterodactyl source or dependency is included.

No adopted component or third-party asset requires an attribution addition because no source/assets were reused. Review file-level and vendored notices before any future adoption.

## OPPO A15 acceptance plan

Run and record this matrix on the physical OPPO A15, not in Codespaces:

1. Record Android 10 build, OPPO model, Termux source/version, Python version, CPU ABI, available memory/storage, and Termux storage permission state.
2. Install Flask, run `termux-setup-storage`, set a private persistent `XSERVER_DATA_DIR`, and start with the foreground command in `README.md`. Check the local login/setup page, stop with Ctrl+C, restart, and confirm session-key persistence.
3. Test shared-storage permission denial/re-grant and movie directory selection. Confirm supported extensions, Unicode filenames, one known browser-compatible codec, unsupported-codec messaging, seeking/range playback, My List, and resumed progress.
4. Measure app-process RSS at idle and during a representative library scan using Android/Termux process tools; measure browser RSS separately if available. Record the media workload, duration, peak RSS, Android baseline, temperature/battery observations, and any OS kill.
5. Confirm protected routes, CSRF rejection, login throttling, and settings persistence on device. Verify the listener remains loopback-only.
6. Do not start Minecraft for acceptance on a 2 GB device. Do not configure remote access or claim an Onion Service without a separately designed, tested private gateway.

## Backup and rollback procedure

1. Identify `XSERVER_DATA_DIR` and all environment configuration without printing secrets. No production data directory was present in the inspected workspace.
2. Before a change, stop writes where possible and run the documented `backup` command; the SQLite online backup API is also safe against active SQLite writes.
3. Store the unique generated backup outside the active data directory. Keep environment configuration separately secured. Do not copy only a live `.sqlite3` while WAL is active.
4. Run `verify` and restore to a new empty location with `restore`; check the restored app using `XSERVER_DATA_DIR` pointed to that new location.
5. Preserve the original active directory untouched until the restored instance has passed checks. The utility refuses to overwrite an existing destination and does not automatically cut over or delete any original data.

The temporary backup/restore path has automated test evidence. A production backup/restore has not been run because there was no workspace DB to protect.

## Remote access

Local loopback use is implemented independently. Remote access is future deployment work and requires an authenticated, TLS-protected VPN or reverse proxy with firewall restrictions, secure-cookie configuration, and online/offline tests. Do not expose Flask's development server, disable authentication, or treat an Onion address as security.
