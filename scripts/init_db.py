#!/usr/bin/env python3
"""Initialize PostgreSQL schema and seed default identity / dimensions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mr_data.db import PostgresStore


def main() -> None:
    store = PostgresStore()
    store.init_schema()
    store.seed()
    print("Database initialized and seeded.")


if __name__ == "__main__":
    main()
