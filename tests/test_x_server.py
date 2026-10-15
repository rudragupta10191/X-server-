import os
import re
import hashlib
import signal
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from unittest.mock import patch

TEST_ROOT = tempfile.TemporaryDirectory(prefix="xserver-tests-")
os.environ["XSERVER_DATA_DIR"] = str(Path(TEST_ROOT.name) / "initial-data")
os.environ["XSERVER_LOG_DIR"] = str(Path(TEST_ROOT.name) / "initial-logs")
os.environ.pop("XSERVER_MOVIE_DIR", None)
os.environ["XSERVER_MC_DIR"] = str(Path(TEST_ROOT.name) / "minecraft")

import x_server
from scripts.data_tools import backup, restore_to, verify_backup


class XServerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xserver-case-")
        self.root = Path(self.temp.name)
        self.movies = self.root / "Movies"
        self.movies.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.old_values = {
            "DB_PATH": x_server.DB_PATH,
            "DATA_DIR": x_server.DATA_DIR,
            "LOG_DIR": x_server.LOG_DIR,
            "MOVIE_DIR": x_server.MOVIE_DIR,
            "MOVIE_DIR_FROM_ENV": x_server.MOVIE_DIR_FROM_ENV,
            "MC_DIR": x_server.MC_DIR,
            "MC_JAR_FROM_ENV": x_server.MC_JAR_FROM_ENV,
        }
        x_server.DB_PATH = self.data / "xserver.sqlite3"
        x_server.DATA_DIR = self.data
        x_server.LOG_DIR = self.data / "logs"
        x_server.LOG_DIR.mkdir()
        x_server.MOVIE_DIR = self.movies
        x_server.MOVIE_DIR_FROM_ENV = None
        x_server.MC_DIR = self.root / "mc"
        x_server.MC_DIR.mkdir()
        x_server.MC_JAR_FROM_ENV = ""
        x_server.init_db()
        x_server.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = x_server.app.test_client()

    def tearDown(self):
        for name, value in self.old_values.items():
            setattr(x_server, name, value)
        self.temp.cleanup()

    def csrf(self, response):
        match = re.search(rb'name="_csrf" value="([^"]+)"', response.data)
        self.assertIsNotNone(match, response.data[:400])
        return match.group(1).decode()

    def setup_account(self, username="owner", password="a-secure-test-passphrase"):
        response = self.client.get("/setup")
        token = self.csrf(response)
        response = self.client.post("/setup", data={
            "_csrf": token, "username": username, "password": password, "confirm": password,
        })
        self.assertEqual(response.status_code, 302)
        return username, password

    def login(self, username="owner", password="a-secure-test-passphrase"):
        response = self.client.get("/login")
        token = self.csrf(response)
        return self.client.post("/login", data={
            "_csrf": token, "username": username, "password": password,
        })

    def test_setup_login_protection_and_security_headers(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        self.setup_account()
        self.assertEqual(self.login().status_code, 302)
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_bad_password_is_throttled_and_safe_redirect_is_enforced(self):
        self.setup_account()
        for _ in range(x_server.MAX_LOGIN_ATTEMPTS):
            response = self.login(password="incorrect-password")
            self.assertEqual(response.status_code, 200)
        response = self.login(password="incorrect-password")
        self.assertIn(b"Too many attempts", response.data)
        with self.client.session_transaction() as session:
            session.clear()
        x_server.clear_login_failures("127.0.0.1")
        response = self.client.get("/login?next=//evil.example")
        token = self.csrf(response)
        response = self.client.post("/login?next=//evil.example", data={
            "_csrf": token, "username": "owner", "password": "a-secure-test-passphrase",
        })
        self.assertEqual(response.location, "/dashboard")

    def test_logout_requires_post_and_csrf(self):
        self.setup_account()
        self.login()
        self.assertEqual(self.client.get("/logout").status_code, 405)
        self.assertEqual(self.client.post("/logout").status_code, 400)
        with self.client.session_transaction() as session:
            token = session["_csrf"]
        response = self.client.post("/logout", data={"_csrf": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 302)

    def test_password_change_verifies_current_password_and_requires_new_login(self):
        self.setup_account()
        self.login()
        second_client = x_server.app.test_client()
        second_login = second_client.get("/login")
        second_client.post("/login", data={
            "_csrf": self.csrf(second_login),
            "username": "owner",
            "password": "a-secure-test-passphrase",
        })
        response = self.client.get("/account/password")
        token = self.csrf(response)
        response = self.client.post("/account/password", data={
            "_csrf": token,
            "current_password": "wrong-current-password",
            "new_password": "another-secure-passphrase",
            "confirm": "another-secure-passphrase",
        })
        self.assertIn(b"Current password is incorrect", response.data)
        response = self.client.get("/account/password")
        token = self.csrf(response)
        response = self.client.post("/account/password", data={
            "_csrf": token,
            "current_password": "a-secure-test-passphrase",
            "new_password": "another-secure-passphrase",
            "confirm": "another-secure-passphrase",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        self.assertEqual(second_client.get("/dashboard").status_code, 302)
        self.assertEqual(self.login(password="another-secure-passphrase").status_code, 302)

    def test_movie_scan_range_path_guard_and_missing_file(self):
        self.setup_account()
        self.login()
        movie = self.movies / "A film with spaces – 日本語.mp4"
        movie.write_bytes(b"0123456789")
        (self.movies / "not-a-movie.txt").write_text("ignore")
        (self.movies / "clip.MKV").write_bytes(b"matroska")
        movies, error = x_server.scan_movies()
        self.assertIsNone(error)
        self.assertEqual(len(movies), 2)
        self.assertIn("A film with spaces – 日本語", {item["title"] for item in movies})
        key = x_server.movie_key(movie)
        response = self.client.get(f"/video/{key}", headers={"Range": "bytes=2-5"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"2345")
        self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(self.client.get("/video/../../etc/passwd").status_code, 404)
        self.assertEqual(self.client.get(f"/video/{key}", headers={"Range": "bytes=" + ("9" * 5000) + "-"}).status_code, 416)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get(f"/video/{key}").status_code, 302)
        self.login()
        movie.unlink()
        self.assertEqual(self.client.get(f"/video/{key}").status_code, 404)

    def test_movie_directory_errors_are_reported_without_crashing(self):
        x_server.MOVIE_DIR = self.root / "missing-movies"
        movies, error = x_server.scan_movies()
        self.assertEqual(movies, [])
        self.assertIn("not found", error)
        x_server.MOVIE_DIR = self.movies
        with patch.object(Path, "iterdir", side_effect=PermissionError("inaccessible")):
            movies, error = x_server.scan_movies()
        self.assertEqual(movies, [])
        self.assertIn("Cannot access", error)

    def test_my_list_progress_persistence_and_csrf(self):
        self.setup_account()
        self.login()
        movie = self.movies / "Persistence.mp4"
        movie.write_bytes(b"movie")
        key = x_server.movie_key(movie)
        self.assertEqual(self.client.post(f"/list/toggle/{key}").status_code, 400)
        with self.client.session_transaction() as session:
            token = session["_csrf"]
        self.assertEqual(self.client.post(f"/list/toggle/{key}", data={"_csrf": token}).status_code, 302)
        self.assertEqual(self.client.get("/my-list").status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/progress/{key}", json={"seconds": 30, "duration": 90},
            headers={"X-CSRF-Token": token},
        ).status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/progress/{key}", json=["not", "an", "object"],
            headers={"X-CSRF-Token": token},
        ).status_code, 400)
        x_server.init_db()
        with x_server.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM my_list").fetchone()[0], 1)
            progress = connection.execute("SELECT seconds,duration FROM progress").fetchone()
        self.assertEqual((progress["seconds"], progress["duration"]), (30, 90))

    def test_settings_persist_and_diagnostics_are_protected_read_only(self):
        self.setup_account()
        self.login()
        new_movie_dir = self.root / "configured-movies"
        response = self.client.get("/settings")
        token = self.csrf(response)
        response = self.client.post("/settings", data={"_csrf": token, "movie_dir": str(new_movie_dir)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(x_server.MOVIE_DIR, new_movie_dir)
        x_server.MOVIE_DIR = Path("/not-the-persisted-setting")
        x_server.init_db()
        self.assertEqual(x_server.MOVIE_DIR, new_movie_dir)
        self.assertEqual(self.client.get("/diagnostics").status_code, 200)
        self.assertIn(b"does not provide a shell", self.client.get("/diagnostics").data)

    def test_additive_schema_initialization_preserves_existing_user_rows(self):
        with x_server.db() as connection:
            connection.execute(
                "INSERT INTO users(id,username,password_hash,created_at) VALUES(1,'preserved','hash','date')"
            )
            connection.execute("INSERT INTO my_list(user_id,movie_key,added_at) VALUES(1,'abc','date')")
        x_server.init_db()
        with x_server.db() as connection:
            self.assertEqual(connection.execute("SELECT username FROM users WHERE id=1").fetchone()[0], "preserved")
            self.assertEqual(connection.execute("SELECT movie_key FROM my_list").fetchone()[0], "abc")
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
            ).fetchone())

    def test_minecraft_status_does_not_claim_unconfigured_server_running(self):
        status = x_server.minecraft_status()
        self.assertFalse(status["running"])
        self.assertIsNone(status["pid"])
        self.assertFalse(status["jar_exists"])
        self.assertEqual(self.client.get("/minecraft").status_code, 302)
        self.setup_account()
        self.login()
        with patch("x_server.subprocess.run", return_value=subprocess.CompletedProcess(
                ["java", "-version"], 0, stdout="", stderr="")):
            with patch("x_server.subprocess.Popen") as launch:
                response = self.client.get("/minecraft")
                self.assertEqual(response.status_code, 200)
                token = self.csrf(response)
                self.client.post("/minecraft", data={"_csrf": token, "action": "start"})
        launch.assert_not_called()

    def test_minecraft_pid_record_cannot_signal_an_unverified_process(self):
        x_server.MC_DIR.mkdir(exist_ok=True)
        (x_server.DATA_DIR / "minecraft.pid").write_text(
            '{"pid":%d,"jar":"/tmp/other-server.jar","start_ticks":"1"}' % os.getpid(),
            encoding="utf-8",
        )
        self.assertIsNone(x_server.minecraft_process_record())
        with patch("x_server.os.kill") as kill:
            self.assertFalse(x_server.stop_minecraft())
        kill.assert_not_called()

    def test_read_only_diagnostics_has_no_terminal_execution_surface(self):
        self.assertEqual(self.client.get("/diagnostics").status_code, 302)
        self.assertNotIn("/terminal", {rule.rule for rule in x_server.app.url_map.iter_rules()})

    def test_optional_metadata_missing_dependency_falls_back_to_local_metadata(self):
        item = {"key": "abc", "title": "Real Local Filename"}
        with patch.object(x_server, "TMDB_API_KEY", "test-only-placeholder"):
            with patch.dict(sys.modules, {"requests": None}):
                metadata = x_server.tmdb_lookup(item)
        self.assertEqual(metadata["title"], "Real Local Filename")
        self.assertEqual(metadata["overview"], "")


class BackupTestCase(unittest.TestCase):
    def test_backup_integrity_and_restore_to_separate_location(self):
        with tempfile.TemporaryDirectory(prefix="xserver-backup-test-") as temp:
            root = Path(temp)
            data_dir = root / "live"
            data_dir.mkdir()
            database = data_dir / "xserver.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE preserved(value TEXT)")
                connection.execute("INSERT INTO preserved VALUES('keep')")
            (data_dir / "session_secret").write_text("not-a-real-secret", encoding="utf-8")
            backup_dir = backup(data_dir, root / "backup-store")
            verify_backup(backup_dir)
            restored = restore_to(backup_dir, root / "restored")
            with sqlite3.connect(restored / "xserver.sqlite3") as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT value FROM preserved").fetchone()[0], "keep")
            self.assertEqual((restored / "session_secret").read_text(), "not-a-real-secret")
            with self.assertRaises(FileExistsError):
                restore_to(backup_dir, restored)


class ApplicationRestartTestCase(unittest.TestCase):
    def test_real_server_restart_preserves_session_list_and_progress(self):
        with tempfile.TemporaryDirectory(prefix="xserver-restart-test-") as temp:
            root = Path(temp)
            data_dir = root / "data"
            movies_dir = root / "movies"
            movies_dir.mkdir()
            movie = movies_dir / "Restarted session.mp4"
            movie.write_bytes(b"test movie data")
            port_socket = socket.socket()
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
            port_socket.close()
            env = os.environ.copy()
            env.update({
                "XSERVER_DATA_DIR": str(data_dir),
                "XSERVER_LOG_DIR": str(root / "logs"),
                "XSERVER_MOVIE_DIR": str(movies_dir),
                "XSERVER_MC_DIR": str(root / "minecraft"),
                "XSERVER_HOST": "127.0.0.1",
                "XSERVER_PORT": str(port),
            })
            app_script = str(Path(x_server.__file__).resolve())
            client = build_opener(HTTPCookieProcessor(CookieJar()))

            def start_server():
                process = subprocess.Popen(
                    [sys.executable, app_script], cwd=Path(app_script).parent, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        client.open(f"http://127.0.0.1:{port}/login", timeout=1)
                        return process
                    except Exception:
                        if process.poll() is not None:
                            output, _ = process.communicate()
                            raise AssertionError(f"Flask process exited during startup: {output[-1000:]}")
                        time.sleep(0.1)
                process.send_signal(signal.SIGINT)
                output, _ = process.communicate(timeout=10)
                raise AssertionError(f"Flask process did not become ready: {output[-1000:]}")

            def stop_server(process):
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0)

            def get_csrf(path):
                response = client.open(f"http://127.0.0.1:{port}{path}")
                match = re.search(rb'name="_csrf" value="([^"]+)"', response.read())
                self.assertIsNotNone(match)
                return match.group(1).decode()

            process = start_server()
            try:
                setup_token = get_csrf("/setup")
                client.open(Request(
                    f"http://127.0.0.1:{port}/setup",
                    data=urlencode({
                        "_csrf": setup_token, "username": "owner",
                        "password": "restart-test-passphrase", "confirm": "restart-test-passphrase",
                    }).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                ))
                login_token = get_csrf("/login")
                client.open(Request(
                    f"http://127.0.0.1:{port}/login",
                    data=urlencode({
                        "_csrf": login_token, "username": "owner",
                        "password": "restart-test-passphrase",
                    }).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                ))
                key = hashlib.sha256(str(movie.resolve()).encode()).hexdigest()[:32]
                csrf = get_csrf("/dashboard")
                client.open(Request(
                    f"http://127.0.0.1:{port}/list/toggle/{key}",
                    data=urlencode({"_csrf": csrf}).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                ))
                client.open(Request(
                    f"http://127.0.0.1:{port}/api/progress/{key}",
                    data=b'{"seconds":25,"duration":100}',
                    headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
                    method="POST",
                ))
            finally:
                stop_server(process)

            process = start_server()
            try:
                listed = client.open(f"http://127.0.0.1:{port}/my-list").read()
                detail = client.open(f"http://127.0.0.1:{port}/movie/{key}").read()
                self.assertIn(b"Restarted session", listed)
                self.assertIn(b"Saved progress: 25%", detail)
            finally:
                stop_server(process)


if __name__ == "__main__":
    unittest.main()
