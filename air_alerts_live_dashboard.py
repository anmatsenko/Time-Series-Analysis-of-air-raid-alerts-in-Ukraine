"""
Air Raid Alerts in Ukraine — Live Dashboard (alerts.in.ua API)
==============================================================

Near-real-time analysis of air raid alerts across Ukraine, with an
interval-merged map view by oblast, national/regional time series, and
filters for period and region.

DATA SOURCE
-----------
Live data from the alerts.in.ua public API (https://devs.alerts.in.ua/).
You need a free personal token: request one at https://alerts.in.ua/api-request
Set it as an environment variable before running:

    export ALERTS_IN_UA_TOKEN="your_token_here"      # macOS / Linux
    setx  ALERTS_IN_UA_TOKEN "your_token_here"        # Windows (new shell after)

Run:
    pip install streamlit pandas plotly requests
    streamlit run air_alerts_live_dashboard.py

WHAT THIS API CAN AND CANNOT DO  (important — read this)
--------------------------------------------------------
* /v1/alerts/active.json   -> all alerts active *right now*.
* /v1/regions/{uid}/alerts/month_ago.json -> history, but ONLY a trailing
  ~30 days, and rate-limited to 2 requests/minute.
* There is NO endpoint that returns the full timeline back to 24 Feb 2022.
  Live APIs give you "now" plus a short tail. That is a property of the
  source, not of this code.

How the "since 2022 / updates for future alerts" requirement is handled:
This app keeps its OWN growing history file (alert_history_store.csv). Every
refresh appends new/updated alerts, so the longer you run it the longer your
local timeline becomes. The trailing month can be backfilled from the API on
first run; anything before that would need a one-time import from a historical
archive (the API itself cannot reach back that far).

DEMO MODE
---------
With no token set, the app runs on synthetic data so you can see the UI and
the interval-merged map immediately. Toggle it off once your token is set.
"""

from __future__ import annotations

import os
import time
import random
import datetime as dt
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

# --------------------------------------------------------------------------- #
# Reference data: oblast UID table + approximate centroids (from API docs)
# --------------------------------------------------------------------------- #
API_BASE = "https://api.alerts.in.ua/v1"
STORE_PATH = Path("alert_history_store.csv")
KYIV_TZ = "Europe/Kyiv"

# uid -> (Ukrainian title, English label, lat, lon)
OBLASTS: dict[int, tuple[str, str, float, float]] = {
    3:  ("Хмельницька область", "Khmelnytskyi", 49.42, 26.99),
    4:  ("Вінницька область", "Vinnytsia", 49.23, 28.47),
    5:  ("Рівненська область", "Rivne", 50.62, 26.25),
    8:  ("Волинська область", "Lutsk", 50.75, 25.33),
    9:  ("Дніпропетровська область", "Dnipro", 48.46, 35.05),
    10: ("Житомирська область", "Zhytomyr", 50.25, 28.66),
    11: ("Закарпатська область", "Uzhhorod", 48.62, 22.29),
    12: ("Запорізька область", "Zaporizhzhia", 47.84, 35.14),
    13: ("Івано-Франківська область", "Ivano-Frankivsk", 48.92, 24.71),
    14: ("Київська область", "Kyiv oblast", 50.05, 30.76),
    15: ("Кіровоградська область", "Kropyvnytskyi", 48.51, 32.26),
    16: ("Луганська область", "Luhansk", 48.57, 39.31),
    17: ("Миколаївська область", "Mykolaiv", 46.97, 31.99),
    18: ("Одеська область", "Odesa", 46.48, 30.73),
    19: ("Полтавська область", "Poltava", 49.59, 34.55),
    20: ("Сумська область", "Sumy", 50.91, 34.80),
    21: ("Тернопільська область", "Ternopil", 49.55, 25.59),
    22: ("Харківська область", "Kharkiv", 49.99, 36.23),
    23: ("Херсонська область", "Kherson", 46.64, 32.61),
    24: ("Черкаська область", "Cherkasy", 49.44, 32.06),
    25: ("Чернігівська область", "Chernihiv", 51.49, 31.29),
    26: ("Чернівецька область", "Chernivtsi", 48.29, 25.94),
    27: ("Львівська область", "Lviv", 49.84, 24.03),
    28: ("Донецька область", "Donetsk", 48.02, 37.80),
    29: ("Автономна Республіка Крим", "Crimea", 44.95, 34.10),
    30: ("м. Севастополь", "Sevastopol", 44.62, 33.53),
    31: ("м. Київ", "Kyiv city", 50.45, 30.52),
}
UA_TO_UID = {ua: uid for uid, (ua, *_rest) in OBLASTS.items()}
UA_TO_EN = {ua: en for _uid, (ua, en, *_c) in OBLASTS.items()}
CENTROID = {ua: (lat, lon) for _uid, (ua, _en, lat, lon) in OBLASTS.items()}

