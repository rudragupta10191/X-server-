#!/usr/bin/env python3
"""Consistent X Server SQLite backup, verification, and non-destructive restore."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path(os.environ.get("XSERVER_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
DATABASE_NAME = "xserver.sqlite3"
SECRET_NAME = "session_secret"


def integrity_check(database: Path) -> None:
    if not database.is_file():
        raise FileNotFoundError(f"Database not found: {database}")
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise sqlite3.DatabaseError("SQLite integrity check failed")


def sqlite_backup(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Database not found: {source}")
    with sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True) as source_connection:
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)
    destination.chmod(0o600)
    integrity_check(destination)


def verify_backup(backup_dir: Path) -> None:
    integrity_check(backup_dir / DATABASE_NAME)


def backup(data_dir: Path, backup_root: Path) -> Path:
    data_dir = data_dir.expanduser().resolve()
    backup_root = backup_root.expanduser().resolve()
    source_database = data_dir / DATABASE_NAME
    if backup_root == data_dir or data_dir in backup_root.parents or backup_root == source_database:
        raise ValueError("Backup destination must be outside the active data directory")
    if not source_database.is_file():
        raise FileNotFoundError(f"Database not found: {source_database}")
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination_dir = backup_root / f"xserver-{stamp}"
    destination_dir.mkdir(mode=0o700, exist_ok=False)
    sqlite_backup(source_database, destination_dir / DATABASE_NAME)
    secret = data_dir / SECRET_NAME
    if secret.is_file():
        shutil.copyfile(secret, destination_dir / SECRET_NAME)
        (destination_dir / SECRET_NAME).chmod(0o600)
    verify_backup(destination_dir)
    return destination_dir


def restore_to(backup_dir: Path, destination: Path) -> Path:
    backup_dir = backup_dir.expanduser().resolve()
    destination = destination.expanduser().resolve()
    verify_backup(backup_dir)
    if destination.exists():
        raise FileExistsError(f"Restore destination already exists: {destination}")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    sqlite_backup(backup_dir / DATABASE_NAME, destination / DATABASE_NAME)
    secret = backup_dir / SECRET_NAME
    if secret.is_file():
        shutil.copyfile(secret, destination / SECRET_NAME)
        (destination / SECRET_NAME).chmod(0o600)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    backup_parser = subparsers.add_parser("backup", help="Create a unique SQLite-consistent backup")
    backup_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    backup_parser.add_argument("--backup-dir", type=Path, default=DEFAULT_DATA_DIR.parent / "backups")

    verify_parser = subparsers.add_parser("verify", help="Check a backup database")
    verify_parser.add_argument("backup_dir", type=Path)

    restore_parser = subparsers.add_parser("restore", help="Restore to a new, separate directory")
    restore_parser.add_argument("backup_dir", type=Path)
    restore_parser.add_argument("destination", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.action == "backup":
            result = backup(args.data_dir, args.backup_dir)
        elif args.action == "verify":
            verify_backup(args.backup_dir)
            result = args.backup_dir
        else:
            result = restore_to(args.backup_dir, args.destination)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Data operation failed: {exc}", file=sys.stderr)
        return 1
    print(f"{args.action.capitalize()} verified: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
