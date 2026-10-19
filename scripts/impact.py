"""
Cyclone impact layer: hazard x exposure x vulnerability on a common 0.1° grid.

Hazard     Holland (1980) wind profile with B from the official intensity and pressure deficit
           (Holland 1980: B = rho e Vmax^2 / dp, limited to 1-2.5), radius of maximum wind from
           TCVitals (Willoughby et al. 2006 fallback), plus 0.55 x the storm's forward motion
           (asymmetry). Footprint = highest wind each cell sees along the official track.
Exposure   GHSL GHS-POP 2025 (people per cell); economic value = people x national GDP per
           capita (PPP), the population-weighted form of LitPop (Eberenz et al. 2020).
Damage     Emanuel (2011) mean damage ratio: f = v^3 / (1 + v^3),
           v = max(V - 25.7, 0) / (74.7 - 25.7)   (m/s; CLIMADA default thresholds).
Vulnerability  country-level index from World Bank indicators (see fetch_exposure.py).
Risk index R = 100 x f(V) x E x Vul, with E = min(1, log10(1 + people) / 4.5).
All results are relative, indicative research products, not damage estimates or warnings.
"""
import base64, json, math, os
import numpy as np
import pandas as pd

D = 0.1; NY, NX = 1800, 3600
KT = 1.943844
V_THRESH, V_HALF = 25.7, 74.7
THRESH_MS = {"34": 34 / KT, "50": 50 / KT, "64": 64 / KT, "96": 96 / KT}
RISK_CLASSES = [(1, "very low"), (3, "low"), (8, "moderate"), (20, "high"), (1e9, "very high")]


def load_exposure(cache):
    try:
        pop = np.load(os.path.join(cache, "pop_0p1.npy"), mmap_mode="r")
        cid = np.load(os.path.join(cache, "country_0p1.npy"), mmap_mode="r")
        meta = json.load(open(os.path.join(cache, "countries.json")))
        return dict(pop=pop, cid=cid, meta=meta)
    except Exception as e:
        print("impact layer skipped (exposure data not available):", e)
        return None


def mdr(v):
    """Emanuel (2011) mean damage ratio, v in m/s."""
    vn = np.clip(v - V_THRESH, 0, None) / (V_HALF - V_THRESH)
    return vn ** 3 / (1 + vn ** 3)


def rmw_default(vmax, lat):
    return 46.4 * np.exp(-0.0155 * vmax + 0.0169 * abs(lat))           # Willoughby et al. (2006), km


def _hourly(track):
    tr = track.sort_values("time").reset_index(drop=True)
    t = pd.Series(pd.to_datetime(tr.time).values.astype("datetime64[s]").astype("int64"))   # seconds, whatever pandas' unit
    lon = np.rad2deg(np.unwrap(np.deg2rad(tr.lon.values)))
    th = np.arange(t.iloc[0], t.iloc[-1] + 1, 3600)
    f = lambda col: np.interp(th, t, col)
    out = pd.DataFrame(dict(t=th, lat=f(tr.lat.values), lon=f(lon), vmax=f(tr.vmax_ms.values)))
    for c in ("pmin", "penv", "rmw_km"):
        v = tr[c].values.astype(float) if c in tr else np.full(len(tr), np.nan)
        bad = ~np.isfinite(v) | (v <= 0) | (v > 2000)
        out[c] = np.interp(th, t[~bad], v[~bad]) if (~bad).sum() >= 1 else np.nan
    return out


