"""
setup_db.py — Initialize the SQLite database schema.

Run directly: python3 scripts/setup_db.py
Also called by install_deps.sh during setup.
"""

import sys
from pathlib import Path

# Allow running from repo root or from scripts/
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

from modules.config_manager import ConfigManager
from modules.plate_database import PlateDatabase


def main():
    config_path = repo_root / "config.yaml"
    if not config_path.exists():
        print(f"ERROR: config.yaml not found at {config_path}")
        sys.exit(1)

    config = ConfigManager(str(config_path))
    db_path = config.storage_base_path / "plates.db"

    print(f"Initialising database at {db_path} ...")
    db = PlateDatabase(db_path=str(db_path))
    db.close()
    print("Database ready.")


if __name__ == "__main__":
    main()
