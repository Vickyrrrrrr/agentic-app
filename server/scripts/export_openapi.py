"""Dump the engine OpenAPI contract. TS clients generate from this file.

Usage (from server/): python scripts/export_openapi.py [--out path]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from agentic_server.main import app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs", "openapi", "engine.json"))
    args = parser.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(app.openapi(), fh, indent=2)
    print(f"wrote {args.out} ({len(app.routes)} routes)")


if __name__ == "__main__":
    main()
