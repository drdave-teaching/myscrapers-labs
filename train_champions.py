"""
train_champions.py
------------------
Production script: raw scrapes -> champion models -> GCS upload.

Steps:
  1. Extract structured fields from scrapes_backup.zip (regex)
  2. Merge LLM CSV baselines
  3. Train champion_regex and champion_llm
  4. Save .pkl files locally
  5. Upload models + baselines to GCS

Usage:
  python train_champions.py                        # full run + GCS upload
  python train_champions.py --dry-run              # train locally, skip upload
  python train_champions.py --skip-extract         # skip extraction (reuse existing CSVs)

Requirements:
  pip install scikit-learn pandas numpy joblib google-cloud-storage
"""

import argparse
import csv
import glob
import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import cross_val_score, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.tree import DecisionTreeRegressor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── config ────────────────────────────────────────────────────────────────────
DOWNLOADS   = r"C:\Users\dww05002\Downloads"
ZIP_PATH    = rf"{DOWNLOADS}\scrapes_backup.zip"
MODELS_DIR  = rf"{DOWNLOADS}\models"
OUT_REGEX   = rf"{DOWNLOADS}\baseline_regex_clean.csv"
OUT_LLM     = rf"{DOWNLOADS}\baseline_llm_clean.csv"

GCS_BUCKET  = "craigslist-scraper-bucket"
GCS_PROJECT = "craigslist-scraper-499015"

REGEX_CAT    = ["make", "model", "transmission", "color", "fuel", "vehicle_type"]
LLM_CAT      = ["make", "model", "transmission", "color"]
NUM_COLS     = ["year_n", "mileage_n"]
TARGET       = "price_n"
RANDOM_STATE = 42
MAX_DEPTH    = 12
MIN_LEAF     = 10


