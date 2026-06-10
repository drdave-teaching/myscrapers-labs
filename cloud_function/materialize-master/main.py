# main.py
# Incrementally append new listings to listings_master.csv.
# Reads:  gs://<bucket>/<STRUCTURED_PREFIX>/run_id=*/jsonl/*.jsonl  (recent runs only)
# Merges: existing listings_master.csv (loads post_ids already seen)
# Writes: gs://<bucket>/<STRUCTURED_PREFIX>/datasets/listings_master.csv

import csv
import io
import json
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Iterable

from flask import Request, jsonify
from google.cloud import storage

# -------------------- ENV --------------------
BUCKET_NAME        = os.getenv("GCS_BUCKET")                      # REQUIRED
STRUCTURED_PREFIX  = os.getenv("STRUCTURED_PREFIX", "structured") # e.g., "structured"

storage_client = storage.Client()

# Accept BOTH runIDs:
RUN_ID_ISO_RE   = re.compile(r"^\d{8}T\d{6}Z$")  # 20251026T170002Z
RUN_ID_PLAIN_RE = re.compile(r"^\d{14}$")        # 20251026170002

# Stable CSV schema for students
CSV_COLUMNS = [
    "post_id", "run_id", "scraped_at",
    "price", "year", "make", "model", "mileage",
    "source_txt"
]

def _list_run_ids(bucket: str, structured_prefix: str) -> list[str]:
    it = storage_client.list_blobs(bucket, prefix=f"{structured_prefix}/", delimiter="/")
    for _ in it:
        pass
    run_ids = []
    for p in getattr(it, "prefixes", []):
        tail = p.rstrip("/").split("/")[-1]
        if tail.startswith("run_id="):
            rid = tail.split("run_id=", 1)[1]
            if RUN_ID_ISO_RE.match(rid) or RUN_ID_PLAIN_RE.match(rid):
                run_ids.append(rid)
    return sorted(run_ids)

def _jsonl_records_for_run(bucket: str, structured_prefix: str, run_id: str):
    """Yield dict records from .jsonl under .../run_id=<run_id>/jsonl/"""
    b = storage_client.bucket(bucket)
    prefix = f"{structured_prefix}/run_id={run_id}/jsonl/"
    for blob in b.list_blobs(prefix=prefix):
        if not blob.name.endswith(".jsonl"):
            continue
        data = blob.download_as_text()
        line = data.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            rec.setdefault("run_id", run_id)
            yield rec
        except Exception:
            continue

def _run_id_to_dt(rid: str) -> datetime:
    if RUN_ID_ISO_RE.match(rid):
        return datetime.strptime(rid, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    if RUN_ID_PLAIN_RE.match(rid):
        return datetime.strptime(rid, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)

def _open_gcs_text_writer(bucket: str, key: str):
    b = storage_client.bucket(bucket)
    blob = b.blob(key)
    return blob.open("w")

def _write_csv(records: Iterable[Dict], dest_key: str, columns=CSV_COLUMNS) -> int:
    n = 0
    with _open_gcs_text_writer(BUCKET_NAME, dest_key) as out:
        w = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for rec in records:
            row = {c: rec.get(c, None) for c in columns}
            w.writerow(row)
            n += 1
    return n

def _get_existing_master(bucket_name: str, key: str) -> Dict[str, Dict]:
    """Load existing master CSV into a dict keyed by post_id. Returns {} if file doesn't exist yet."""
    b = storage_client.bucket(bucket_name)
    blob = b.blob(key)
    data = {}
    if not blob.exists():
        return data
    try:
        content = blob.download_as_text()
        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            pid = row.get("post_id")
            if pid:
                data[pid] = row
    except Exception:
        pass
    return data

def materialize_http(request: Request):
    """
    HTTP POST (no body needed).
    Looks back 75 minutes for new runs, loads existing master CSV,
    merges new records (de-duped by post_id, keep newest run_id),
    and writes back to listings_master.csv -- never rescans old data.
    """
    try:
        if not BUCKET_NAME:
            return jsonify({"ok": False, "error": "missing GCS_BUCKET env"}), 500

        # Only scan runs from the last 75 minutes (catches previous hour's run)
        all_run_ids = _list_run_ids(BUCKET_NAME, STRUCTURED_PREFIX)
        limit_time  = datetime.now(timezone.utc) - timedelta(minutes=75)
        recent_runs = [r for r in all_run_ids if _run_id_to_dt(r) > limit_time]

        if not recent_runs:
            return jsonify({"ok": True, "message": "No new runs in the last 75 minutes"}), 200

        final_key = f"{STRUCTURED_PREFIX}/datasets/listings_master.csv"

        # Load what we already have so we don't reprocess old listings
        master: Dict[str, Dict] = _get_existing_master(BUCKET_NAME, final_key)
        existing_count = len(master)

        # Merge in only new/updated records
        for rid in recent_runs:
            for rec in _jsonl_records_for_run(BUCKET_NAME, STRUCTURED_PREFIX, rid):
                pid = rec.get("post_id")
                if not pid:
                    continue
                prev = master.get(pid)
                if (prev is None) or (_run_id_to_dt(rid) >= _run_id_to_dt(prev.get("run_id", ""))):
                    master[pid] = rec

        rows = _write_csv(master.values(), final_key)

        return jsonify({
            "ok": True,
            "recent_runs_scanned": len(recent_runs),
            "existing_listings": existing_count,
            "total_listings": rows,
            "new_or_updated": rows - existing_count,
            "output_csv": f"gs://{BUCKET_NAME}/{final_key}"
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