def compute(track, exp, south=False, pad=6.0):
    """track: DataFrame time, lat, lon, vmax_ms, pmin, penv, rmw_km (official, 6-hourly)."""
    if exp is None or len(track) < 2: return None
    H = _hourly(track)
    lat0, lat1 = max(-70, H.lat.min() - pad), min(70, H.lat.max() + pad)
    lon0, lon1 = H.lon.min() - pad, H.lon.max() + pad
    r0, r1 = int(math.floor((90 - lat1) / D)), int(math.ceil((90 - lat0) / D))
    c0, c1 = int(math.floor((lon0 + 180) / D)), int(math.ceil((lon1 + 180) / D))
    rows = np.arange(r0, r1); cols = np.arange(c0, c1) % NX                    # wraps across the dateline
    pop = np.asarray(exp["pop"][r0:r1][:, cols], np.float64)
    cid = np.asarray(exp["cid"][r0:r1][:, cols])
    meta = exp["meta"]
    vul_c = np.array([m.get("vuln", 0.5) for m in meta] + [0.0])               # -1 (sea) -> last = 0
    gdp_c = np.array([(m.get("gdppc") or 0.0) for m in meta] + [0.0])
    vul = vul_c[cid]; gdp = pop * gdp_c[cid]
    lat = 90 - (rows + 0.5) * D; lon = -180 + (np.arange(c0, c1) + 0.5) * D     # unwrapped longitudes
    LAT, LON = np.meshgrid(lat, lon, indexing="ij")
    ny, nx = LAT.shape
    wmax = np.zeros((ny, nx)); arrival = np.full((ny, nx), 255, np.uint8)
    series = {"t": [], "pop34": [], "pop64": []}
    hemi = -1 if south else 1
    for i, h in H.iterrows():
        if h.vmax < 12: continue
        rmw = h.rmw_km if np.isfinite(h.rmw_km) and 5 <= h.rmw_km <= 300 else rmw_default(h.vmax, h.lat)
        dp = (h.penv - h.pmin) * 100 if np.isfinite(h.penv) and np.isfinite(h.pmin) and h.penv > h.pmin else np.nan
        B = np.clip(1.15 * math.e * h.vmax ** 2 / dp, 1.0, 2.5) if np.isfinite(dp) else 1.5
        dx = (LON - h.lon) * 111.2 * np.cos(np.deg2rad(h.lat)); dy = (LAT - h.lat) * 111.2
        r = np.hypot(dx, dy) + 1e-3; near = r < 1000
        if not near.any(): continue
        x = (rmw / r[near]) ** B
        vs = h.vmax * np.sqrt(x * np.exp(1 - x))                               # symmetric Holland profile, peak = Vmax
        if 0 < i < len(H) - 1:                                                 # forward motion (m/s)
            a, b = H.iloc[i - 1], H.iloc[i + 1]
            ux = (b.lon - a.lon) * 111.2e3 * np.cos(np.deg2rad(h.lat)) / 7200; uy = (b.lat - a.lat) * 111.2e3 / 7200
        else:
            ux = uy = 0.0
        tx, ty = hemi * -dy[near] / r[near], hemi * dx[near] / r[near]          # tangential unit vector
        k = 0.55 * np.clip(vs / max(h.vmax, 1), 0, 1)
        w = np.hypot(vs * tx + k * ux, vs * ty + k * uy)
        cur = wmax[near]; wmax[near] = np.maximum(cur, w)
        hour = int((h.t - H.t.iloc[0]) // 3600)
        first = (arrival[near] == 255) & (w >= THRESH_MS["34"])
        arr = arrival[near]; arr[first] = min(hour, 254); arrival[near] = arr
        if hour % 3 == 0:
            series["t"].append(pd.Timestamp(int(h.t), unit="s").strftime("%Y-%m-%dT%H:%MZ"))
            series["pop34"].append(int(pop[wmax >= THRESH_MS["34"]].sum())); series["pop64"].append(int(pop[wmax >= THRESH_MS["64"]].sum()))
    f = mdr(wmax)
    E = np.clip(np.log10(1 + pop) / 4.5, 0, 1)
    risk = 100 * f * E * vul
    summ = {f"pop{k}": int(pop[wmax >= v].sum()) for k, v in THRESH_MS.items()}
    summ["gdp64_usd_ppp"] = float(gdp[wmax >= THRESH_MS["64"]].sum())
    summ["peak_footprint_kt"] = round(float(wmax.max() * KT))
    edges = [0.05] + [hi for hi, _ in RISK_CLASSES]                          # cells per risk class (ignoring ~zero risk)
    summ["risk_cells"] = {name: int(((risk >= edges[i]) & (risk < edges[i + 1])).sum()) for i, (_, name) in enumerate(RISK_CLASSES)}
    by = []
    for j in np.unique(cid[(wmax >= THRESH_MS["34"]) & (cid >= 0)]):
        m = (cid == j)
        p34 = int(pop[m & (wmax >= THRESH_MS["34"])].sum())
        if p34 < 1000: continue
        by.append(dict(name=meta[j]["name"], iso3=meta[j]["iso3"], pop34=p34, pop64=int(pop[m & (wmax >= THRESH_MS["64"])].sum()),
                       vuln=round(float(meta[j]["vuln"]), 2), vuln_filled=bool(meta[j].get("vuln_filled"))))
    by.sort(key=lambda x: -x["pop34"])
    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a, np.uint8).tobytes()).decode()
    layers = dict(
        wind_kt=b64(np.clip(np.round(wmax * KT), 0, 254)),
        arrival_h=b64(arrival),
        pop_log=b64(np.clip(np.round(40 * np.log10(1 + pop)), 0, 254)),     # people = 10**(v/40) - 1
        vuln=b64(np.where(cid >= 0, np.clip(np.round(vul * 100), 0, 100), 255)),
        risk=b64(np.clip(np.round(risk * 4), 0, 254)))                         # index = v / 4
    return dict(grid=dict(lat_top=float(90 - r0 * D), lon_left=float(-180 + c0 * D), d=D, ny=ny, nx=nx),
                t0=pd.Timestamp(int(H.t.iloc[0]), unit="s").strftime("%Y-%m-%dT%H:%MZ"),
                layers=layers, summary=summ, countries=by[:8], series=series,
                method=dict(wind="Holland (1980) profile, B from official Vmax and pressure deficit, RMW from TCVitals; 0.55 x forward motion",
                            damage="Emanuel (2011), V_thresh 25.7 m/s, V_half 74.7 m/s", exposure="GHSL GHS-POP 2025; population x GDP per capita (PPP)",
                            vulnerability="country index from World Bank GDP per capita (PPP), under-5 mortality, access to electricity",
                            risk="100 x damage ratio x exposure x vulnerability, exposure = min(1, log10(1 + people) / 4.5)"))


def headline(imp):
    """One plain sentence for the written summary."""
    if not imp: return ""
    s = imp["summary"]
    fmt = lambda n: f"{n / 1e6:.1f} million" if n >= 1e6 else f"{n / 1e3:.0f} thousand" if n >= 1e3 else str(n)
    if s["pop34"] < 1000: return "The estimated wind footprint has so far stayed away from populated land."
    out = f"An estimated {fmt(s['pop34'])} people live where the modelled winds reached tropical-storm force (34 kt) or more"
    if s["pop64"] >= 1000: out += f", {fmt(s['pop64'])} of them in hurricane-force winds (64 kt)"
    top = imp["countries"][0]["name"] if imp["countries"] else None
    return out + (f", mostly in {top}" if top else "") + " (indicative, wind only; see the Impact view)."
