"""Date-partitioned JSONL storage with itemId dedupe."""
import json
from datetime import datetime, timezone

from . import config


def _files():
    if not config.DATA_DIR.exists():
        return []
    return sorted(config.DATA_DIR.glob("*.jsonl"))


def load_seen_ids():
    """Every itemId already stored, across all partitions."""
    seen = set()
    for path in _files():
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item_id = json.loads(line).get("itemId")
                except ValueError:
                    # A truncated final line shouldn't take out the whole run.
                    print(f"[store] skipping unparseable line in {path.name}")
                    continue
                if item_id:
                    seen.add(item_id)
    return seen


def total_stored():
    return sum(
        1
        for path in _files()
        for line in open(path)
        if line.strip()
    )


def append(records, today=None):
    """Append records to data/YYYY-MM-DD.jsonl. Returns the partition path."""
    if not records:
        return None
    day = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DATA_DIR / f"{day}.jsonl"
    with open(path, "a") as fh:
        for record in records:
            # sort_keys keeps diffs stable run to run.
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    return path
