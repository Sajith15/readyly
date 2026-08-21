"""Create the Stash schema.

Idempotent: every statement in schema.sql is CREATE ... IF NOT EXISTS, so this
runs safely as part of the Render build command on every deploy.

    python -m scripts.init_db
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def main() -> int:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is not set; cannot initialise the schema.", file=sys.stderr)
        return 1

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        conn.commit()

    print("Schema is up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
