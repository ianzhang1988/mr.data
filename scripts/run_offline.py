#!/usr/bin/env python3
"""Run offline attribution analysis on recent dialogues."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mr_data.db import PostgresStore
from mr_data.offline import AttributionEngine


def main() -> None:
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    engine = AttributionEngine(pg_store=pg)
    engine.run()


if __name__ == "__main__":
    main()
