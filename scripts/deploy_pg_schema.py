#!/usr/bin/env python3
"""Run the existing owner migration CLI inside the newly built application image."""
import argparse
import os
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", default="5432")
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password-file", required=True, type=Path)
    args = parser.parse_args(argv)
    password = args.password_file.read_text(encoding="utf-8").strip()
    if not password:
        raise ValueError("PG 管理员 secret 为空，拒绝迁移")
    os.environ.update({
        "CONSOLE_STORAGE_ENGINE": "postgres", "CONSOLE_PG_HOST": args.host,
        "CONSOLE_PG_PORT": args.port, "CONSOLE_PG_DB": args.database,
        "CONSOLE_PG_USER": args.user, "CONSOLE_PG_PASSWORD": password,
    })
    # pg_schema owns the migration lock, SET ROLE, transaction, checksums and validation.
    # Do not import app or bootstrap roles on a normal deployment.
    from pg_schema import main as schema_main

    return schema_main(["migrate"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"PG 部署迁移失败（{type(exc).__name__}）", file=sys.stderr)
        sys.exit(1)
