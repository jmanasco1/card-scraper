"""Load the JSONL partitions into SQLite for ad-hoc querying.

    python -m ebay_scanner.load_sqlite [--db cards.db] [--rebuild]

Idempotent: re-running upserts on itemId, so it is safe to point at the whole
data/ directory after every scan.
"""
import argparse
import json
import sqlite3

from . import config
from .fields import ASPECT_COLUMNS

COLUMNS = [
    ("itemId", "TEXT PRIMARY KEY"),
    ("legacyItemId", "TEXT"),
    ("title", "TEXT"),
    ("price", "REAL"),
    ("currency", "TEXT"),
    ("itemWebUrl", "TEXT"),
    ("itemCreationDate", "TEXT"),
    ("sellerUsername", "TEXT"),
    ("sellerFeedbackScore", "INTEGER"),
    ("sellerFeedbackPercentage", "TEXT"),
    ("condition", "TEXT"),
    ("conditionId", "TEXT"),
    ("imageUrl", "TEXT"),
    ("categoryId", "TEXT"),
    ("epid", "TEXT"),
    ("firstSeenAt", "TEXT"),
    ("detailFetched", "INTEGER"),
    ("aspectCount", "INTEGER"),
] + [(name, "TEXT") for name in ASPECT_COLUMNS] + [
    ("buyingOptions", "TEXT"),
    ("aspects_json", "TEXT"),
]


def build_schema(conn):
    cols = ", ".join(f'"{name}" {sqltype}' for name, sqltype in COLUMNS)
    conn.execute(f"CREATE TABLE IF NOT EXISTS listings ({cols})")
    for index in ["price", "grader", "grade", "itemCreationDate", "sellerUsername"]:
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS idx_listings_{index} ON listings("{index}")'
        )
    conn.commit()


def row_from(record):
    values = []
    for name, _ in COLUMNS:
        if name == "aspects_json":
            values.append(json.dumps(record.get("aspects") or {}, sort_keys=True))
        elif name == "buyingOptions":
            options = record.get("buyingOptions")
            values.append(",".join(options) if isinstance(options, list) else options)
        elif name == "detailFetched":
            values.append(1 if record.get("detailFetched") else 0)
        else:
            values.append(record.get(name))
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(config.ROOT / "cards.db"))
    parser.add_argument("--rebuild", action="store_true",
                        help="drop the listings table before loading")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    if args.rebuild:
        conn.execute("DROP TABLE IF EXISTS listings")
        conn.commit()
    build_schema(conn)

    placeholders = ", ".join("?" for _ in COLUMNS)
    names = ", ".join(f'"{name}"' for name, _ in COLUMNS)
    sql = f"INSERT OR REPLACE INTO listings ({names}) VALUES ({placeholders})"

    loaded = 0
    files = sorted(config.DATA_DIR.glob("*.jsonl"))
    for path in files:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                conn.execute(sql, row_from(json.loads(line)))
                loaded += 1
        conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"Loaded {loaded} rows from {len(files)} file(s) into {args.db}")
    print(f"listings table now holds {count} unique itemIds")

    graded = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE grader IS NOT NULL"
    ).fetchone()[0]
    if count:
        print(f"rows with a Professional Grader aspect: {graded} "
              f"({graded / count * 100:.0f}%)")
    conn.close()


if __name__ == "__main__":
    main()