# ── regex patterns ────────────────────────────────────────────────────────────
RE_PRICE        = re.compile(r"\n\$([\d,]+)\n")
RE_YEAR_MAKE    = re.compile(r"^\s*(\d{4})\s*\n\s*([a-z0-9 &\-/.]+)\n", re.IGNORECASE | re.MULTILINE)
RE_ODOMETER     = re.compile(r"odometer:\s*\n?\s*([^\n]+)", re.IGNORECASE)
RE_TRANSMISSION = re.compile(r"transmission:\s*\n?\s*([^\n]+)", re.IGNORECASE)
RE_COLOR        = re.compile(r"paint color:\s*\n?\s*([^\n]+)", re.IGNORECASE)
RE_CONDITION    = re.compile(r"condition:\s*\n?\s*([^\n]+)", re.IGNORECASE)
RE_FUEL         = re.compile(r"fuel:\s*\n?\s*([^\n]+)", re.IGNORECASE)
RE_TYPE         = re.compile(r"type:\s*\n?\s*([^\n]+)", re.IGNORECASE)
RE_POST_ID      = re.compile(r"post id:\s*(\d+)", re.IGNORECASE)
RE_POSTED       = re.compile(r"Posted\s*\n\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def _parse_txt(text, run_id, pid_path):
    def _num(s): return re.sub(r"[^\d]", "", s or "")
    def _m(p): m = p.search(text); return m.group(1).strip() if m else ""

    pid = _m(RE_POST_ID) or pid_path
    m_ym = RE_YEAR_MAKE.search(text)
    year = m_ym.group(1) if m_ym else ""
    parts = (m_ym.group(2).strip().lower().split(None, 1)) if m_ym else []

    return {
        "post_id":      pid,
        "run_id":       run_id,
        "scraped_at":   _m(RE_POSTED),
        "price":        _num(_m(RE_PRICE)),
        "year":         year,
        "make":         parts[0].title() if parts else "",
        "model":        parts[1].title() if len(parts) > 1 else "",
        "mileage":      _num(_m(RE_ODOMETER)),
        "transmission": _m(RE_TRANSMISSION).lower(),
        "color":        _m(RE_COLOR).lower(),
        "condition":    _m(RE_CONDITION).lower(),
        "fuel":         _m(RE_FUEL).lower(),
        "vehicle_type": _m(RE_TYPE).lower(),
        "source_txt":   text[:300].replace("\n", " "),
    }


# ── step 1: extract ───────────────────────────────────────────────────────────
def extract_regex():
    logging.info("Opening %s", ZIP_PATH)
    seen, records = set(), []

    with zipfile.ZipFile(ZIP_PATH) as z:
        txt_files = [n for n in z.namelist() if n.endswith(".txt")]
        logging.info("Found %d .txt files", len(txt_files))

        for i, name in enumerate(txt_files):
            if i % 25000 == 0:
                logging.info("  %d / %d  (%d unique)", i, len(txt_files), len(records))
            parts = name.split("/")
            run_id   = parts[1] if len(parts) >= 2 else ""
            pid_path = Path(parts[-1]).stem
            try:
                raw = z.read(name).decode("utf-8", errors="replace")
                rec = _parse_txt(raw, run_id, pid_path)
            except Exception:
                continue
            pid = rec["post_id"]
            if pid and pid not in seen:
                seen.add(pid)
                records.append(rec)

    df = pd.DataFrame(records)
    df["price_n"]   = pd.to_numeric(df["price"], errors="coerce")
    df["year_n"]    = pd.to_numeric(df["year"],  errors="coerce")
    df["mileage_n"] = pd.to_numeric(df["mileage"], errors="coerce")

    clean = df[
        df["price_n"].between(500, 150_000) &
        df["year_n"].between(1980, 2027) &
        df["make"].notna() & (df["make"] != "")
    ].copy()

    clean.to_csv(OUT_REGEX, index=False)
    logging.info("Regex baseline: %d unique -> %d clean -> %s", len(df), len(clean), OUT_REGEX)
    return clean


# ── step 2: merge llm ────────────────────────────────────────────────────────
def merge_llm():
    files = sorted(glob.glob(rf"{DOWNLOADS}\structured_datasets_listings_master_llm*.csv"))
    logging.info("Found %d LLM files", len(files))

    combined = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    combined["_filled"] = combined.notna().sum(axis=1)
    combined = (combined.sort_values("_filled", ascending=False)
                        .drop_duplicates(subset="post_id", keep="first")
                        .drop(columns=["_filled"])
                        .sort_values("scraped_at").reset_index(drop=True))

    combined["price_n"] = pd.to_numeric(
        combined["price"].astype(str).str.replace(r"[^\d]", "", regex=True), errors="coerce")
    combined["year_n"]  = pd.to_numeric(combined["year"], errors="coerce")

    clean = combined[
        combined["price_n"].between(500, 150_000) &
        combined["year_n"].between(1980, 2027) &
        combined["make"].notna()
    ].copy()

    clean.to_csv(OUT_LLM, index=False)
    logging.info("LLM baseline: %d unique -> %d clean -> %s", len(combined), len(clean), OUT_LLM)
    return clean


# ── step 3: train ─────────────────────────────────────────────────────────────
def build_pipeline(cat_cols):
    pre = ColumnTransformer(transformers=[
        ("num", SimpleImputer(strategy="median"), NUM_COLS),
        ("cat", Pipeline([
            ("imp", SimpleImputer(strategy="most_frequent")),
            ("oh",  OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
        ]), cat_cols),
    ], remainder="drop")
    return Pipeline([("pre", pre), ("model", DecisionTreeRegressor(
        max_depth=MAX_DEPTH, min_samples_leaf=MIN_LEAF, random_state=RANDOM_STATE))])


def train_champion(df, cat_cols, name):
    df = df.copy()
    for c in cat_cols:
        if c not in df.columns:
            df[c] = np.nan

    X, y = df[cat_cols + NUM_COLS], df[TARGET]
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=RANDOM_STATE)

    pipe = build_pipeline(cat_cols)
    pipe.fit(X_tr, y_tr)

    test_mae = float(mean_absolute_error(y_te, pipe.predict(X_te)))
    test_r2  = float(r2_score(y_te, pipe.predict(X_te)))
    cv       = -cross_val_score(pipe, X, y, cv=5, scoring="neg_mean_absolute_error")

    metrics = {
        "champion":    name,
        "trained_at":  datetime.now(timezone.utc).isoformat(),
        "train_rows":  int(len(X_tr)),
        "test_rows":   int(len(X_te)),
        "train_mae":   round(float(mean_absolute_error(y_tr, pipe.predict(X_tr))), 2),
        "test_mae":    round(test_mae, 2),
        "test_r2":     round(test_r2, 4),
        "cv_mae_mean": round(float(cv.mean()), 2),
        "cv_mae_std":  round(float(cv.std()), 2),
        "price_median": round(float(y.median()), 2),
    }

    logging.info("%s: train_rows=%d  test_mae=$%,.0f  R2=%.3f  cv=$%,.0f+/-$%,.0f",
                 name, len(X_tr), test_mae, test_r2, cv.mean(), cv.std())
    return pipe, metrics


# ── step 4: upload ────────────────────────────────────────────────────────────
def upload_to_gcs(local_path, gcs_key):
    from google.cloud import storage
    client = storage.Client(project=GCS_PROJECT)
    client.bucket(GCS_BUCKET).blob(gcs_key).upload_from_filename(local_path)
    logging.info("Uploaded: gs://%s/%s", GCS_BUCKET, gcs_key)


# ── main ──────────────────────────────────────────────────────────────────────
def main(dry_run=False, skip_extract=False):
    Path(MODELS_DIR).mkdir(parents=True, exist_ok=True)

    # step 1 & 2: data
    if skip_extract and Path(OUT_REGEX).exists():
        logging.info("Skipping extraction, loading existing CSVs")
        df_regex = pd.read_csv(OUT_REGEX)
        df_llm   = pd.read_csv(OUT_LLM)
        df_regex["price_n"]   = pd.to_numeric(df_regex["price"], errors="coerce")
        df_regex["year_n"]    = pd.to_numeric(df_regex["year"],  errors="coerce")
        df_regex["mileage_n"] = pd.to_numeric(df_regex["mileage"], errors="coerce")
        df_llm["price_n"]     = pd.to_numeric(df_llm["price"], errors="coerce")
        df_llm["year_n"]      = pd.to_numeric(df_llm["year"],  errors="coerce")
        df_llm["mileage_n"]   = pd.to_numeric(
            df_llm["mileage"].astype(str).str.replace(r"[^\d.]","",regex=True), errors="coerce")
    else:
        df_regex = extract_regex()
        df_llm   = merge_llm()

    # step 3: train
    pipe_regex, metrics_regex = train_champion(df_regex, REGEX_CAT, "champion_regex")
    pipe_llm,   metrics_llm   = train_champion(df_llm,   LLM_CAT,   "champion_llm")

    # step 4: save locally
    regex_pkl    = rf"{MODELS_DIR}\champion_regex.pkl"
    llm_pkl      = rf"{MODELS_DIR}\champion_llm.pkl"
    metrics_path = rf"{MODELS_DIR}\champion_metrics.json"

    joblib.dump(pipe_regex, regex_pkl)
    joblib.dump(pipe_llm,   llm_pkl)

    all_metrics = [metrics_regex, metrics_llm]
    with open(metrics_path, "w") as f:
        json.dump(all_metrics, f, indent=2)

    logging.info("Saved locally: %s, %s, %s", regex_pkl, llm_pkl, metrics_path)

    # step 5: upload
    if dry_run:
        logging.info("Dry run -- skipping GCS upload")
    else:
        logging.info("Uploading to GCS...")
        upload_to_gcs(regex_pkl,    "models/champion_regex.pkl")
        upload_to_gcs(llm_pkl,      "models/champion_llm.pkl")
        upload_to_gcs(metrics_path, "models/champion_metrics.json")
        upload_to_gcs(OUT_REGEX,    "structured/datasets/baseline_regex_clean.csv")
        upload_to_gcs(OUT_LLM,      "structured/datasets/baseline_llm_clean.csv")
        logging.info("All uploads complete.")

    print(f"\nDone.")
    print(f"  champion_regex  test MAE=${metrics_regex['test_mae']:,.0f}  R2={metrics_regex['test_r2']:.3f}")
    print(f"  champion_llm    test MAE=${metrics_llm['test_mae']:,.0f}  R2={metrics_llm['test_r2']:.3f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--skip-extract", action="store_true",
                        help="Skip ZIP extraction, reuse existing baseline CSVs")
    args = parser.parse_args()
    main(dry_run=args.dry_run, skip_extract=args.skip_extract)
