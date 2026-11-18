#!/usr/bin/env python
"""Apply the additive alias index to an existing SQLite database."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blackholememory.alias_index import migrate_alias_index

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("database does not exist")
    migrate_alias_index(args.database)
