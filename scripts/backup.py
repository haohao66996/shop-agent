# -*- coding: utf-8 -*-
"""Create a consistent backup of the SQLite database and upload directory."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from src.config import abs_path, settings


def create_backup(dest: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = dest / f"shop_agent_backup_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)

    db_path = abs_path(settings.database_path)
    if db_path.exists():
        source = sqlite3.connect(db_path)
        target = sqlite3.connect(backup_dir / "shop_agent.db")
        with target:
            source.backup(target)
        target.close()
        source.close()

    upload_dir = abs_path(settings.upload_dir)
    if upload_dir.exists():
        shutil.copytree(upload_dir, backup_dir / "uploads")

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "database": str(db_path),
        "upload_dir": str(upload_dir),
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return backup_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup shop_agent data")
    parser.add_argument(
        "--dest",
        default=str(Path("data") / "backups"),
        help="Backup destination directory",
    )
    args = parser.parse_args()
    backup_dir = create_backup(Path(args.dest))
    print(f"BACKUP_OK {backup_dir}")


if __name__ == "__main__":
    main()
