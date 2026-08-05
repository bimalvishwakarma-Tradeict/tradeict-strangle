#!/usr/bin/env python3
"""
Google Drive automated backup for trading-bot.

Requires:
  pip install google-api-python-client google-auth

Cron example (daily 03:00 IST):
  30 21 * * * /home/botuser/trading-bot/.venv/bin/python /home/botuser/trading-bot/deploy/backup.py
"""

from __future__ import annotations

import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- Exact config (do not change without ops approval) ---
GDRIVE_FOLDER_ID = "1qjEJZFsUxh030OPIKgpvZHPS8E_Cxs4K"
CREDENTIALS_FILE = "/home/botuser/trading-bot/deploy/gdrive_credentials.json"
BACKUP_DIR = "/home/botuser/trading-bot"
KEEP_DAYS = 7

SCOPES = ["https://www.googleapis.com/auth/drive"]
EXCLUDE_DIR_NAMES = {".venv", "__pycache__", "node_modules", ".git", "dist"}
EXCLUDE_SUFFIXES = {".pyc"}


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def _should_exclude(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True
    if any(part in EXCLUDE_DIR_NAMES for part in rel_parts):
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    return False


def create_zip(backup_root: Path) -> Path:
    """Step 1 — zip project excluding heavy/ephemeral dirs and *.pyc."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    zip_path = Path("/tmp") / f"trading-bot-backup_{stamp}.zip"
    root = backup_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"BACKUP_DIR not found: {root}")

    _log(f"Creating zip: {zip_path}")
    count = 0
    with zipfile.ZipFile(
        zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as zf:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if _should_exclude(path, root):
                continue
            arcname = path.relative_to(root).as_posix()
            zf.write(path, arcname=arcname)
            count += 1
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    _log(f"Zip ready: {count} files, {size_mb:.2f} MB")
    return zip_path


def _drive_service():
    creds_path = Path(CREDENTIALS_FILE)
    if not creds_path.is_file():
        raise FileNotFoundError(f"Credentials missing: {creds_path}")
    credentials = service_account.Credentials.from_service_account_file(
        str(creds_path),
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def upload_to_drive(zip_path: Path) -> str:
    """Step 2 — upload zip to the configured Google Drive folder."""
    service = _drive_service()
    metadata = {
        "name": zip_path.name,
        "parents": [GDRIVE_FOLDER_ID],
    }
    media = MediaFileUpload(str(zip_path), mimetype="application/zip", resumable=True)
    _log(f"Uploading to Drive folder {GDRIVE_FOLDER_ID}: {zip_path.name}")
    created = (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name,createdTime,size",
            supportsAllDrives=True,
        )
        .execute()
    )
    file_id = str(created.get("id") or "")
    if not file_id:
        raise RuntimeError(f"Upload returned no file id: {created}")
    _log(f"Uploaded OK id={file_id} name={created.get('name')}")
    return file_id


def delete_old_backups() -> int:
    """Step 3 — delete files older than KEEP_DAYS in the same Drive folder."""
    service = _drive_service()
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    query = (
        f"'{GDRIVE_FOLDER_ID}' in parents and trashed=false "
        f"and mimeType='application/zip'"
    )
    deleted = 0
    page_token: str | None = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, createdTime)",
                pageToken=page_token,
                pageSize=100,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for item in response.get("files", []):
            created_raw = item.get("createdTime")
            if not created_raw:
                continue
            created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            if created_at >= cutoff:
                continue
            file_id = item["id"]
            name = item.get("name", file_id)
            service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
            deleted += 1
            _log(f"Deleted old backup: {name} (created {created_raw})")
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    _log(f"Cleanup done: deleted {deleted} file(s) older than {KEEP_DAYS} days")
    return deleted


def main() -> None:
    zip_path: Path | None = None
    try:
        _log("Backup started")
        zip_path = create_zip(Path(BACKUP_DIR))
        upload_to_drive(zip_path)
        delete_old_backups()
        _log("SUCCESS — backup complete")
    except Exception as exc:
        _log(f"FAILURE — {exc}")
        sys.exit(1)
    finally:
        if zip_path is not None and zip_path.exists():
            try:
                zip_path.unlink()
                _log(f"Local zip removed: {zip_path}")
            except OSError as exc:
                _log(f"Warning: could not remove local zip: {exc}")


if __name__ == "__main__":
    main()
