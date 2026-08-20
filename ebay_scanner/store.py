"""Date-partitioned JSONL storage with itemId dedupe."""
import json
from datetime import datetime, timezone

from . import config


def _files():
    if not config.DATA_DIR.exists():
        return []
    return sorted(config.DATA_DIR.glob("*.jsonl"))


def load_index():
    """Return (seen itemIds, newest itemCreationDate) across all partitions.

    The newest stored listing time is what makes a coverage gap detectable:
    if a run's oldest fetched listing is newer than this, listings appeared
    and disappeared from the pagination window between runs.
    """
    seen = set()
    newest = None
    for path in _files():
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    # A truncated final line shouldn't take out the whole run.
                    print(f"[store] skipping unparseable line in {path.name}")
                    continue
                item_id = record.get("itemId")
                if item_id:
                    seen.add(item_id)
                created = record.get("itemCreationDate")
                if created and (newest is None or created > newest):
                    newest = created
    return seen, newest


def load_seen_ids():
    return load_index()[0]


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
