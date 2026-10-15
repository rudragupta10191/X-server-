# X Server Error Log

This file records implementation or verification errors reproduced with evidence. Deployment and license blockers are documented in `Update.md` and `Maker.md`.

## Error records

### ERR-001 — Dashboard health indicator referenced movie count before assignment

- **Date:** 2026-10-15
- **Component:** Dashboard / application health
- **Description:** The first regression run raised `UnboundLocalError` on `/dashboard` after adding the DB-health indicator.
- **Reproduction:** Create/log in to a temporary test account and request `/dashboard`.
- **Root cause:** `application_health(movie_count)` was called before `movie_count = len(movies)`.
- **Fix:** Compute the scanned movie count before calling the health helper.
- **Verification:** `python -m unittest discover -s tests -v` subsequently passed all 15 tests, including dashboard access; `python -m py_compile x_server.py scripts/data_tools.py tests/test_x_server.py` passed.
- **Status:** Verified

## Verification status and known unrun checks

| Check | Result |
|---|---|
| Regression suite | 15 tests passed on 2026-10-15. |
| Python syntax compilation | Passed for `x_server.py`, `scripts/data_tools.py`, and `tests/test_x_server.py`. |
| Backup/restore rehearsal | Passed in the test suite with a temporary DB and new restore location; no live DB was changed. |
| Backup CLI entrypoints | `backup`, `verify`, and `restore` passed against a temporary database, preserving a row and session-key file. |
| Restart persistence | Two real local Flask processes restarted against one disposable data directory; login session, My List, and saved progress persisted. |
| Local application process | Python 3.14.2/Linux x86_64 startup, HTTP 200, security headers, SIGINT shutdown passed; RSS after one request 41,916 kB. |
| OPPO A15 / Termux / Android storage / mobile browser | Not run; target device unavailable. |
| Real Minecraft server process | Not run; unsafe/unnecessary for current host validation. |
| Remote access / Tor | Not configured or tested. |

No other implementation errors are currently known. Absence of device testing is a limitation, not a Verified result.