st.set_page_config(page_title="Ukraine Air Raid Alerts — Live", layout="wide")


# --------------------------------------------------------------------------- #
# API layer
# --------------------------------------------------------------------------- #
class AlertsAPI:
    def __init__(self, token: str):
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}

    def _get(self, path: str) -> dict:
        r = requests.get(f"{API_BASE}/{path}", headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()

    def active_alerts(self) -> list[dict]:
        return self._get("alerts/active.json").get("alerts", [])

    def region_history(self, uid: int, period: str = "month_ago") -> list[dict]:
        # NOTE: rate-limited to 2 requests/minute by the API.
        return self._get(f"regions/{uid}/alerts/{period}.json").get("alerts", [])


# --------------------------------------------------------------------------- #
# Local history store  (this is what makes the timeline grow over time)
# --------------------------------------------------------------------------- #
STORE_COLS = ["id", "location_title", "location_type", "location_oblast",
              "alert_type", "started_at", "finished_at", "updated_at", "calculated"]


def load_store() -> pd.DataFrame:
    if STORE_PATH.exists():
        df = pd.read_csv(STORE_PATH, parse_dates=["started_at", "finished_at", "updated_at"])
    else:
        df = pd.DataFrame(columns=STORE_COLS)
    return df


def upsert_store(existing: pd.DataFrame, new_rows: list[dict]) -> pd.DataFrame:
    if not new_rows:
        return existing
    new = pd.DataFrame(new_rows)
    for c in ("started_at", "finished_at", "updated_at"):
        if c in new:
            new[c] = pd.to_datetime(new[c], utc=True, errors="coerce")
    combined = pd.concat([existing, new], ignore_index=True)
    # Keep the most recently updated version of each alert id (finished beats active).
    combined = combined.sort_values("updated_at").drop_duplicates("id", keep="last")
    combined.to_csv(STORE_PATH, index=False)
    return combined


def refresh_live(api: AlertsAPI, backfill: bool) -> pd.DataFrame:
    store = load_store()
    store = upsert_store(store, api.active_alerts())
    if backfill:
        prog = st.progress(0.0, text="Backfilling trailing month (rate-limited 2/min)…")
        uids = list(OBLASTS.keys())
        for i, uid in enumerate(uids):
            try:
                store = upsert_store(store, api.region_history(uid, "month_ago"))
            except requests.HTTPError as e:
                st.warning(f"History fetch failed for uid {uid}: {e}")
            prog.progress((i + 1) / len(uids))
            time.sleep(31)  # respect the 2-requests/minute limit
        prog.empty()
    return store


# --------------------------------------------------------------------------- #
# Demo data (used when no token is provided)
# --------------------------------------------------------------------------- #
@st.cache_data
def demo_store(days: int = 45) -> pd.DataFrame:
    random.seed(7)
    now = pd.Timestamp.now(tz="UTC").floor("h")
    rows, rid = [], 0
    weights = {ua: (8 if en in {"Donetsk", "Kharkiv", "Zaporizhzhia",
                                "Sumy", "Kherson", "Dnipropetrovsk"} else 2)
               for ua, en in UA_TO_EN.items()}
    for ua in OBLASTS_BY_TITLE:
        t = now - pd.Timedelta(days=days)
        while t < now:
            gap = random.uniform(2, 60) / max(1, weights[ua] / 3)
            t = t + pd.Timedelta(hours=gap)
            if t >= now:
                break
            dur = random.uniform(8, 200)
            lt = random.choices(["oblast", "raion", "hromada"], [0.2, 0.6, 0.2])[0]
            end = t + pd.Timedelta(minutes=dur)
            rows.append(dict(id=rid, location_title=ua, location_type=lt,
                             location_oblast=ua, alert_type="air_raid",
                             started_at=t, finished_at=end, updated_at=end,
                             calculated=False))
            rid += 1
            if random.random() < 0.25:  # overlapping sub-regional alert
                rows.append(dict(id=rid, location_title=ua, location_type="raion",
                                 location_oblast=ua, alert_type="air_raid",
                                 started_at=t + pd.Timedelta(minutes=5),
                                 finished_at=end + pd.Timedelta(minutes=25),
                                 updated_at=end, calculated=False))
                rid += 1
    df = pd.DataFrame(rows)
    return df


OBLASTS_BY_TITLE = [ua for _uid, (ua, *_r) in OBLASTS.items()]


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
def merge_minutes(intervals: list[tuple]) -> float:
    """Total minutes covered by (start, end) intervals, overlaps merged."""
    intervals = [iv for iv in intervals if pd.notna(iv[0]) and pd.notna(iv[1])]
    if not intervals:
        return 0.0
    intervals.sort()
    total, cur_s, cur_e = 0.0, *intervals[0]
    for s, e in intervals[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
    total += (cur_e - cur_s).total_seconds()
    return total / 60.0


def derive_current_status(active: pd.DataFrame) -> dict[str, str]:
    """Per-oblast status from currently-active alerts: A=whole oblast, P=partial, N=none."""
    status = {ua: "N" for ua in OBLASTS_BY_TITLE}
    for ob, g in active.groupby("location_oblast"):
        if (g["location_type"] == "oblast").any():
            status[ob] = "A"
        elif len(g):
            status[ob] = "P"
    return status


def merged_hours_by_oblast(frame: pd.DataFrame) -> pd.Series:
    out = {}
    f = frame.copy()
    f["finished_at"] = f["finished_at"].fillna(pd.Timestamp.now(tz="UTC"))
    for ob, g in f.groupby("location_oblast"):
        out[ob] = merge_minutes(list(zip(g["started_at"], g["finished_at"]))) / 60.0
    return pd.Series(out)


# --------------------------------------------------------------------------- #
# Sidebar — data source + filters
# --------------------------------------------------------------------------- #
st.sidebar.header("Data source")
token = os.environ.get("ALERTS_IN_UA_TOKEN", "")
token = st.sidebar.text_input("alerts.in.ua API token", value=token, type="password", key="api_token")

# Load whatever history already exists (from import_archive.py and past refreshes).
stored = load_store()
has_stored = not stored.empty

demo = st.sidebar.toggle(
    "Demo mode (synthetic data)",
    value=(not has_stored and not bool(token)),
    help="Off = your real imported/collected history. On = synthetic sample data.",
)

if demo:
    df_all = demo_store()
    st.sidebar.caption("Synthetic demo data. Turn this OFF to see your imported history.")
else:
    df_all = stored
    if token:
        api = AlertsAPI(token)
        backfill = st.sidebar.checkbox(
            "Backfill trailing month on refresh (~14 min, rate-limited)", value=False
        )
        if st.sidebar.button("🔄 Refresh from API"):
            with st.spinner("Fetching from alerts.in.ua…"):
                try:
                    df_all = refresh_live(api, backfill)
                    st.sidebar.success(f"Store now holds {len(df_all):,} alerts.")
                except requests.HTTPError as e:
                    st.sidebar.error(f"API error: {e}")
    if has_stored:
        st.sidebar.caption(
            f"Loaded {len(stored):,} alerts from alert_history_store.csv."
            + ("" if token else " Add a token above to also pull live updates.")
        )
    else:
        st.sidebar.warning(
            "No history found. Run the importer (run_import.bat) to create "
            "alert_history_store.csv in this folder, then reload this page."
        )

# Normalise dtypes
for c in ("started_at", "finished_at", "updated_at"):
    if c in df_all:
        df_all[c] = pd.to_datetime(df_all[c], utc=True, errors="coerce")
df_all = df_all.dropna(subset=["started_at"]).copy()
df_all["oblast_en"] = df_all["location_oblast"].map(UA_TO_EN).fillna(df_all["location_oblast"])

st.sidebar.header("Filters")
if df_all.empty:
    st.title("🇺🇦 Ukraine Air Raid Alerts — Live")
    st.warning("No data to show. Run the importer (run_import.bat) to load history, "
               "or enable Demo mode in the sidebar.")
    st.stop()

min_d = df_all["started_at"].min().date()
max_d = df_all["started_at"].max().date()
dr = st.sidebar.date_input("Period", (min_d, max_d), min_value=min_d, max_value=max_d)
start_d, end_d = (dr if isinstance(dr, (list, tuple)) and len(dr) == 2 else (dr, dr))

all_en = sorted(df_all["oblast_en"].unique())
sel_en = st.sidebar.multiselect("Regions (empty = all)", all_en, default=[])
levels = st.sidebar.multiselect("Alert level", ["oblast", "raion", "hromada", "city"],
                                default=["oblast", "raion", "hromada", "city"])
freq_label = st.sidebar.selectbox("Time resolution", ["Daily", "Weekly", "Monthly"], 0)
freq = {"Daily": "D", "Weekly": "W", "Monthly": "MS"}[freq_label]

# Apply filters
m = (
    (df_all["started_at"].dt.date >= start_d)
    & (df_all["started_at"].dt.date <= end_d)
    & (df_all["location_type"].isin(levels))
)
if sel_en:
    m &= df_all["oblast_en"].isin(sel_en)
data = df_all.loc[m].copy()

# --------------------------------------------------------------------------- #
# Header + KPIs
# --------------------------------------------------------------------------- #
st.title("🇺🇦 Ukraine Air Raid Alerts — Live")
st.caption(
    f"{'Demo data' if demo else 'Stored history'} · "
    f"{', '.join(sel_en) if sel_en else 'All regions'} · {start_d} → {end_d}"
)
if data.empty:
    st.warning("No alerts match the current filters.")
    st.stop()

mh = merged_hours_by_oblast(data)
c1, c2, c3, c4 = st.columns(4)
c1.metric("Alerts in range", f"{len(data):,}")
c2.metric("Hours under alert (merged)", f"{mh.sum():,.0f}")
c3.metric("Regions affected", f"{data['location_oblast'].nunique()}")
c4.metric("Currently active", "—" if demo else f"{(df_all['finished_at'].isna()).sum()}")

# --------------------------------------------------------------------------- #
# 1. Interval-merged map by oblast  (the headline view)
# --------------------------------------------------------------------------- #
st.subheader("Map — hours under alert by oblast (overlaps merged)")

status = derive_current_status(df_all[df_all["finished_at"].isna()]) if not demo \
    else {ua: random.choice(["A", "P", "N", "N"]) for ua in OBLASTS_BY_TITLE}

map_rows = []
counts = data.groupby("location_oblast").size()
for ua, (lat, lon) in CENTROID.items():
    hrs = float(mh.get(ua, 0.0))
    map_rows.append(dict(
        oblast=UA_TO_EN.get(ua, ua), lat=lat, lon=lon,
        merged_hours=round(hrs, 1), alerts=int(counts.get(ua, 0)),
        status={"A": "Active now", "P": "Partial now", "N": "No alert"}[status.get(ua, "N")],
    ))
map_df = pd.DataFrame(map_rows)

fig_map = px.scatter_geo(
    map_df, lat="lat", lon="lon",
    color="status",
    color_discrete_map={"Active now": "#d62728", "Partial now": "#ff7f0e",
                        "No alert": "#9aa0a6"},
    hover_name="oblast",
    hover_data={"merged_hours": True, "alerts": True, "lat": False, "lon": False},
    projection="mercator",
)
fig_map.update_traces(marker=dict(size=14, line=dict(width=1, color="white")))
fig_map.update_geos(fitbounds="locations", visible=True, resolution=50,
                    showcountries=True, countrycolor="#cccccc",
                    showland=True, landcolor="#f4f4f4")
fig_map.update_layout(
    height=580, margin=dict(t=10, b=60, l=0, r=0),
    legend=dict(
        title_text="Current status",
        orientation="h", yanchor="top", y=-0.02,
        xanchor="center", x=0.5,
    ),
)
st.plotly_chart(fig_map, use_container_width=True, key="map_chart")
st.caption(
    "Bubble **colour** = live status right now. Hover a region for the total hours "
    "under any air-raid alert in the selected period — computed by merging overlapping "
    "raion/hromada alerts within each oblast so simultaneous sub-regional alerts aren't "
    "double-counted. Markers sit on approximate oblast centroids (points, not polygons)."
)

# --------------------------------------------------------------------------- #
# 2. National time series
# --------------------------------------------------------------------------- #
st.subheader("Alerts over time")
ts = data.set_index("started_at").resample(freq).size().rename("Alerts")
fig_ts = px.area(ts, labels={"value": "Alerts", "started_at": ""})
fig_ts.update_layout(height=340, showlegend=False, margin=dict(t=10))
st.plotly_chart(fig_ts, use_container_width=True, key="ts_chart")

# --------------------------------------------------------------------------- #
# 3. Regional comparison
# --------------------------------------------------------------------------- #
left, right = st.columns(2)
with left:
    st.subheader("Regions ranked (merged hours)")
    rank = mh.rename(index=UA_TO_EN).sort_values()
    fig_r = px.bar(rank, orientation="h", labels={"value": "Hours", "index": ""})
    fig_r.update_layout(height=620, showlegend=False, margin=dict(t=10))
    st.plotly_chart(fig_r, use_container_width=True, key="rank_chart")
with right:
    st.subheader("Trends for top regions")
    top = data.groupby("oblast_en").size().sort_values(ascending=False).head(6).index
    multi = (data[data["oblast_en"].isin(top)].set_index("started_at")
             .groupby("oblast_en").resample(freq).size().rename("Alerts").reset_index())
    fig_m = px.bar(multi, x="started_at", y="Alerts", color="oblast_en",
                   labels={"started_at": "", "oblast_en": "Oblast"})
    fig_m.update_layout(
        height=620, barmode="stack", bargap=0,
        margin=dict(t=10, b=70),
        legend=dict(title_text="Oblast", orientation="h",
                    yanchor="top", y=-0.12, xanchor="center", x=0.5),
    )
    st.plotly_chart(fig_m, use_container_width=True, key="trend_chart")
    st.caption("Stacked bars per region. Switch **Time resolution** (sidebar) to "
               "Weekly or Monthly for a cleaner view over long periods.")

st.download_button("Download filtered data (CSV)",
                   data.to_csv(index=False).encode("utf-8"),
                   "filtered_alerts.csv", "text/csv", key="dl_filtered")

with st.expander("How history & updates work — read me"):
    st.markdown(
        """
- **Live status & map** come from `/v1/alerts/active.json` (refreshed on demand).
- **Recent history** is backfilled from `/v1/regions/{uid}/alerts/month_ago.json`,
  which only reaches back ~30 days and is limited to 2 requests/minute.
- **Future alerts**: every refresh appends new/updated alerts to
  `alert_history_store.csv`, so your local timeline grows the longer you run it.
- **Full timeline since 24 Feb 2022 is NOT available from this API** — no live
  Ukrainian alert API exposes the multi-year backlog. For that you'd do a
  one-time import from a historical archive into the same store file.
- All API timestamps are UTC; display can be converted to Kyiv time.
        """
    )
