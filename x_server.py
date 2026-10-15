#!/usr/bin/env python3
"""
X Server / X Stream — single-file bootstrap application
Target: Android Termux / Python + Flask + SQLite

Install:
    python -m pip install -r requirements.txt
Optional TMDB metadata:
    python -m pip install requests
Run:
    python x_server.py

Open:
    http://127.0.0.1:5000

Optional online artwork:
    Set TMDB_API_KEY in the environment to enable TMDB movie metadata/posters.
    Without a key, the app remains fully usable with local files and fallback artwork.
"""
from __future__ import annotations

import os
import re
import json
import time
import hmac
import signal
import sqlite3
import secrets
import mimetypes
import subprocess
import logging
import threading
from pathlib import Path
from functools import wraps
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from flask import (
    Flask, request, redirect, url_for, session, abort, jsonify,
    render_template_string, Response, flash
)
from werkzeug.security import generate_password_hash, check_password_hash

APP_NAME = "X Server"
MEDIA_NAME = "X Stream"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("XSERVER_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
LOG_DIR = Path(os.environ.get("XSERVER_LOG_DIR", str(BASE_DIR / "logs"))).expanduser()
DB_PATH = DATA_DIR / "xserver.sqlite3"
DEFAULT_MOVIE_DIR = Path("/storage/emulated/0/Movies")
MOVIE_DIR_FROM_ENV = os.environ.get("XSERVER_MOVIE_DIR")
MOVIE_DIR = Path(MOVIE_DIR_FROM_ENV or str(DEFAULT_MOVIE_DIR)).expanduser()
MC_DIR = Path(os.environ.get("XSERVER_MC_DIR", str(Path.home() / "mc_server"))).expanduser()
MC_JAR_FROM_ENV = os.environ.get("XSERVER_MC_JAR", "").strip()
MC_MAX_MEMORY_MB_SETTING = os.environ.get("XSERVER_MC_MAX_MEMORY_MB", "512")
HOST = os.environ.get("XSERVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("XSERVER_PORT", "5000"))
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
ALLOWED_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
MAX_LOGIN_ATTEMPTS = 8
LOGIN_WINDOW_SECONDS = 300
SESSION_HOURS = 12

DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

app = Flask(__name__)
# Persist a random secret so sessions remain valid across normal restarts.
SECRET_PATH = DATA_DIR / "session_secret"
if SECRET_PATH.exists():
    SECRET_KEY = SECRET_PATH.read_text(encoding="utf-8").strip()
    if not SECRET_KEY:
        raise RuntimeError(f"Session secret file is empty: {SECRET_PATH}")
else:
    SECRET_KEY = secrets.token_hex(32)
    SECRET_PATH.write_text(SECRET_KEY, encoding="utf-8")
SECRET_PATH.chmod(0o600)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(os.environ.get("XSERVER_HTTPS", "0") == "1"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)
app.logger.setLevel(logging.INFO)
MC_START_LOCK = threading.Lock()

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

def init_db():
    database_existed = DB_PATH.exists()
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS my_list (
            user_id INTEGER NOT NULL DEFAULT 1,
            movie_key TEXT NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY (user_id, movie_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS progress (
            user_id INTEGER NOT NULL DEFAULT 1,
            movie_key TEXT NOT NULL,
            seconds REAL NOT NULL DEFAULT 0,
            duration REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, movie_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS metadata (
            movie_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            overview TEXT DEFAULT '',
            poster_path TEXT DEFAULT '',
            backdrop_path TEXT DEFAULT '',
            genres TEXT DEFAULT '',
            year TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            ip TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0,
            window_start REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        if MOVIE_DIR_FROM_ENV is None:
            row = con.execute("SELECT value FROM settings WHERE key='movie_dir'").fetchone()
            if row:
                global MOVIE_DIR
                MOVIE_DIR = Path(row["value"]).expanduser()
    if not database_existed:
        DB_PATH.chmod(0o600)
init_db()

def has_user():
    with db() as con:
        return con.execute("SELECT 1 FROM users WHERE id=1").fetchone() is not None

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]

def verify_csrf():
    expected = session.get("_csrf", "")
    supplied = request.form.get("_csrf", "") or request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, description="CSRF validation failed. Refresh the page and try again.")

@app.context_processor
def inject_globals():
    return {
        "csrf": csrf_token,
        "app_name": APP_NAME,
        "media_name": MEDIA_NAME,
        "tmdb_enabled": bool(TMDB_API_KEY),
    }

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        with db() as con:
            row = con.execute("SELECT value FROM settings WHERE key='session_version'").fetchone()
        current_version = int(row["value"]) if row else 0
        session_version = session.get("session_version")
        if session_version is None and current_version == 0:
            session["session_version"] = 0
        elif session_version != current_version:
            session.clear()
            return redirect(url_for("login", next=request.path))
        session.permanent = True
        return fn(*args, **kwargs)
    return wrapped

def safe_next_target(target):
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return url_for("dashboard")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or any(ord(char) < 32 for char in target):
        return url_for("dashboard")
    return target

@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' https://image.tmdb.org data:; "
        "media-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'",
    )
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response

def login_throttled(ip):
    now = time.time()
    with db() as con:
        row = con.execute("SELECT count, window_start FROM login_attempts WHERE ip=?", (ip,)).fetchone()
        if not row or now - row["window_start"] > LOGIN_WINDOW_SECONDS:
            con.execute("INSERT OR REPLACE INTO login_attempts(ip,count,window_start) VALUES(?,?,?)", (ip, 0, now))
            return False
        return row["count"] >= MAX_LOGIN_ATTEMPTS

def record_login_failure(ip):
    now = time.time()
    with db() as con:
        row = con.execute("SELECT count, window_start FROM login_attempts WHERE ip=?", (ip,)).fetchone()
        if not row or now - row["window_start"] > LOGIN_WINDOW_SECONDS:
            con.execute("INSERT OR REPLACE INTO login_attempts(ip,count,window_start) VALUES(?,?,?)", (ip, 1, now))
        else:
            con.execute("UPDATE login_attempts SET count=count+1 WHERE ip=?", (ip,))

def clear_login_failures(ip):
    with db() as con:
        con.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))

def movie_key(path: Path) -> str:
    # Opaque key prevents clients from supplying arbitrary filesystem paths.
    import hashlib
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:32]

