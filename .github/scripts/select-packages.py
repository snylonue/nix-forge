#!/usr/bin/env python3
import argparse
import json
import os
import sys


def attr_name(lock_path: str) -> str | None:
    """
    `…/locks/forge-1.20.1-47.4.10.json` -> `forge-1_20_1`.
    """
    name = os.path.basename(lock_path)
    if not (name.startswith("forge-") and name.endswith(".json")):
        return None
    mc = name[len("forge-") : -len(".json")].split("-", 1)[0]
    return "forge-" + mc.replace(".", "_")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--full", required=True, help="JSON array of the names that exist")
    ap.add_argument("--changed", required=True, help="file of lock paths this run changed")
    ap.add_argument("--force", action="store_true", help="select every name in --full")
    ap.add_argument("--output", help="file to append to instead of stdout")
    args = ap.parse_args()

    with open(args.full) as f:
        full = set(json.load(f))
    with open(args.changed) as f:
        changed = f.read().split()

    if args.force:
        selected = full
    else:
        # Intersecting with --full drops a deleted lock, which has nothing left
        # to build, and any name whose derivation is gone, which should not fail
        # the matrix.
        selected = {name for name in map(attr_name, changed) if name} & full

    line = "attrs=" + json.dumps(sorted(selected)) + "\n"
    if args.output:
        with open(args.output, "a") as out:
            out.write(line)
    else:
        sys.stdout.write(line)


if __name__ == "__main__":
    main()
