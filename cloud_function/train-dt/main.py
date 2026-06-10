# train-dt/main.py
# Champion vs Challenger price prediction.
#
# Champions: pre-trained models loaded from GCS (never retrained automatically)
#   - champion_regex : trained on 4,185 regex-extracted listings (Nov 2025-May 2026)
#   - champion_llm   : trained on 867 LLM-extracted listings (Dec 2025-Apr 2026)
#
# Challengers: retrained fresh each run on the latest master CSVs
#   - challenger_regex : listings_master.csv       (regex fields)
#   - challenger_llm   : listings_master_llm_v1.csv (LLM fields)
#
# All four models score today's holdout. Results written to GCS as:
#   structured/preds/<YYYYMMDDHH>/preds.csv          (champion_regex predictions)
#   structured/preds/<YYYYMMDDHH>/comparison.csv     (all 4 models side by side)

import io
import json
import logging
import os
import traceback
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
from google.cloud import storage
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import mean_absolute_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.tree import DecisionTreeRegressor

# ── env ───────────────────────────────────────────────────────────────────────
PROJECT_ID        = os.getenv("PROJECT_ID", "")
GCS_BUCKET        = os.getenv("GCS_BUCKET", "")
STRUCTURED_PREFIX = os.getenv("STRUCTURED_PREFIX", "structured")
OUTPUT_PREFIX     = os.getenv("OUTPUT_PREFIX", "structured/preds")
TIMEZONE          = os.getenv("TIMEZONE", "America/New_York")
LOG_LEVEL         = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")

# ── GCS paths ─────────────────────────────────────────────────────────────────
REGEX_MASTER_KEY   = f"{STRUCTURED_PREFIX}/datasets/listings_master.csv"
LLM_MASTER_KEY     = f"{STRUCTURED_PREFIX}/datasets/listings_master_llm_v1.csv"
CHAMPION_REGEX_KEY = "models/champion_regex.pkl"
CHAMPION_LLM_KEY   = "models/champion_llm.pkl"

# ── feature sets ──────────────────────────────────────────────────────────────
REGEX_CAT = ["make", "model", "transmission", "color", "fuel", "vehicle_type"]
LLM_CAT   = ["make", "model", "transmission", "color"]
NUM_COLS  = ["year_n", "mileage_n"]
TARGET    = "price_n"

MAX_DEPTH        = 12
MIN_SAMPLES_LEAF = 10
RANDOM_STATE     = 42


# ── helpers ───────────────────────────────────────────────────────────────────

def _read_csv(client, bucket, key):
    b = client.bucket(bucket)
    blob = b.blob(key)
    if not blob.exists():
        return None
    return pd.read_csv(io.BytesIO(blob.download_as_bytes()))


def _write_csv(client, bucket, key, df):
    client.bucket(bucket).blob(key).upload_from_string(
        df.to_csv(index=False), content_type="text/csv")


def _load_model(client, bucket, key):
    blob = client.bucket(bucket).blob(key)
    if not blob.exists():
        return None
    return joblib.load(io.BytesIO(blob.download_as_bytes()))


def _clean(df, cat_cols):
    df = df.copy()
    df["price_n"] = pd.to_numeric(
        df["price"].astype(str).str.replace(r"[^\d.]", "", regex=True), errors="coerce")
    df["year_n"] = pd.to_numeric(df.get("year"), errors="coerce")
    df["mileage_n"] = pd.to_numeric(
        df.get("mileage", pd.Series(dtype=float)).astype(str).str.replace(r"[^\d.]", "", regex=True),
        errors="coerce")
    for c in cat_cols:
        if c not in df.columns:
            df[c] = np.nan
    return df[df[TARGET].between(500, 150_000)].copy()


def _date_split(df, timezone_str):
    dt = pd.to_datetime(df.get("scraped_at", pd.Series(dtype=str)), errors="coerce", utc=True)
    df["_dt_utc"] = dt
    try:
        df["_date_local"] = df["_dt_utc"].dt.tz_convert(timezone_str).dt.date
    except Exception:
        df["_date_local"] = df["_dt_utc"].dt.date
    unique_dates = sorted(df["_date_local"].dropna().unique())
    if len(unique_dates) < 2:
        return None, None, None
    today = unique_dates[-1]
    return (df[df["_date_local"] < today].copy(),
            df[df["_date_local"] == today].copy(),
            str(today))