def scan_movies():
    items = []
    try:
        root = MOVIE_DIR.resolve()
        if not root.is_dir():
            return items, f"Movie folder not found: {MOVIE_DIR}"
        for p in root.iterdir():
            try:
                if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS:
                    st = p.stat()
                    items.append({
                        "key": movie_key(p), "title": p.stem,
                        "path": str(p.resolve()), "size": st.st_size,
                        "modified": st.st_mtime, "extension": p.suffix.lower(),
                    })
            except (OSError, PermissionError):
                continue
        items.sort(key=lambda x: x["modified"], reverse=True)
        return items, None
    except (OSError, PermissionError) as exc:
        return [], f"Cannot access movie folder: {exc}"

def application_health(movie_count=None):
    database_ok = False
    try:
        with db() as con:
            result = con.execute("PRAGMA quick_check").fetchone()
            database_ok = bool(result and result[0] == "ok")
    except sqlite3.Error as exc:
        app.logger.error("Database health check failed: %s", type(exc).__name__)
    return {
        "database_ok": database_ok,
        "database_path": str(DB_PATH),
        "movie_count": movie_count,
        "movie_directory_accessible": MOVIE_DIR.is_dir() and os.access(MOVIE_DIR, os.R_OK),
    }

def safe_movie_by_key(key):
    if not re.fullmatch(r"[a-f0-9]{32}", key or ""):
        return None
    movies, _ = scan_movies()
    for item in movies:
        if item["key"] == key:
            p = Path(item["path"]).resolve()
            try:
                if p.parent == MOVIE_DIR.resolve() and p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS:
                    return item
            except OSError:
                return None
    return None

def metadata_for(item):
    with db() as con:
        row = con.execute("SELECT * FROM metadata WHERE movie_key=?", (item["key"],)).fetchone()
    if row:
        return dict(row)
    title = item["title"]
    return {
        "movie_key": item["key"], "title": title, "overview": "",
        "poster_path": "", "backdrop_path": "", "genres": "",
        "year": "", "updated_at": ""
    }

