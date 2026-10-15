# X Server Implementation Update

**Date:** 2026-10-15
**Product:** RTAD / X Server
**Target:** OPPO A15, Android 10, Termux, approximately 2 GB RAM
**Current status:** Host-side implementation and regression work completed for this execution; physical-device validation remains pending.

## Baseline and preservation

- Started on branch `main`, baseline commit `a5701ff` (`X server`). The tracked application and README had no modifications at task start. `Update.md`, `Maker.md`, `Error.md`, and `references/` were already untracked user content and were preserved/updated rather than discarded.
- The default workspace runtime paths (`data/`, `data/xserver.sqlite3`, `data/session_secret`, and `logs/`) did not exist at inspection. No production database or existing account was available in this checkout to back up or modify.
- Existing `x_server.py` behavior was inspected before edits and retained. The prior Git commit is the source rollback point; no new commit or stash was created.
- No reference source files were changed, moved, or copied into the runtime.

## Implemented

| Area | Actual change |
|---|---|
| Existing Flask/SQLite core | Preserved setup/login, catalog, details, playback, My List, progress, settings, and Minecraft pages. Added `XSERVER_DATA_DIR`/`XSERVER_LOG_DIR` path configuration while retaining existing default paths. |
| Authentication/security | Added CSRF-protected POST logout and password change with current-password verification and all-session revocation; bounded password lengths; safe same-origin login redirect validation; retained password hashing, login throttling, session lifetime, and first-run-only setup. Added response security headers and restrictive permissions for a newly created DB/session key. |
| Settings/schema | Added an additive `settings` table to persist the movie-directory choice when `XSERVER_MOVIE_DIR` is not explicitly set. Existing account/list/progress/metadata/login rows are preserved. |
| X Stream | Preserved local scanning, supported extensions, opaque path keys, chunked media delivery, Range responses, resume progress, and list behavior. Hardened long/malformed ranges and invalid JSON handling. |
| Diagnostics | Added an authenticated, read-only diagnostics page showing DB integrity, actual movie scan state, bind/configuration, and verified Minecraft process state. No terminal or arbitrary command route exists. |
| Minecraft | Requires `XSERVER_MC_JAR` to select an actual JAR directly inside `XSERVER_MC_DIR`; no auto-start/download. Reports a process only when PID, process start time, and command line match the recorded JAR. Serializes manager actions within the Flask process, limits heap configuration to 128–512 MiB, and uses verified SIGTERM for stop/restart. |
| Backup/restore | Added `scripts/data_tools.py` for SQLite online backup, integrity verification, and restore only into a new destination. Backup and restored DB use restrictive permissions; session key is included if present; no existing destination is overwritten. |
| Verification | Added isolated `unittest` coverage. Current result: 15 tests passed, including a real two-process restart preserving signed session, My List, and progress, plus temporary backup/restore. |

## Tests and checks run

| Command/check | Result |
|---|---|
| `python -m unittest discover -s tests -v` | **PASS** — 15 tests, 0 failures, last run 2026-10-15. |
| `python -m py_compile x_server.py scripts/data_tools.py tests/test_x_server.py` | **PASS**. |
| Real local server start/HTTP response/clean shutdown | **PASS** — Python 3.14.2, Linux x86_64 Codespaces; HTTP 200 first-run page and security headers; stopped with SIGINT (exit 0). |
| Process RSS after local startup and request | **41,916 kB** — Linux 6.8 Azure x86_64/glibc 2.39, Python 3.14.2, Flask 3.1.3. This is a single idle server process after one login-page request; no browser, video, Android, or Minecraft workload. |
| Backup/verify/restore CLI | **PASS** — create and integrity-check backup, restore to a new path, and verify preserved row/key using disposable database. |
| `git diff --check` | **PASS**. Final review confirmed the reference trees and runtime data paths were not modified/created. |
| OPPO A15 / Android 10 / Termux tests | **Not run** — target device unavailable. |

The automated tests use disposable temporary data and do not access a production database, run Minecraft, use TMDB, or access any reference repository.

## Current limitations and blockers

- **Device support:** OPPO A15/Android 10/Termux permissions, Python compatibility, codec playback, background stability, and RAM/battery use are not verified.
- **Minecraft:** Manager behavior is tested without starting a game server. A 512 MiB heap excludes JVM/native memory and is not a safe-use claim for a 2 GB phone.
- **Terminal:** Only diagnostics are implemented. No terminal emulator or shell backend is included.
- **Remote access:** No LAN, VPN, reverse proxy, Tor, or Onion Service configuration is implemented. Localhost remains the default.
- **Licensing:** Cine App source has no identified root `LICENSE`, `NOTICE`, or `COPYING` file; reuse/redistribution rights remain unresolved and no code was copied. The inspected Pterodactyl root license is MIT; it is not used by the app. Future assets still require file-level notice review.
- **Metadata:** TMDB is optional, external, and not tested; local library features do not depend on it.
- **Backup:** Temporary backup/verification/restore-to-new-location is covered by tests. No production backup exists because no workspace database was present. Production cutover/restore into a live data path is intentionally not automated.

## Modified/added files

- `x_server.py`
- `README.md`
- `Update.md`
- `Maker.md`
- `Error.md`
- `.gitignore`
- `requirements.txt`
- `scripts/data_tools.py`
- `tests/test_x_server.py`

Removed only the unused `references/xterm.js/` clone. Other reference repositories were preserved.

## Next steps

1. Run the OPPO A15 acceptance matrix in `Maker.md`; record actual Android/Termux versions, storage behavior, browser codec tests, RSS, and background reliability.
2. Keep the application bound to loopback. Design and test a private TLS/VPN boundary before any remote exposure.
3. Leave Minecraft stopped on the phone unless a separate device-specific memory/reliability assessment approves it.
4. Resolve Cine App reuse rights before considering source adoption.
