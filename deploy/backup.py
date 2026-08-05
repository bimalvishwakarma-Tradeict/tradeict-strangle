import os
import sys
import zipfile
import subprocess
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s UTC] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

BACKUP_DIR = "/home/botuser/trading-bot"
RCLONE_REMOTE = "gdrive:"
RCLONE_CONFIG = "/root/.config/rclone/rclone.conf"
KEEP_DAYS = 7
EXCLUDE_DIRS = {'.venv', '__pycache__', 'node_modules', '.git', 'dist'}

def create_zip():
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    zip_path = f"/tmp/trading-bot-backup_{timestamp}.zip"
    count = 0
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(BACKUP_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for file in files:
                if file.endswith('.pyc'):
                    continue
                fp = os.path.join(root, file)
                zf.write(fp, os.path.relpath(fp, BACKUP_DIR))
                count += 1
    size = os.path.getsize(zip_path) / (1024*1024)
    logger.info(f"Zip ready: {count} files, {size:.2f} MB — {zip_path}")
    return zip_path

def upload(zip_path):
    logger.info(f"Uploading via rclone: {os.path.basename(zip_path)}")
    result = subprocess.run([
        'rclone', 'copy', zip_path, RCLONE_REMOTE,
        '--config', RCLONE_CONFIG,
        '-v'
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"rclone upload failed: {result.stderr}")
    logger.info("Upload successful!")

def delete_old():
    logger.info(f"Deleting backups older than {KEEP_DAYS} days...")
    result = subprocess.run([
        'rclone', 'delete', RCLONE_REMOTE,
        '--min-age', f'{KEEP_DAYS}d',
        '--include', 'trading-bot-backup_*.zip',
        '--config', RCLONE_CONFIG,
        '-v'
    ], capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(f"Cleanup warning: {result.stderr}")
    else:
        logger.info("Old backups cleaned up")

def main():
    logger.info("=== Backup started ===")
    zip_path = None
    try:
        zip_path = create_zip()
        upload(zip_path)
        delete_old()
        logger.info("=== Backup complete! ===")
    except Exception as e:
        logger.error(f"BACKUP FAILED: {e}")
        sys.exit(1)
    finally:
        if zip_path and os.path.exists(zip_path):
            os.remove(zip_path)
            logger.info("Local zip removed")

if __name__ == "__main__":
    main()