def tmdb_lookup(item):
    """Best-effort metadata lookup. No API key means no external lookup."""
    if not TMDB_API_KEY:
        return metadata_for(item)
    try:
        import requests
    except ImportError:
        app.logger.warning("Optional metadata lookup is enabled but requests is not installed")
        return metadata_for(item)
    try:
        title = re.sub(r"[\._]+", " ", item["title"]).strip()
        response = requests.get(
            "https://api.themoviedb.org/3/search/movie",
            params={"api_key": TMDB_API_KEY, "query": title},
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return metadata_for(item)
        match = results[0]
        data = {
            "movie_key": item["key"],
            "title": match.get("title") or item["title"],
            "overview": match.get("overview") or "",
            "poster_path": match.get("poster_path") or "",
            "backdrop_path": match.get("backdrop_path") or "",
            "genres": "",
            "year": (match.get("release_date") or "")[:4],
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with db() as con:
            con.execute("""INSERT OR REPLACE INTO metadata
                (movie_key,title,overview,poster_path,backdrop_path,genres,year,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (data["movie_key"], data["title"], data["overview"], data["poster_path"],
                 data["backdrop_path"], data["genres"], data["year"], data["updated_at"]))
        return data
    except (requests.RequestException, ValueError, sqlite3.Error) as exc:
        app.logger.warning("Optional metadata lookup failed: %s", type(exc).__name__)
        return metadata_for(item)

def poster_url(meta, backdrop=False):
    path = meta.get("backdrop_path" if backdrop else "poster_path", "")
    if path and path.startswith("/"):
        return "https://image.tmdb.org/t/p/" + ("w1280" if backdrop else "w500") + path
    return ""

def fmt_size(n):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"

BASE_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#08090d">
<title>{{ title }} · X Server</title>
<style>
:root{color-scheme:dark;--bg:#08090d;--panel:#11131a;--panel2:#191c26;--text:#f4f5f8;--muted:#a2a7b4;--accent:#e50914;--line:#292d38}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:inherit;text-decoration:none}button,input,select{font:inherit}button,.btn{border:0;border-radius:9px;padding:11px 15px;background:#f4f4f5;color:#101116;font-weight:700;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:7px}button.secondary,.btn.secondary{background:#292d38;color:#fff}button.danger,.btn.danger{background:#9e1720;color:#fff}button:disabled{opacity:.5;cursor:not-allowed}input,select{width:100%;padding:12px;border:1px solid var(--line);border-radius:9px;background:#10121a;color:var(--text)}label{display:block;margin:12px 0 6px;color:#d5d8e0;font-size:.92rem}.topbar{height:64px;display:flex;align-items:center;justify-content:space-between;padding:0 5%;position:sticky;top:0;background:#08090deF;backdrop-filter:blur(15px);z-index:20;border-bottom:1px solid #ffffff0a}.brand{font-weight:900;letter-spacing:.5px;font-size:1.2rem}.brand b{color:var(--accent)}nav{display:flex;align-items:center;gap:18px;color:#d7d8de;font-size:.92rem}.wrap{width:min(1200px,92%);margin:28px auto 70px}.muted{color:var(--muted)}.notice{padding:12px 14px;border:1px solid var(--line);background:var(--panel);border-radius:10px;margin:12px 0}.notice.error{border-color:#74303a;color:#ffb6bd}.notice.success{border-color:#276749;color:#b7f7d1}.hero{min-height:390px;border-radius:18px;overflow:hidden;display:flex;align-items:end;padding:clamp(22px,5vw,54px);background:linear-gradient(90deg,#08090df5 0%,#08090db8 50%,#08090d22 100%),linear-gradient(0deg,#08090d 0%,transparent 55%),var(--hero-bg,linear-gradient(120deg,#252936,#10121a));background-size:cover;background-position:center;position:relative}.hero-copy{max-width:610px;position:relative;z-index:1}.eyebrow{text-transform:uppercase;letter-spacing:2px;color:#ff5963;font-size:.76rem;font-weight:800}.hero h1{font-size:clamp(2rem,6vw,4.1rem);line-height:1.02;margin:12px 0}.hero p{line-height:1.6;color:#d3d5dc;max-width:550px}.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}.section{margin-top:32px}.section-head{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:14px}.section h2{font-size:1.25rem;margin:0}.grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:15px}.movie{min-width:0}.poster{aspect-ratio:2/3;background:linear-gradient(140deg,#252936,#10121a);border-radius:11px;overflow:hidden;position:relative;border:1px solid #ffffff0c}.poster img{width:100%;height:100%;object-fit:cover;display:block}.poster-fallback{height:100%;display:flex;align-items:center;justify-content:center;text-align:center;padding:12px;color:#9da3b3;font-weight:800;overflow-wrap:anywhere;background:radial-gradient(circle at 25% 10%,#353b4d,#12141c 65%)}.movie-title{font-weight:700;margin:8px 0 4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.movie-sub{font-size:.8rem;color:var(--muted)}.progress{height:4px;background:#343744;border-radius:4px;margin-top:7px;overflow:hidden}.progress span{height:100%;display:block;background:var(--accent)}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0}.toolbar input{flex:2;min-width:170px}.toolbar select{flex:1;min-width:140px}.card{background:var(--panel);border:1px solid var(--line);padding:22px;border-radius:14px}.narrow{width:min(460px,92%);margin:7vh auto}.form-actions{margin-top:18px}.detail{display:grid;grid-template-columns:minmax(180px,280px) 1fr;gap:28px;align-items:start}.detail-art{width:100%;border-radius:14px;overflow:hidden;aspect-ratio:2/3;background:#181b25}.detail-art img{width:100%;height:100%;object-fit:cover}.detail h1{font-size:clamp(1.8rem,5vw,3.5rem);margin:0 0 12px}.player{width:100%;background:#000;border-radius:12px;max-height:70vh}.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.stat{padding:15px;border-radius:11px;background:var(--panel2)}.stat strong{display:block;font-size:1.2rem;margin-bottom:5px}.table{width:100%;border-collapse:collapse}.table td,.table th{text-align:left;padding:12px;border-bottom:1px solid var(--line)}.small{font-size:.82rem}.footer{padding:22px 5%;color:#747b8b;border-top:1px solid #ffffff0a;font-size:.82rem}.inline{display:inline}.inline button{padding:8px 10px}.empty{padding:35px 18px;text-align:center;border:1px dashed #343947;border-radius:12px;color:var(--muted)}.iconbtn{background:#252936;color:white;padding:8px 11px}.notice a{text-decoration:underline}
@media(max-width:900px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}.hero{min-height:350px}}@media(max-width:620px){.topbar{padding:0 4%;height:58px}nav{gap:10px;font-size:.82rem}nav .optional{display:none}.wrap{width:92%;margin-top:18px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.hero{min-height:390px;padding:23px}.hero p{font-size:.92rem}.detail{grid-template-columns:115px 1fr;gap:15px}.stats{grid-template-columns:1fr}.table{font-size:.84rem}.table td,.table th{padding:8px 5px}}
</style>
</head><body>
<header class="topbar">
<a class="brand" href="{{ url_for('dashboard') if logged_in else url_for('login') }}"><b>X</b> SERVER</a>
{% if logged_in %}<nav><a href="{{ url_for('dashboard') }}">Home</a><a href="{{ url_for('movies_page') }}">X Stream</a><a class="optional" href="{{ url_for('my_list_page') }}">My List</a><a class="optional" href="{{ url_for('minecraft_page') }}">Minecraft</a><a class="optional" href="{{ url_for('diagnostics_page') }}">Diagnostics</a><a class="optional" href="{{ url_for('settings_page') }}">Settings</a><a class="optional" href="{{ url_for('change_password') }}">Password</a><form class="inline" method="post" action="{{ url_for('logout') }}"><input type="hidden" name="_csrf" value="{{ csrf() }}"><button class="secondary" type="submit">Logout</button></form></nav>{% endif %}
</header>
<main class="wrap">
{% with messages = get_flashed_messages(with_categories=true) %}{% for category,message in messages %}<div class="notice {{ category }}">{{ message }}</div>{% endfor %}{% endwith %}
{{ body|safe }}
</main><footer class="footer">X Server · Personal media, running locally. Keep it private; default access is localhost only.</footer>
<script>
document.querySelectorAll('form[data-confirm]').forEach(f=>f.addEventListener('submit',e=>{if(!confirm(f.dataset.confirm))e.preventDefault()}));
</script>
</body></html>
"""

def page(title, body, **kwargs):
    return render_template_string(BASE_HTML, title=title, body=render_template_string(body, **kwargs),
                                  logged_in=bool(session.get("user_id")))

AUTH_FORM = r"""
<div class="narrow card">
<div class="eyebrow">Private access</div>
<h1 style="margin:8px 0 5px">{{ heading }}</h1>
<p class="muted">{{ subtitle }}</p>
<form method="post" autocomplete="on">
<input type="hidden" name="_csrf" value="{{ csrf() }}">
<label for="username">User ID</label>
<input id="username" name="username" autocomplete="username" required minlength="3" maxlength="64" pattern="[A-Za-z0-9_.@-]+" value="{{ username or '' }}" placeholder="Choose your user ID">
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="{{ 'new-password' if setup else 'current-password' }}" required minlength="{{ 10 if setup else 1 }}" maxlength="256" placeholder="{{ 'At least 10 characters' if setup else 'Enter your password' }}">
{% if setup %}<label for="confirm">Confirm password</label><input id="confirm" name="confirm" type="password" autocomplete="new-password" required minlength="10" maxlength="256" placeholder="Enter password again"><p class="muted small">Your ID and password hash will be saved in the local SQLite database. Setup appears only once; restarting will not erase your account.</p>{% endif %}
<div class="form-actions"><button type="submit" style="width:100%">{{ 'Create private account' if setup else 'Sign in' }}</button></div>
</form>
</div>
"""

@app.route("/", methods=["GET"])
def index():
    if not has_user():
        return redirect(url_for("setup"))
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if has_user():
        return redirect(url_for("login"))
    if request.method == "POST":
        verify_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", username):
            flash("User ID must be 3–64 characters: letters, numbers, dot, underscore, @ or hyphen.", "error")
        elif len(password) < 10:
            flash("Use a password with at least 10 characters.", "error")
        elif len(password) > 256:
            flash("Password cannot exceed 256 characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        else:
            try:
                with db() as con:
                    # INSERT OR IGNORE ensures setup can never overwrite an existing account.
                    con.execute("INSERT OR IGNORE INTO users(id,username,password_hash,created_at) VALUES(1,?,?,?)",
                                (username, generate_password_hash(password), datetime.now(timezone.utc).isoformat(timespec="seconds")))
                if has_user():
                    flash("Private account created. Sign in with the same User ID and password.", "success")
                    return redirect(url_for("login"))
            except sqlite3.Error:
                flash("Could not create account. Check storage permissions and try again.", "error")
    return page("First-time setup", AUTH_FORM, heading="Create your account",
                subtitle="First launch only · Choose your permanent User ID and password.",
                setup=True, username=request.form.get("username", ""))

@app.route("/login", methods=["GET", "POST"])
def login():
    if not has_user():
        return redirect(url_for("setup"))
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    ip = request.remote_addr or "unknown"
    if request.method == "POST":
        verify_csrf()
        if login_throttled(ip):
            flash("Too many attempts. Wait five minutes before trying again.", "error")
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if len(password) > 256:
                record_login_failure(ip)
                flash("Incorrect User ID or password.", "error")
                return page("Sign in", AUTH_FORM, heading="Welcome back",
                            subtitle="Sign in to your private X Server.", setup=False,
                            username=username)
            with db() as con:
                user = con.execute("SELECT * FROM users WHERE id=1 AND username=?", (username,)).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                clear_login_failures(ip)
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                with db() as con:
                    row = con.execute("SELECT value FROM settings WHERE key='session_version'").fetchone()
                session["session_version"] = int(row["value"]) if row else 0
                session.permanent = True
                csrf_token()
                return redirect(safe_next_target(request.args.get("next", "")))
            record_login_failure(ip)
            flash("Incorrect User ID or password.", "error")
    return page("Sign in", AUTH_FORM, heading="Welcome back",
                subtitle="Sign in to your private X Server.", setup=False,
                username=request.form.get("username", ""))

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    verify_csrf()
    session.clear()
    return redirect(url_for("login"))

@app.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        verify_csrf()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm", "")
        with db() as con:
            user = con.execute(
                "SELECT password_hash FROM users WHERE id=?",
                (session.get("user_id"),),
            ).fetchone()
            if not user or not check_password_hash(user["password_hash"], current_password):
                flash("Current password is incorrect.", "error")
            elif len(new_password) < 10 or len(new_password) > 256:
                flash("Use a new password between 10 and 256 characters.", "error")
            elif new_password != confirm:
                flash("New passwords do not match.", "error")
            else:
                con.execute(
                    "UPDATE users SET password_hash=? WHERE id=?",
                    (generate_password_hash(new_password), session["user_id"]),
                )
                row = con.execute("SELECT value FROM settings WHERE key='session_version'").fetchone()
                version = int(row["value"]) + 1 if row else 1
                con.execute(
                    "INSERT INTO settings(key,value) VALUES('session_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(version),),
                )
                session.clear()
                flash("Password changed. Sign in again with your new password.", "success")
                return redirect(url_for("login"))
    body = r"""
    <div class="narrow card"><div class="eyebrow">Account security</div><h1>Change password</h1>
    <form method="post"><input type="hidden" name="_csrf" value="{{csrf()}}">
    <label for="current_password">Current password</label><input id="current_password" name="current_password" type="password" autocomplete="current-password" required>
    <label for="new_password">New password</label><input id="new_password" name="new_password" type="password" autocomplete="new-password" minlength="10" maxlength="256" required>
    <label for="confirm">Confirm new password</label><input id="confirm" name="confirm" type="password" autocomplete="new-password" minlength="10" maxlength="256" required>
    <div class="form-actions"><button type="submit">Update password</button></div></form></div>
    """
    return page("Change password", body)

@app.route("/dashboard")
@login_required
def dashboard():
    movies, scan_error = scan_movies()
    resume = []
    with db() as con:
        rows = con.execute("SELECT movie_key,seconds,duration FROM progress WHERE user_id=1 AND seconds>10 ORDER BY updated_at DESC LIMIT 8").fetchall()
    for row in rows:
        item = safe_movie_by_key(row["movie_key"])
        if item:
            item["progress_seconds"] = row["seconds"]
            item["duration"] = row["duration"]
            item["percent"] = min(100, int(row["seconds"] / row["duration"] * 100)) if row["duration"] else 0
            item["meta"] = metadata_for(item)
            resume.append(item)
    recent = movies[:10]
    featured = (recent[0] if recent else None)
    featured_meta = tmdb_lookup(featured) if featured else {}
    movie_count = len(movies)
    health = application_health(movie_count)
    minecraft = minecraft_status()
    body = r"""
    {% if featured %}
    <section class="hero" style="--hero-bg:url('{{ poster_url(featured_meta, true) or poster_url(featured_meta) }}')">
      <div class="hero-copy"><div class="eyebrow">Your personal cinema</div><h1>{{ featured_meta.get('title') or featured.title }}</h1>
      <p>{{ featured_meta.get('overview') or 'Your local movie collection, beautifully organized. Pick up where you left off or explore your library.' }}</p>
      <div class="actions"><a class="btn" href="{{ url_for('movie_detail', key=featured.key) }}">▶ Play / Details</a><a class="btn secondary" href="{{ url_for('movies_page') }}">Explore X Stream</a></div>
      </div>
    </section>
    {% else %}
    <section class="hero"><div class="hero-copy"><div class="eyebrow">X Stream</div><h1>Your cinema.<br>Your rules.</h1><p>Build your personal library by placing videos in your Movies folder. X Stream will discover supported files automatically.</p><div class="actions"><a class="btn" href="{{ url_for('movies_page') }}">Open X Stream</a></div></div></section>
    {% endif %}
    {% if scan_error %}<div class="notice error">{{ scan_error }}. In Termux, run <code>termux-setup-storage</code> and allow storage access, then check the folder path.</div>{% endif %}
    <section class="section"><div class="stats"><div class="stat"><strong>{{ movie_count }}</strong><span class="muted">Local movies</span></div><div class="stat"><strong>{{ resume|length }}</strong><span class="muted">Continue watching</span></div><div class="stat"><strong>{{ 'Healthy' if health.database_ok else 'Check failed' }}</strong><span class="muted">Application database</span></div><div class="stat"><strong>{{ 'Running' if minecraft.running else 'Stopped' }}</strong><span class="muted">Managed Minecraft process</span></div></div></section>
    {% if resume %}<section class="section"><div class="section-head"><h2>Continue Watching</h2><a class="muted" href="{{ url_for('movies_page') }}">View library →</a></div><div class="grid">{% for m in resume %}<a class="movie" href="{{ url_for('movie_detail',key=m.key) }}"><div class="poster">{% set p=poster_url(m.meta) %}{% if p %}<img loading="lazy" src="{{ p }}" alt="Artwork for {{ m.title }}">{% else %}<div class="poster-fallback">{{ m.title }}</div>{% endif %}</div><div class="movie-title">{{ m.title }}</div><div class="movie-sub">Resume · {{ m.percent }}%</div><div class="progress"><span style="width:{{m.percent}}%"></span></div></a>{% endfor %}</div></section>{% endif %}
    <section class="section"><div class="section-head"><h2>Recently Added</h2><a class="muted" href="{{ url_for('movies_page') }}">See all →</a></div>
    {% if recent %}<div class="grid">{% for m in recent[:5] %}<a class="movie" href="{{ url_for('movie_detail',key=m.key) }}"><div class="poster">{% set meta=metadata_for(m) %}{% set p=poster_url(meta) %}{% if p %}<img loading="lazy" src="{{ p }}" alt="Artwork for {{m.title}}">{% else %}<div class="poster-fallback">{{m.title}}</div>{% endif %}</div><div class="movie-title">{{m.title}}</div><div class="movie-sub">{{m.extension }} · {{fmt_size(m.size)}}</div></a>{% endfor %}</div>{% else %}<div class="empty">No movies found yet.<br><span class="small">Expected folder: {{ movie_dir }}</span></div>{% endif %}</section>
    <section class="section"><div class="section-head"><h2>Services</h2></div><div class="actions"><a class="btn secondary" href="{{ url_for('minecraft_page') }}">Minecraft Manager</a><a class="btn secondary" href="{{ url_for('diagnostics_page') }}">Read-only Diagnostics</a><a class="btn secondary" href="{{ url_for('settings_page') }}">Settings</a></div></section>
    """
    return page("Dashboard", body, featured=featured, featured_meta=featured_meta, movie_count=movie_count,
                recent=recent, resume=resume, scan_error=scan_error, movie_dir=str(MOVIE_DIR),
                poster_url=poster_url, metadata_for=metadata_for, fmt_size=fmt_size, tmdb_enabled=bool(TMDB_API_KEY),
                health=health, minecraft=minecraft)

@app.route("/movies")
@login_required
def movies_page():
    movies, scan_error = scan_movies()
    query = request.args.get("q", "").strip().lower()[:100]
    sort = request.args.get("sort", "recent")
    if query:
        movies = [m for m in movies if query in m["title"].lower()]
    if sort == "title":
        movies.sort(key=lambda x: x["title"].lower())
    elif sort == "oldest":
        movies.sort(key=lambda x: x["modified"])
    else:
        movies.sort(key=lambda x: x["modified"], reverse=True)
    with db() as con:
        listed = {r["movie_key"] for r in con.execute("SELECT movie_key FROM my_list WHERE user_id=1")}
    body = r"""
    <div class="section-head"><div><div class="eyebrow">Personal media library</div><h1 style="margin:7px 0">X Stream</h1><div class="muted">{{ movies|length }} movies</div></div></div>
    {% if scan_error %}<div class="notice error">{{ scan_error }}. Grant Termux storage permission and verify the movie directory in Settings.</div>{% endif %}
    <form class="toolbar" method="get"><input name="q" value="{{query}}" placeholder="Search your movies…" aria-label="Search movies"><select name="sort" aria-label="Sort movies"><option value="recent" {{'selected' if sort=='recent'}}>Recently added</option><option value="title" {{'selected' if sort=='title'}}>Title A–Z</option><option value="oldest" {{'selected' if sort=='oldest'}}>Oldest first</option></select><button type="submit">Search</button></form>
    {% if movies %}<div class="grid">{% for m in movies %}{% set meta=metadata_for(m) %}{% set p=poster_url(meta) %}<div class="movie"><a href="{{url_for('movie_detail',key=m.key)}}"><div class="poster">{% if p %}<img loading="lazy" src="{{p}}" alt="Artwork for {{m.title}}">{% else %}<div class="poster-fallback">{{m.title}}</div>{% endif %}</div><div class="movie-title">{{m.title}}</div><div class="movie-sub">{{m.extension}} · {{fmt_size(m.size)}}</div></a><div style="margin-top:8px"><form method="post" action="{{url_for('toggle_list',key=m.key)}}"><input type="hidden" name="_csrf" value="{{csrf()}}"><button class="iconbtn" type="submit">{{'✓ In My List' if m.key in listed else '+ My List'}}</button></form></div></div>{% endfor %}</div>{% else %}<div class="empty">No matching movies found.</div>{% endif %}
    """
    return page("X Stream", body, movies=movies, scan_error=scan_error, query=query, sort=sort,
                listed=listed, metadata_for=metadata_for, poster_url=poster_url, fmt_size=fmt_size)

@app.route("/movie/<key>")
@login_required
def movie_detail(key):
    item = safe_movie_by_key(key)
    if not item:
        abort(404, description="Movie not found in the configured media folder.")
    meta = tmdb_lookup(item)
    with db() as con:
        row = con.execute("SELECT seconds,duration FROM progress WHERE user_id=1 AND movie_key=?", (key,)).fetchone()
        listed = con.execute("SELECT 1 FROM my_list WHERE user_id=1 AND movie_key=?", (key,)).fetchone() is not None
    seconds = row["seconds"] if row else 0
    percent = min(100, int(seconds / row["duration"] * 100)) if row and row["duration"] else 0
    body = r"""
    <div class="detail"><div class="detail-art">{% set p=poster_url(meta) %}{% if p %}<img src="{{p}}" alt="Artwork for {{meta.title}}">{% else %}<div class="poster-fallback">{{item.title}}</div>{% endif %}</div><div><div class="eyebrow">X Stream · Movie details</div><h1>{{meta.title or item.title}}</h1><div class="muted">{{meta.year or 'Local file'}} · {{item.extension}} · {{fmt_size(item.size)}}</div>{% if meta.overview %}<p style="line-height:1.7;color:#d0d3dc">{{meta.overview}}</p>{% else %}<p class="muted">Metadata has not been found. This is the actual local filename; no details are invented.</p>{% endif %}<div class="actions"><a class="btn" href="{{url_for('player_page',key=item.key)}}">▶ {{'Resume' if seconds>10 else 'Play movie'}}</a><form method="post" action="{{url_for('toggle_list',key=item.key)}}"><input type="hidden" name="_csrf" value="{{csrf()}}"><button class="secondary" type="submit">{{'✓ Remove from My List' if listed else '+ Add to My List'}}</button></form></div>{% if seconds>10 %}<p class="muted small">Saved progress: {{percent}}%</p>{% endif %}<p class="muted small">File: {{item.path}}</p></div></div>
    """
    return page(meta.get("title") or item["title"], body, item=item, meta=meta, seconds=seconds,
                percent=percent, listed=listed, poster_url=poster_url, fmt_size=fmt_size)

@app.route("/my-list")
@login_required
def my_list_page():
    with db() as con:
        rows = con.execute("SELECT movie_key FROM my_list WHERE user_id=1 ORDER BY added_at DESC").fetchall()
    movies = []
    for row in rows:
        item = safe_movie_by_key(row["movie_key"])
        if item:
            item["meta"] = metadata_for(item)
            movies.append(item)
    body = r"""
    <div class="eyebrow">Saved for later</div><h1>My List</h1>
    {% if movies %}<div class="grid">{% for m in movies %}<a class="movie" href="{{url_for('movie_detail',key=m.key)}}"><div class="poster">{% set p=poster_url(m.meta) %}{% if p %}<img loading="lazy" src="{{p}}" alt="Artwork for {{m.title}}">{% else %}<div class="poster-fallback">{{m.title}}</div>{% endif %}</div><div class="movie-title">{{m.title}}</div><div class="movie-sub">{{m.extension}} · {{fmt_size(m.size)}}</div></a>{% endfor %}</div>{% else %}<div class="empty">Your list is empty. Add movies from X Stream.</div>{% endif %}
    """
    return page("My List", body, movies=movies, poster_url=poster_url, fmt_size=fmt_size)

@app.route("/list/toggle/<key>", methods=["POST"])
@login_required
def toggle_list(key):
    verify_csrf()
    item = safe_movie_by_key(key)
    if not item:
        abort(404)
    with db() as con:
        row = con.execute("SELECT 1 FROM my_list WHERE user_id=1 AND movie_key=?", (key,)).fetchone()
        if row:
            con.execute("DELETE FROM my_list WHERE user_id=1 AND movie_key=?", (key,))
            flash("Removed from My List.", "success")
        else:
            con.execute("INSERT OR IGNORE INTO my_list(user_id,movie_key,added_at) VALUES(1,?,?)",
                        (key, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            flash("Added to My List.", "success")
    return redirect(request.referrer if request.referrer and request.referrer.startswith("/") else url_for("movies_page"))

@app.route("/player/<key>")
@login_required
def player_page(key):
    item = safe_movie_by_key(key)
    if not item:
        abort(404)
    with db() as con:
        row = con.execute("SELECT seconds FROM progress WHERE user_id=1 AND movie_key=?", (key,)).fetchone()
    start = float(row["seconds"]) if row else 0
    body = r"""
    <div class="section-head"><div><div class="eyebrow">Now playing</div><h1>{{item.title}}</h1></div><a class="btn secondary" href="{{url_for('movie_detail',key=item.key)}}">← Details</a></div>
    <video id="video" class="player" controls playsinline preload="metadata" src="{{url_for('video_file',key=item.key)}}"></video>
    <div class="notice" id="player-status">Progress saves automatically while playing.</div>
    <p class="muted small">If playback does not work, your browser may not support this file's codec. MKV/AVI container support varies by browser.</p>
    <script>
    (()=>{const v=document.getElementById('video'),s=document.getElementById('player-status');let saved={{start|tojson}},last=0;
      v.addEventListener('loadedmetadata',()=>{if(saved>5&&saved<v.duration-3){v.currentTime=saved;s.textContent='Resumed from your saved position.';}});
      async function save(){if(!Number.isFinite(v.currentTime))return;const now=Date.now();if(now-last<4000)return;last=now;try{const r=await fetch('{{url_for("save_progress",key=item.key)}}',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':{{csrf()|tojson}}},body:JSON.stringify({seconds:v.currentTime,duration:Number.isFinite(v.duration)?v.duration:0})});if(!r.ok)throw new Error();s.textContent='Playback progress saved.';}catch(e){s.textContent='Progress save failed; playback can continue.';}}
      v.addEventListener('timeupdate',save);v.addEventListener('pause',()=>{last=0;save()});v.addEventListener('ended',()=>{saved=0;fetch('{{url_for("save_progress",key=item.key)}}',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':{{csrf()|tojson}}},body:JSON.stringify({seconds:0,duration:Number.isFinite(v.duration)?v.duration:0})}).catch(()=>{});});
      v.addEventListener('error',()=>{s.textContent='Could not play this file. Check the file and browser codec support.'});
    })();
    </script>
    """
    return page("Player", body, item=item, start=start)

@app.route("/video/<key>")
@login_required
def video_file(key):
    item = safe_movie_by_key(key)
    if not item:
        abort(404)
    path = Path(item["path"])
    try:
        size = path.stat().st_size
        if not path.is_file():
            abort(404)
    except OSError:
        abort(404)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    range_header = request.headers.get("Range")
    if not range_header:
        # Stream file in chunks; never read the whole file into RAM.
        def generate():
            with path.open("rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        response = Response(generate(), status=200, mimetype=mime, direct_passthrough=True)
        response.headers["Content-Length"] = str(size)
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match or "," in range_header:
        return Response(status=416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
    first, last = match.groups()
    if (first == "" and last == "") or len(first) > 20 or len(last) > 20:
        return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
    try:
        if first == "":
            suffix = int(last)
            if suffix <= 0:
                return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
    except ValueError:
        return Response(status=416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
    if start >= size or start < 0 or end < start:
        return Response(status=416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
    end = min(end, size - 1)
    length = end - start + 1
    def generate_range():
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
    response = Response(generate_range(), status=206, mimetype=mime, direct_passthrough=True)
    response.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    response.headers["Content-Length"] = str(length)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

@app.route("/api/progress/<key>", methods=["POST"])
@login_required
def save_progress(key):
    item = safe_movie_by_key(key)
    if not item:
        abort(404)
    token = session.get("_csrf", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not token or not supplied or not hmac.compare_digest(token, supplied):
        return jsonify(error="CSRF validation failed"), 400
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Progress must be a JSON object"), 400
    try:
        seconds = float(data.get("seconds", 0))
        duration = float(data.get("duration", 0))
        if not (0 <= seconds < 10**9 and 0 <= duration < 10**9):
            raise ValueError
        if duration and seconds > duration:
            seconds = duration
    except (TypeError, ValueError):
        return jsonify(error="Invalid progress values"), 400
    with db() as con:
        con.execute("""INSERT INTO progress(user_id,movie_key,seconds,duration,updated_at)
            VALUES(1,?,?,?,?) ON CONFLICT(user_id,movie_key) DO UPDATE SET
            seconds=excluded.seconds,duration=excluded.duration,updated_at=excluded.updated_at""",
            (key, seconds, duration, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return jsonify(ok=True)

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    global MOVIE_DIR
    if request.method == "POST":
        verify_csrf()
        candidate = Path(request.form.get("movie_dir", "")).expanduser()
        if not candidate.is_absolute():
            flash("Movie directory must be an absolute path.", "error")
        else:
            MOVIE_DIR = candidate
            if MOVIE_DIR_FROM_ENV is None:
                with db() as con:
                    con.execute(
                        "INSERT INTO settings(key,value) VALUES('movie_dir',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(candidate),),
                    )
            flash("Movie directory updated. The setting is saved in SQLite unless XSERVER_MOVIE_DIR overrides it.", "success")
        return redirect(url_for("settings_page"))
    movies, err = scan_movies()
    health = application_health(len(movies))
    body = r"""
    <div class="eyebrow">Configuration</div><h1>Settings</h1>
    <div class="card"><h2>Media directory</h2><p class="muted">Current folder: {{movie_dir}}</p><form method="post"><input type="hidden" name="_csrf" value="{{csrf()}}"><label for="movie_dir">Absolute movie folder path</label><input id="movie_dir" name="movie_dir" value="{{movie_dir}}" required><div class="form-actions"><button type="submit">Update folder</button></div></form>
    <p class="muted small">Default Android path: /storage/emulated/0/Movies. In Termux, run <code>termux-setup-storage</code> and grant the permission. For a permanent custom path, set the environment variable XSERVER_MOVIE_DIR before launching.</p></div>
    <section class="section card"><h2>System status</h2><p>Database integrity: <strong>{{'OK' if health.database_ok else 'Check failed'}}</strong></p><p>Movie folder: <strong>{{'Accessible' if not err else 'Unavailable'}}</strong></p><p>Detected movies: <strong>{{movies|length}}</strong></p><p>TMDB artwork lookup: <strong>{{'Enabled' if tmdb_enabled else 'Disabled (optional)'}}</strong></p><p class="muted small">Session lifetime: {{session_hours}} hours. Default bind: {{host}}:{{port}}.</p></section>
    """
    return page("Settings", body, movie_dir=str(MOVIE_DIR), movies=movies, err=err, health=health,
                tmdb_enabled=bool(TMDB_API_KEY), session_hours=SESSION_HOURS, host=HOST, port=PORT)

@app.route("/diagnostics")
@login_required
def diagnostics_page():
    movies, movie_error = scan_movies()
    health = application_health(len(movies))
    status = minecraft_status()
    body = r"""
    <div class="eyebrow">Read-only system information</div><h1>Diagnostics</h1>
    <p class="muted">This screen reports application state only. It does not provide a shell or execute client-supplied commands.</p>
    <div class="card"><h2>Application</h2><p>Database integrity: <strong>{{'OK' if health.database_ok else 'Check failed'}}</strong></p><p>Database path: <code>{{health.database_path}}</code></p><p>X Stream: <strong>{{health.movie_count}} movies</strong> · {{'directory readable' if health.movie_directory_accessible else 'directory unavailable'}}</p>{% if movie_error %}<p class="notice error">{{movie_error}}</p>{% endif %}</div>
    <section class="section card"><h2>Minecraft manager</h2><p>Managed process: <strong>{{'Running' if minecraft.running else 'Stopped'}}</strong></p><p>Java: <strong>{{minecraft.java}}</strong> · configured JAR: <strong>{{'Yes' if minecraft.jar_exists else 'No'}}</strong></p><p class="muted small">Game-server start is always explicit. Opening diagnostics never starts it.</p></section>
    <section class="section card"><h2>Configuration</h2><p>Bind address: <code>{{host}}:{{port}}</code></p><p>Movie directory: <code>{{movie_dir}}</code></p><p>Optional online metadata: <strong>{{'enabled' if tmdb_enabled else 'disabled'}}</strong></p><p>Terminal command execution: <strong>Not implemented</strong></p></section>
    """
    return page("Diagnostics", body, health=health, minecraft=status, movie_error=movie_error,
                host=HOST, port=PORT, movie_dir=str(MOVIE_DIR), tmdb_enabled=bool(TMDB_API_KEY))

def configured_minecraft_jar():
    if not MC_JAR_FROM_ENV:
        return None
    try:
        root = MC_DIR.resolve()
        configured = Path(MC_JAR_FROM_ENV).expanduser()
        candidate = (configured if configured.is_absolute() else root / configured).resolve()
        if candidate.parent != root or candidate.suffix.lower() != ".jar" or not candidate.is_file():
            return None
        return candidate
    except OSError:
        return None

def minecraft_process_record():
    pid_file = DATA_DIR / "minecraft.pid"
    try:
        record = json.loads(pid_file.read_text(encoding="utf-8"))
        pid = int(record["pid"])
        jar = Path(record["jar"]).resolve()
        start_ticks = str(record["start_ticks"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if pid <= 1:
        return None
    if configured_minecraft_jar() != jar:
        return None
    try:
        proc_dir = Path("/proc") / str(pid)
        command = (proc_dir / "cmdline").read_bytes().split(b"\0")
        if os.fsencode(str(jar)) not in command:
            return None
        stat_fields = (proc_dir / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        if len(stat_fields) <= 19 or stat_fields[19] != start_ticks:
            return None
        os.kill(pid, 0)
    except (OSError, IndexError, UnicodeError, ProcessLookupError):
        return None
    return {"pid": pid, "jar": str(jar), "start_ticks": start_ticks}

def minecraft_status():
    try:
        proc = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=3)
        java = "available" if proc.returncode == 0 else "unavailable"
    except (OSError, subprocess.TimeoutExpired):
        java = "unavailable"
    jar = configured_minecraft_jar()
    try:
        memory_mb = int(MC_MAX_MEMORY_MB_SETTING)
    except ValueError:
        memory_mb = 0
    record = minecraft_process_record()
    return {
        "directory": str(MC_DIR),
        "directory_exists": MC_DIR.is_dir(),
        "jar_exists": jar is not None,
        "jar": str(jar) if jar else None,
        "java": java,
        "running": record is not None,
        "pid": record["pid"] if record else None,
        "memory_mb": memory_mb,
        "memory_valid": 128 <= memory_mb <= 512,
    }

def minecraft_jar_in_use(jar: Path) -> bool:
    expected = os.fsencode(str(jar.resolve()))
    try:
        entries = os.scandir("/proc")
    except OSError:
        return True
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                arguments = (Path("/proc") / entry.name / "cmdline").read_bytes().split(b"\0")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                continue
            if expected in arguments:
                return True
    return False

def start_minecraft():
    status = minecraft_status()
    if status["running"]:
        flash("Minecraft manager already reports a verified process running.", "success")
        return
    if status["java"] != "available":
        flash("Java was not found. Install a compatible Java runtime first.", "error")
        return
    if not status["directory_exists"] or not status["jar_exists"]:
        flash(f"No explicitly configured server JAR was found inside {MC_DIR}. Set XSERVER_MC_JAR to the trusted JAR path.", "error")
        return
    if not status["memory_valid"]:
        flash("XSERVER_MC_MAX_MEMORY_MB must be between 128 and 512.", "error")
        return
    jar = Path(status["jar"])
    if minecraft_jar_in_use(jar):
        flash("A process already uses the configured JAR, or process status cannot be inspected; refusing a duplicate start.", "error")
        return
    eula = MC_DIR / "eula.txt"
    try:
        eula_accepted = eula.is_file() and "eula=true" in eula.read_text(errors="ignore").lower()
    except OSError:
        eula_accepted = False
    if not eula_accepted:
        flash("Minecraft EULA not accepted. Review the official EULA and set eula=true yourself before starting.", "error")
        return
    try:
        log_path = LOG_DIR / "minecraft.log"
        with open(log_path, "ab", buffering=0) as log_file:
            process = subprocess.Popen(
                ["java", "-Xms128M", f"-Xmx{status['memory_mb']}M", "-jar", str(jar), "nogui"],
                cwd=str(MC_DIR), stdin=subprocess.PIPE, stdout=log_file, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True,
            )
        time.sleep(1)
        if process.poll() is not None:
            flash("Minecraft process exited during startup. Check logs.", "error")
            return
        stat_fields = (Path("/proc") / str(process.pid) / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        record = {"pid": process.pid, "jar": str(jar), "start_ticks": stat_fields[19]}
        pid_file = DATA_DIR / "minecraft.pid"
        temp_pid_file = DATA_DIR / "minecraft.pid.tmp"
        temp_pid_file.write_text(json.dumps(record), encoding="utf-8")
        temp_pid_file.chmod(0o600)
        temp_pid_file.replace(pid_file)
    except (OSError, IndexError, subprocess.SubprocessError) as exc:
        if "process" in locals() and process.poll() is None:
            process.terminate()
        app.logger.warning("Minecraft launch failed: %s", type(exc).__name__)
        flash("Could not safely start Minecraft. Check Java, the configured server file, permissions, and logs.", "error")
        return
    flash(f"Managed Minecraft process started (PID {process.pid}). Check logs for readiness.", "success")

def stop_minecraft():
    record = minecraft_process_record()
    if not record:
        return False
    try:
        os.kill(record["pid"], signal.SIGTERM)
    except OSError:
        return False
    for _ in range(20):
        if minecraft_process_record() is None:
            return True
        time.sleep(0.25)
    return False

@app.route("/minecraft", methods=["GET", "POST"])
@login_required
def minecraft_page():
    if request.method == "POST":
        verify_csrf()
        action = request.form.get("action", "")
        if action == "start":
            with MC_START_LOCK:
                start_minecraft()
        elif action == "stop":
            with MC_START_LOCK:
                if stop_minecraft():
                    flash("Verified managed Minecraft process stopped.", "success")
                else:
                    flash("No verifiable live Minecraft process stopped; it may still be shutting down.", "error")
        elif action == "restart":
            with MC_START_LOCK:
                if stop_minecraft():
                    start_minecraft()
                else:
                    flash("Minecraft was not restarted because its managed process could not be safely stopped.", "error")
        elif action == "refresh":
            flash("Status refreshed.", "success")
        return redirect(url_for("minecraft_page"))
    status = minecraft_status()
    log_path = LOG_DIR / "minecraft.log"
    try:
        logs = log_path.read_text(errors="replace")[-7000:] if log_path.exists() else "No log output yet."
    except OSError:
        logs = "Cannot read Minecraft log."
    body = r"""
    <div class="eyebrow">Independent service</div><h1>Minecraft Manager</h1>
    <p class="muted">This service is optional. X Stream keeps working if Minecraft is missing or stopped.</p>
    <div class="card"><div class="stats"><div class="stat"><strong>{{'Running' if status.running else 'Stopped'}}</strong><span class="muted">Managed process{% if status.pid %} · PID {{status.pid}}{% endif %}</span></div><div class="stat"><strong>{{status.java|title}}</strong><span class="muted">Java runtime</span></div><div class="stat"><strong>{{'Found' if status.jar_exists else 'Not found'}}</strong><span class="muted">Server JAR</span></div></div>
    <p class="muted small">Directory: {{status.directory}}</p>
    <div class="actions"><form method="post"><input type="hidden" name="_csrf" value="{{csrf()}}"><button name="action" value="start" type="submit" {{'disabled' if status.running}}>Start server</button></form><form method="post" data-confirm="Send a stop signal to the process tracked by X Server?"><input type="hidden" name="_csrf" value="{{csrf()}}"><button class="danger" name="action" value="stop" type="submit" {{'disabled' if not status.running}}>Stop managed process</button></form><form method="post" data-confirm="Stop and relaunch the configured server?"><input type="hidden" name="_csrf" value="{{csrf()}}"><button class="secondary" name="action" value="restart" type="submit" {{'disabled' if not status.running}}>Restart server</button></form><form method="post"><input type="hidden" name="_csrf" value="{{csrf()}}"><button class="secondary" name="action" value="refresh">Refresh status</button></form></div>
    </div>
    <section class="section card"><h2>Recent logs</h2><pre style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;color:#c8ccd6;font-size:.8rem">{{logs}}</pre></section>
    <div class="notice">Safety: starts require an explicitly configured JAR and accepted EULA. The configured heap defaults to 512 MiB maximum, excluding JVM/native and Android overhead. A Minecraft server is not recommended on a 2 GB phone; this manager does not auto-start it.</div>
    """
    return page("Minecraft Manager", body, status=status, logs=logs)

@app.errorhandler(400)
def bad_request(err):
    return page("Bad request", '<div class="card"><h1>Request rejected</h1><p class="muted">{{message}}</p><a class="btn" href="{{url_for("dashboard")}}">Return home</a></div>',
                message=getattr(err, "description", "Invalid request")), 400

@app.errorhandler(403)
def forbidden(err):
    return page("Forbidden", '<div class="card"><h1>Access denied</h1><p class="muted">You do not have permission to access this resource.</p></div>'), 403

@app.errorhandler(404)
def not_found(err):
    return page("Not found", '<div class="card"><h1>Not found</h1><p class="muted">{{message}}</p><a class="btn" href="{{url_for("dashboard")}}">Return home</a></div>',
                message=getattr(err, "description", "The requested item was not found.")), 404

@app.errorhandler(500)
def server_error(err):
    app.logger.exception("Unhandled server error")
    return page("Server error", '<div class="card"><h1>Something went wrong</h1><p class="muted">The error was logged locally. Check logs for details.</p><a class="btn" href="{{url_for("dashboard")}}">Return home</a></div>'), 500

if __name__ == "__main__":
    print("=" * 60)
    print("X Server / X Stream")
    print(f"Local URL: http://{HOST}:{PORT}")
    print(f"Movie folder: {MOVIE_DIR}")
    print(f"Database: {DB_PATH}")
    print("First launch: create your permanent User ID and password in the browser.")
    print("Keep HOST at 127.0.0.1 unless you understand the security implications.")
    print("=" * 60)
    # Do not use Flask debug mode on a personal device.
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
