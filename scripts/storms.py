"""
Active tropical cyclones worldwide from NCEP TCVitals.

TCVitals is written with every GFS cycle and holds the operational centre, intensity and
structure of every active storm (NHC, CPHC, JTWC, ...). Reading the files of the past days
gives each storm's official track and intensity history.

TCVitals line (whitespace separated):
  0 centre  1 id(e.g. 17E)  2 name  3 yyyymmdd  4 hhmm  5 lat(e.g. 150N)  6 lon(e.g. 1010W)
  7 heading  8 speed  9 pmin(hPa)  10 penv  11 r_outer(km)  12 vmax(m/s)  13 rmw(km) ...
"""
import re
import numpy as np
import pandas as pd
import requests

NOMADS = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"
AWS    = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"

BASINS = {"L": "North Atlantic", "E": "East Pacific", "C": "Central Pacific", "W": "West Pacific",
          "A": "Arabian Sea", "B": "Bay of Bengal", "S": "South Indian Ocean", "P": "South Pacific",
          "Q": "South Atlantic", "U": "Australian region"}

def _urls(cyc):
    d, h = f"{cyc:%Y%m%d}", f"{cyc:%H}"
    for base in (NOMADS, AWS):
        for run in ("gfs", "gdas"):
            yield f"{base}/{run}.{d}/{h}/atmos/{run}.t{h}z.syndata.tcvitals.tm00"

def fetch_cycle(cyc, sess):
    for u in _urls(cyc):
        try:
            r = sess.get(u, timeout=30)
            if r.status_code == 200 and r.text.strip():
                return r.text
        except requests.RequestException:
            pass
    return None

def _ll(tok):
    v = int(tok[:-1]) / 10.0
    return -v if tok[-1] in "SW" else v

def parse(text):
    rows = []
    for line in text.splitlines():
        t = line.split()
        if len(t) < 14 or not re.fullmatch(r"\d{2}[A-Z]", t[1]):
            continue
        try:
            rows.append(dict(centre=t[0], sid=t[1], name=t[2],
                             time=pd.Timestamp(f"{t[3]} {t[4][:2]}:{t[4][2:]}"),
                             lat=_ll(t[5]), lon=_ll(t[6]),
                             pmin=float(t[9]), penv=float(t[10]), vmax_ms=float(t[12]), rmw_km=float(t[13])))
        except (ValueError, IndexError):
            continue
    return rows

def classify(basin_letter, kt, lat=None):
    """Label + Saffir-Simpson category from 1-min Vmax (kt)."""
    b = basin_letter
    if kt >= 64:
        cat = 1 if kt < 83 else 2 if kt < 96 else 3 if kt < 113 else 4 if kt < 137 else 5
        word = {"L": "Hurricane", "E": "Hurricane", "C": "Hurricane",
                "W": "Super Typhoon" if kt >= 130 else "Typhoon"}.get(b, "Cyclone")
        return word, cat
    if kt >= 34:
        return ("Cyclonic Storm" if b in "AB" else "Tropical Storm"), 0
    return ("Depression" if b in "AB" else "Tropical Depression"), -1

def find_active(lookback_days=8, include_invests=False, now=None, sess=None):
    """Storms active in the latest available cycle (kept for compatibility)."""
    active, _, latest = find_storms(lookback_days, include_invests, now, sess)
    return active, latest

def find_storms(lookback_days=8, include_invests=False, now=None, sess=None):
    """(active, ended, latest): ended = storms whose last advisory fell inside the look-back
    window but more than 12 h before the latest cycle (they have dissipated)."""
    sess = sess or requests.Session()
    sess.headers.setdefault("User-Agent", "tc3d-site/1.0")
    now = (now or pd.Timestamp.now("UTC").tz_localize(None)).floor("6h")
    cycles = pd.date_range(now - pd.Timedelta(days=lookback_days), now, freq="6h")
    rows, latest = [], None
    for c in cycles:
        txt = fetch_cycle(c, sess)
        if txt is None:
            continue
        latest = c
        rows += parse(txt)
    if not rows or latest is None:
        return [], [], latest
    df = pd.DataFrame(rows).drop_duplicates(["sid", "time"], keep="last")
    df["year"] = df.time.dt.year
    storms, ended = [], []
    for (sid, yr), g in df.groupby(["sid", "year"]):
        g = g.sort_values("time")
        if int(sid[:2]) >= 90 and not include_invests:            # invest areas
            continue
        is_active = g.time.max() >= latest - pd.Timedelta(hours=12)
        if not is_active and len(g) < 2:                          # a single advisory: nothing to show
            continue
        last = g.iloc[-1]
        name = last["name"]
        kt = last.vmax_ms * 1.944
        peak = g.vmax_ms.max() * 1.944
        label, cat = classify(sid[-1], kt if is_active else peak)   # ended storms are labelled by their peak
        disp = name.title() if name not in ("NONAME", "INVEST", "UNNAMED") and not name.isdigit() else sid
        (storms if is_active else ended).append(dict(
            key=f"{yr}_{sid}", sid=sid, year=int(yr), name=disp, label=label, category=int(cat),
            basin=sid[-1], basin_name=BASINS.get(sid[-1], "Unknown basin"), centre=last.centre,
            lat=float(last.lat), lon=float(last.lon), vmax_kt=round(float(kt)), pmin=float(last.pmin),
            peak_kt=round(float(peak)), first_seen=g.time.min(), last_seen=g.time.max(),
            track=g[["time", "lat", "lon", "vmax_ms", "pmin", "penv", "rmw_km"]].reset_index(drop=True)))
    return storms, ended, latest
