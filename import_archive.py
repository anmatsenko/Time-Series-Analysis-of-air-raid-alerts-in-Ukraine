"""
import_archive.py — one-time historical backfill for the live dashboard
========================================================================

Loads a historical air-raid-alert archive (covering 24 Feb 2022 -> recent)
into the SAME store file the live dashboard reads (alert_history_store.csv),
so past history and live alerts.in.ua data sit in one continuous timeline.

You run this ONCE (or whenever you refresh the archive). After that, the
dashboard's live "Refresh from API" keeps extending the timeline forward.

USAGE
-----
Option A — a file you downloaded (or a URL):
  1. Set SOURCE = "file" and ARCHIVE_PATH to the CSV (local path or https URL).
  2. Check COLUMN_MAP matches your file's headers.
  3. Run:  python import_archive.py

Option B — auto-download from Kaggle:
  1. pip install kagglehub
  2. Get a Kaggle API token: kaggle.com -> Settings -> API ->
     "Create New API Token". Save the downloaded kaggle.json to
     %USERPROFILE%\\.kaggle\\kaggle.json (Windows) or ~/.kaggle/kaggle.json
     (or set KAGGLE_USERNAME and KAGGLE_KEY environment variables).
  3. Set SOURCE = "kaggle" and KAGGLE_DATASET to the "owner/slug" from the URL.
  4. Run:  python import_archive.py
     It downloads the data, prints the columns it found and the column mapping
     it auto-detected, and asks you to confirm before importing.

The importer is source-agnostic: it does not depend on any particular host.
Whatever archive you point it at, it maps the columns into the store schema and
normalizes oblast names to the canonical Ukrainian titles used by the dashboard
(so the map centroids and region filters line up exactly with live data).

NOTE ON ARCHIVE AVAILABILITY (honest caveat)
--------------------------------------------
A genuinely complete, free, daily-updated 2022->today archive is scarce. The
most complete one I know of is GitHub-hosted (the very source you asked to avoid
for live use). air-alarms.in.ua provides historical statistics on request, and
some Kaggle mirrors exist but need a Kaggle login. This script stays neutral:
supply whichever archive you're comfortable with; nothing about the host is
baked in.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pandas as pd
import requests

# --------------------------------------------------------------------------- #
# CONFIG — edit these
# --------------------------------------------------------------------------- #
# Where the archive comes from: "file" (a CSV you downloaded / a URL) or
# "kaggle" (auto-download via the Kaggle API).
SOURCE = "file"

# --- used when SOURCE == "file" ---
# Direct URL to the complete, daily-updated archive (covers 2022 -> today).
# This is the Vadimkin air-raid dataset; the importer reads it straight from the
# URL, so no manual download is needed. Replace with a local path if you prefer.
ARCHIVE_PATH = ("https://raw.githubusercontent.com/Vadimkin/"
                "ukrainian-air-raid-sirens-dataset/main/datasets/official_data_en.csv")

# --- used when SOURCE == "kaggle" ---
# owner/dataset-slug exactly as in the Kaggle URL
# kaggle.com/datasets/<owner>/<slug>.  NOTE: the popular one below republishes
# the same GitHub data discussed earlier — changing host, not provenance.
KAGGLE_DATASET = "cashncarry/airraid-sirens-in-ukraine"
KAGGLE_CSV_NAME = "official_data_en"   # substring to pick which CSV; "" = auto-pick
KAGGLE_DOWNLOAD_DIR = "kaggle_data"
AUTO_CONFIRM = False       # True = skip the interactive "proceed?" prompt

STORE_PATH = Path("alert_history_store.csv")

# Map YOUR archive's column names -> the fields we need.
# Used directly for SOURCE="file"; for SOURCE="kaggle" the columns are
# auto-detected and shown for your confirmation (this stays as a fallback).
# Set a value to None if your file doesn't have that column.
COLUMN_MAP = {
    "oblast":      "oblast",        # region name (any spelling/translit/Ukrainian)
    "raion":       "raion",         # finer location, optional (or None)
    "hromada":     "hromada",       # finest location, optional (or None)
    "level":       "level",         # oblast / raion / hromada / city  (or None)
    "started_at":  "started_at",
    "finished_at": "finished_at",
    "alert_type":  None,            # if None, defaults to "air_raid"
}

# --------------------------------------------------------------------------- #
# Canonical oblast titles (must match the dashboard's OBLASTS table exactly)
# --------------------------------------------------------------------------- #
CANONICAL = [
    "Хмельницька область", "Вінницька область", "Рівненська область",
    "Волинська область", "Дніпропетровська область", "Житомирська область",
    "Закарпатська область", "Запорізька область", "Івано-Франківська область",
    "Київська область", "Кіровоградська область", "Луганська область",
    "Миколаївська область", "Одеська область", "Полтавська область",
    "Сумська область", "Тернопільська область", "Харківська область",
    "Херсонська область", "Черкаська область", "Чернігівська область",
    "Чернівецька область", "Львівська область", "Донецька область",
    "Автономна Республіка Крим", "м. Севастополь", "м. Київ",
]

# Aliases: normalized lowercase key (latin translit OR english root) -> canonical.
# Covers the common transliterated archive spellings.
_ALIASES = {
    "khmelnytska": "Хмельницька область", "khmelnytskyi": "Хмельницька область",
    "vinnytska": "Вінницька область", "vinnytsia": "Вінницька область",
    "rivnenska": "Рівненська область", "rivne": "Рівненська область",
    "volynska": "Волинська область", "volyn": "Волинська область",
    "dnipropetrovska": "Дніпропетровська область", "dnipro": "Дніпропетровська область",
    "zhytomyrska": "Житомирська область", "zhytomyr": "Житомирська область",
    "zakarpatska": "Закарпатська область", "zakarpattia": "Закарпатська область",
    "zaporizka": "Запорізька область", "zaporizhzhia": "Запорізька область",
    "ivano-frankivska": "Івано-Франківська область", "ivano-frankivsk": "Івано-Франківська область",
    "kyivska": "Київська область",
    "kirovohradska": "Кіровоградська область", "kropyvnytskyi": "Кіровоградська область",
    "luhanska": "Луганська область", "luhansk": "Луганська область",
    "mykolaivska": "Миколаївська область", "mykolaiv": "Миколаївська область",
    "odeska": "Одеська область", "odesa": "Одеська область",
    "poltavska": "Полтавська область", "poltava": "Полтавська область",
    "sumska": "Сумська область", "sumy": "Сумська область",
    "ternopilska": "Тернопільська область", "ternopil": "Тернопільська область",
    "kharkivska": "Харківська область", "kharkiv": "Харківська область",
    "khersonska": "Херсонська область", "kherson": "Херсонська область",
    "cherkaska": "Черкаська область", "cherkasy": "Черкаська область",
    "chernivetska": "Чернівецька область", "chernivtsi": "Чернівецька область",
    "chernihivska": "Чернігівська область", "chernihiv": "Чернігівська область",
    "lvivska": "Львівська область", "lviv": "Львівська область",
    "donetska": "Донецька область", "donetsk": "Донецька область",
    "krym": "Автономна Республіка Крим", "crimea": "Автономна Республіка Крим",
    "sevastopol": "м. Севастополь",
    "kyiv": "м. Київ", "kyiv city": "м. Київ", "m. kyiv": "м. Київ",
}
# Ukrainian canonical titles map to themselves too.
for _c in CANONICAL:
    _ALIASES[_c.lower()] = _c


def normalize_oblast(raw: str) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip().lower()
    if s in _ALIASES:
        return _ALIASES[s]
    # strip trailing " oblast" / " область" and retry on the stem
    stem = (s.replace(" oblast", "").replace(" область", "")
              .replace("м. ", "").replace("m. ", "").strip())
    if stem in _ALIASES:
        return _ALIASES[stem]
    # last resort: substring match against english roots
    for key, canon in _ALIASES.items():
        if key and (key in stem or stem in key) and len(stem) >= 4:
            return canon
    return None


def stable_id(title: str, level: str, started: str, finished: str) -> str:
    h = hashlib.md5(f"{title}|{level}|{started}|{finished}".encode("utf-8")).hexdigest()[:16]
    return f"hist_{h}"


def read_source(path: str) -> pd.DataFrame:
    if path.startswith(("http://", "https://")):
        resp = requests.get(path, timeout=120)
        resp.raise_for_status()
        return pd.read_csv(io.StringIO(resp.text))
    return pd.read_csv(path)


# --------------------------------------------------------------------------- #
# Kaggle support
# --------------------------------------------------------------------------- #
def download_from_kaggle(slug: str, dest_dir: str, csv_hint: str) -> Path:
    """Download a Kaggle dataset via kagglehub, return the chosen CSV path.

    Requires:  pip install kagglehub
    And Kaggle credentials, supplied EITHER as a kaggle.json token
    (Kaggle -> Settings -> API -> 'Create New API Token'), saved to:
        Windows:   %USERPROFILE%\\.kaggle\\kaggle.json
        Mac/Linux: ~/.kaggle/kaggle.json
    OR as environment variables KAGGLE_USERNAME and KAGGLE_KEY.
    """
    try:
        import kagglehub
    except ImportError:
        raise SystemExit(
            "The 'kagglehub' package isn't installed. Run:  pip install kagglehub"
        )

    print(f"Downloading Kaggle dataset '{slug}' …")
    try:
        # Returns a local directory containing the dataset's files.
        downloaded = kagglehub.dataset_download(slug, output_dir=dest_dir)
    except Exception as e:
        raise SystemExit(
            "Kaggle download failed. Check that:\n"
            "  1. 'kagglehub' is installed (pip install kagglehub),\n"
            "  2. your credentials are set — kaggle.json in ~/.kaggle/ (or "
            "%USERPROFILE%\\.kaggle\\ on Windows), or KAGGLE_USERNAME/KAGGLE_KEY,\n"
            f"  3. the dataset slug '{slug}' is correct.\n  Details: {e}"
        )

    base = Path(downloaded)
    csvs = sorted(base.glob("**/*.csv")) if base.is_dir() else [base]
    if not csvs:
        raise SystemExit(f"No CSV files found in the Kaggle dataset '{slug}'.")
    if csv_hint:
        matches = [p for p in csvs if csv_hint.lower() in p.name.lower()]
        if matches:
            csvs = matches
    chosen = max(csvs, key=lambda p: p.stat().st_size)  # prefer the biggest CSV
    if len(csvs) > 1:
        print(f"  CSV files available: {[p.name for p in csvs]}")
    print(f"  Using: {chosen.name}")
    return chosen


# Candidate header names (lowercased) for each field we need.
_FIELD_CANDIDATES = {
    "oblast":      ["oblast", "region", "region_title", "location_oblast", "область", "регіон"],
    "raion":       ["raion", "district", "район", "location_raion"],
    "hromada":     ["hromada", "community", "громада", "location_hromada"],
    "level":       ["level", "location_type", "type_level", "рівень"],
    "started_at":  ["started_at", "start", "start_time", "began_at", "start_at",
                    "started", "datetime_start", "time_start"],
    "finished_at": ["finished_at", "end", "end_time", "ended_at", "finish_at",
                    "finished", "finish", "datetime_end", "time_end"],
    "alert_type":  ["alert_type", "type", "alarm_type", "тип"],
}
_REQUIRED = ("oblast", "started_at", "finished_at")


def auto_detect_mapping(columns: list[str]) -> dict:
    lower = {c.lower(): c for c in columns}
    mapping = {}
    for field, cands in _FIELD_CANDIDATES.items():
        mapping[field] = next((lower[c] for c in cands if c in lower), None)
    return mapping


def resolve_kaggle_mapping(columns: list[str]) -> dict:
    mapping = auto_detect_mapping(columns)
    print("\nColumns found in the file:")
    print(f"  {columns}")
    print("\nProposed column mapping (auto-detected):")
    for field, col in mapping.items():
        flag = "  <-- REQUIRED, NOT FOUND" if (field in _REQUIRED and col is None) else ""
        print(f"  {field:<12} -> {col}{flag}")

    missing = [f for f in _REQUIRED if mapping[f] is None]
    if missing:
        raise SystemExit(
            f"\nCouldn't auto-detect required field(s): {missing}.\n"
            "Edit COLUMN_MAP at the top of this file to point at the right column "
            "names, set SOURCE='file', ARCHIVE_PATH to the downloaded CSV, and re-run."
        )

    if not AUTO_CONFIRM:
        ans = input("\nProceed with this mapping? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            raise SystemExit("Stopped. Adjust COLUMN_MAP if needed and re-run.")
    return mapping


def main() -> None:
    if SOURCE == "kaggle":
        csv_path = download_from_kaggle(KAGGLE_DATASET, KAGGLE_DOWNLOAD_DIR, KAGGLE_CSV_NAME)
        src = pd.read_csv(csv_path)
        print(f"\n  {len(src):,} raw rows")
        cm = resolve_kaggle_mapping(list(src.columns))
    else:
        if not ARCHIVE_PATH:
            raise SystemExit("Set ARCHIVE_PATH at the top of this file (or use SOURCE='kaggle').")
        print(f"Reading archive: {ARCHIVE_PATH}")
        src = read_source(ARCHIVE_PATH)
        print(f"  {len(src):,} raw rows, columns: {list(src.columns)}")
        cm = COLUMN_MAP

    out = pd.DataFrame()
    out["location_oblast"] = src[cm["oblast"]].map(normalize_oblast)
    out["location_type"] = (src[cm["level"]].astype(str).str.lower()
                            if cm["level"] else "oblast")
    out["alert_type"] = (src[cm["alert_type"]] if cm["alert_type"] else "air_raid")
    out["started_at"] = pd.to_datetime(src[cm["started_at"]], utc=True, errors="coerce")
    out["finished_at"] = pd.to_datetime(src[cm["finished_at"]], utc=True, errors="coerce")

    unmapped = src.loc[out["location_oblast"].isna(), cm["oblast"]].dropna().unique()
    if len(unmapped):
        print(f"  ⚠ {len(unmapped)} region name(s) could not be normalized and will be "
              f"dropped: {list(unmapped)[:8]}{'…' if len(unmapped) > 8 else ''}")

    out = out.dropna(subset=["location_oblast", "started_at"]).copy()

    # Preserve the FINEST available location name as the title (hromada > raion >
    # oblast), matching how live API rows look and keeping distinct sub-regional
    # alerts distinct. location_oblast stays canonical for map/filter rollup.
    titles = []
    for i in out.index:
        t = out.at[i, "location_oblast"]
        for key in ("hromada", "raion"):
            col = cm.get(key)
            if col and col in src.columns:
                val = src.at[i, col]
                if isinstance(val, str) and val.strip():
                    t = val.strip()
                    break
        titles.append(t)
    out["location_title"] = titles

    out["updated_at"] = out["finished_at"].fillna(out["started_at"])
    out["calculated"] = False
    out["id"] = [
        stable_id(t, l, s.isoformat(), f.isoformat() if pd.notna(f) else "")
        for t, l, s, f in zip(out["location_title"], out["location_type"],
                              out["started_at"], out["finished_at"])
    ]

    store_cols = ["id", "location_title", "location_type", "location_oblast",
                  "alert_type", "started_at", "finished_at", "updated_at", "calculated"]
    out = out[store_cols]

    # Merge into existing store (idempotent: re-running won't duplicate).
    if STORE_PATH.exists():
        existing = pd.read_csv(STORE_PATH, parse_dates=["started_at", "finished_at", "updated_at"])
        before = len(existing)
        combined = (pd.concat([existing, out], ignore_index=True)
                    .sort_values("updated_at").drop_duplicates("id", keep="last"))
        added = len(combined) - before
    else:
        combined = out.drop_duplicates("id", keep="last")
        added = len(combined)

    combined.to_csv(STORE_PATH, index=False)
    print(f"\nImported {len(out):,} historical alerts "
          f"({added:,} new). Store now holds {len(combined):,} rows.")
    print(f"  Date range: {combined['started_at'].min()} -> {combined['started_at'].max()}")
    print(f"  Saved to: {STORE_PATH.resolve()}")
    print("\nNext: open the dashboard — history + live now share one timeline.")


if __name__ == "__main__":
    main()
