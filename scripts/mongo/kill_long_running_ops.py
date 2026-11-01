#!/usr/bin/env python3
"""
Identify (dry run) or kill long-running MongoDB operations.

Safety filters - an operation is NEVER killed if it is:
  * on an internal database (local, config, admin)
  * on a system collection (*.system.*)
  * a balancer operation
  * an index build / createIndexes
  * without a namespace
  * outside the --db filter (when one is given)

The connection string is read from --uri or, preferably, the MONGO_URI
environment variable so that it never appears in the process list.
"""

import argparse
import json
import os
import re
import sys

from pymongo import MongoClient
from pymongo.errors import PyMongoError

INTERNAL_DB_PREFIXES = ("local.", "config.", "admin.")


def positive_int(value):
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return ivalue


def parse_args():
    parser = argparse.ArgumentParser(
        description="Safely identify / kill long-running MongoDB operations"
    )

    parser.add_argument(
        "--uri",
        default=os.environ.get("MONGO_URI"),
        help="MongoDB connection string (defaults to the MONGO_URI env var)",
    )

    parser.add_argument(
        "--seconds",
        type=positive_int,
        required=True,
        help="Target operations running longer than this many seconds",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only identify and print operations, do not kill",
    )

    parser.add_argument(
        "--db",
        help="Only target operations for this database (optional)",
    )

    args = parser.parse_args()
    if not args.uri:
        parser.error("no connection string: pass --uri or set MONGO_URI")
    return args


def skip_reason(op, db_filter):
    """Return why an op must be skipped, or None if it is a valid target."""
    ns = op.get("ns", "")
    desc = op.get("desc", "")

    if not ns:
        return "empty namespace"
    if db_filter and not ns.startswith(db_filter + "."):
        return "outside DB filter"
    if ns.startswith(INTERNAL_DB_PREFIXES):
        return "internal DB"
    if ".system." in ns:
        return "system namespace"
    if re.search("Balancer", desc):
        return "balancer"
    if op.get("msg") == "Index Build":
        return "index build"
    if "createIndexes" in op.get("command", {}):
        return "createIndexes"
    return None


def main():
    args = parse_args()

    print("[INFO] MongoDB long-op killer starting")
    print(f"[INFO] Threshold   : {args.seconds}s")
    print(f"[INFO] Dry run     : {args.dry_run}")
    print(f"[INFO] DB filter   : {args.db or 'ALL'}")

    try:
        client = MongoClient(args.uri, serverSelectionTimeoutMS=50000)
        admin_db = client.admin

        # --- ENSURE PRIMARY NODE ---
        hello = admin_db.command("hello")
        if not hello.get("isWritablePrimary"):
            print("[INFO] Node is not PRIMARY, exiting safely")
            return 0

        # --- FETCH LONG RUNNING OPS ---
        pipeline = [
            {"$currentOp": {"allUsers": True, "idleConnections": False}},
            {"$match": {
                "active": True,
                "secs_running": {"$gte": args.seconds},
                "op": {"$ne": "none"},
            }},
        ]

        inprog = list(admin_db.aggregate(pipeline))

        if not inprog:
            print("[INFO] No long-running operations found")
            return 0

        print(f"[INFO] Found {len(inprog)} candidate operations")

        targeted = 0
        killed = 0
        failed = 0

        for op in inprog:
            ns = op.get("ns", "")
            opid = op.get("opid")

            # --- SAFETY FILTERS ---
            reason = skip_reason(op, args.db)
            if reason:
                # Keep the log readable: these two are noise, not decisions.
                if reason not in ("empty namespace", "outside DB filter", "balancer"):
                    print(f"[SKIP] {reason} opid={opid} ns={ns}")
                continue

            targeted += 1

            # --- PRINT OP INFO ---
            print(
                f"\n[OP] opid={opid} "
                f"secs={op.get('secs_running', 0)} "
                f"ns={ns} "
                f"client={op.get('client', 'unknown')}"
            )

            # --- PRINT FULL QUERY ---
            command = (
                op.get("originatingCommand")
                or op.get("command")
                or op.get("query")
                or {}
            )
            if command:
                print("[COMMAND]")
                print(json.dumps(command, indent=4, default=str))

            if args.dry_run:
                continue

            # --- KILL OPERATION ---
            try:
                admin_db.command("killOp", op=opid)
                killed += 1
                print(f"[KILLED] opid={opid}")
            except PyMongoError as e:
                failed += 1
                print(f"[ERROR] Failed to kill opid={opid}: {e}")

        if args.dry_run:
            print(f"\n[INFO] Dry run completed. Ops that WOULD be killed: {targeted}")
        else:
            print(f"\n[INFO] Completed. Targeted: {targeted}, killed: {killed}, failed: {failed}")
        return 0

    except PyMongoError as e:
        print(f"[FATAL] MongoDB error: {e}")
        return 1

    except Exception as e:
        print(f"[FATAL] Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