def _build_pipeline(cat_cols):
    pre = ColumnTransformer(transformers=[
        ("num", SimpleImputer(strategy="median"), NUM_COLS),
        ("cat", Pipeline([
            ("imp", SimpleImputer(strategy="most_frequent")),
            ("oh",  OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
        ]), cat_cols),
    ], remainder="drop")
    return Pipeline([("pre", pre), ("model", DecisionTreeRegressor(
        max_depth=MAX_DEPTH, min_samples_leaf=MIN_SAMPLES_LEAF, random_state=RANDOM_STATE))])


def _score(pipe, holdout, cat_cols, name):
    if pipe is None or holdout is None or holdout.empty:
        return None, None
    feats = cat_cols + NUM_COLS
    for c in feats:
        if c not in holdout.columns:
            holdout[c] = np.nan
    try:
        y_hat = pipe.predict(holdout[feats])
        mae = None
        if holdout[TARGET].notna().any():
            mask = holdout[TARGET].notna()
            mae = float(mean_absolute_error(holdout[TARGET][mask], y_hat[mask]))
        return np.round(y_hat, 2), mae
    except Exception as e:
        logging.warning("Scoring failed for %s: %s", name, e)
        return None, None


# ── entrypoint ────────────────────────────────────────────────────────────────

def train_dt_http(request):
    try:
        body    = request.get_json(silent=True) or {}
        dry_run = bool(body.get("dry_run", False))

        if not GCS_BUCKET:
            return _resp({"ok": False, "error": "missing GCS_BUCKET env"}, 500)

        client  = storage.Client(project=PROJECT_ID)
        now_utc = datetime.now(timezone.utc)
        out_folder = f"{OUTPUT_PREFIX}/{now_utc.strftime('%Y%m%d%H')}"

        result = {
            "ok": True,
            "run_at": now_utc.isoformat(),
            "dry_run": dry_run,
            "models": {}
        }

        # load champions
        logging.info("Loading champions from GCS...")
        champ_regex = _load_model(client, GCS_BUCKET, CHAMPION_REGEX_KEY)
        champ_llm   = _load_model(client, GCS_BUCKET, CHAMPION_LLM_KEY)
        result["champion_regex_loaded"] = champ_regex is not None
        result["champion_llm_loaded"]   = champ_llm is not None

        comparison_rows = []

        for label, master_key, cat_cols, champ in [
            ("regex", REGEX_MASTER_KEY, REGEX_CAT, champ_regex),
            ("llm",   LLM_MASTER_KEY,   LLM_CAT,   champ_llm),
        ]:
            df_raw = _read_csv(client, GCS_BUCKET, master_key)
            if df_raw is None:
                result["models"][label] = {"status": "no_data"}
                continue

            df = _clean(df_raw, cat_cols)
            train, holdout, today_str = _date_split(df, TIMEZONE)
            m = {"today": today_str}

            if train is None or len(train) < 40:
                m["status"] = "too_few_rows"
                result["models"][label] = m
                continue

            # train challenger
            challenger = _build_pipeline(cat_cols)
            challenger.fit(train[cat_cols + NUM_COLS], train[TARGET])
            train_mae = float(mean_absolute_error(
                train[TARGET], challenger.predict(train[cat_cols + NUM_COLS])))

            m["challenger_train_rows"] = int(len(train))
            m["challenger_train_mae"]  = round(train_mae, 2)
            m["holdout_rows"]          = int(len(holdout)) if holdout is not None else 0

            # score all models on holdout
            champ_preds, champ_mae = _score(champ,      holdout.copy(), cat_cols, f"champion_{label}")
            chal_preds,  chal_mae  = _score(challenger,  holdout.copy(), cat_cols, f"challenger_{label}")

            m["champion_mae"]   = round(champ_mae, 2) if champ_mae is not None else None
            m["challenger_mae"] = round(chal_mae,  2) if chal_mae  is not None else None
            m["status"] = "ok"

            if champ_mae and chal_mae:
                diff = champ_mae - chal_mae
                m["winner"] = "challenger" if diff > 0 else "champion"
                m["challenger_beats_champion_by"] = round(diff, 2)
                logging.info("%s: champion MAE=$%,.0f  challenger MAE=$%,.0f  winner=%s",
                             label, champ_mae, chal_mae, m["winner"])

            result["models"][label] = m

            # build comparison rows for CSV
            if holdout is not None and not holdout.empty:
                base = [c for c in ["post_id","scraped_at","make","model","year","mileage","price"]
                        if c in holdout.columns]
                rows = holdout[base].copy()
                rows["actual_price"] = holdout[TARGET]
                if champ_preds is not None:
                    rows[f"pred_champion_{label}"]  = champ_preds
                if chal_preds is not None:
                    rows[f"pred_challenger_{label}"] = chal_preds
                comparison_rows.append(rows)

        # write outputs
        if not dry_run and comparison_rows:
            # merge all comparison frames on shared base columns
            combined = comparison_rows[0]
            for extra in comparison_rows[1:]:
                shared = [c for c in combined.columns if c in extra.columns
                          and not c.startswith("pred_")]
                combined = pd.merge(combined, extra, on=shared, how="outer")

            comp_key = f"{out_folder}/comparison.csv"
            _write_csv(client, GCS_BUCKET, comp_key, combined)
            result["comparison_csv"] = f"gs://{GCS_BUCKET}/{comp_key}"

            # backwards-compat preds.csv (champion_regex)
            regex_rows = [r for r in comparison_rows if "pred_champion_regex" in r.columns]
            if regex_rows:
                preds_key = f"{out_folder}/preds.csv"
                _write_csv(client, GCS_BUCKET, preds_key, regex_rows[0])
                result["preds_csv"] = f"gs://{GCS_BUCKET}/{preds_key}"

        return _resp(result, 200)

    except Exception as e:
        logging.error("Error: %s\n%s", e, traceback.format_exc())
        return _resp({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


def _resp(body, code):
    return (json.dumps(body, default=str), code, {"Content-Type": "application/json"})
