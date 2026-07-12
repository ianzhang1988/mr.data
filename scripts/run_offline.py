#!/usr/bin/env python3
"""Run offline attribution analysis on recent dialogues."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mr_data.offline import AttributionEngine


def main() -> None:
    engine = AttributionEngine()
    engine.run()


if __name__ == "__main__":
    main()
