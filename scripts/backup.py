"""Create a consistent SQLite backup, including while the app is running."""
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

source = Path(os.getenv('INVENTORY_DB', 'data/inventory.db')).resolve()
target = Path(sys.argv[1] if len(sys.argv) > 1 else f'backups/inventory-{datetime.now():%Y%m%d-%H%M%S}.db').resolve()
if not source.is_file():
    raise SystemExit('数据库不存在；请先启动并使用系统。')
if source == target or target.exists():
    raise SystemExit('备份目标必须是新的文件，不能覆盖原数据库或已有备份。')
target.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
print(f'备份完成：{target}')
