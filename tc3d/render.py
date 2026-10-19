"""
tc3d.render : 3D flow + structure diagnostics animation for any tropical cyclone.

Used by scripts/run_daily.py. Settings live in CFG; the daily script overwrites the
storm-specific ones (name, domain, period, official track) before each storm.
Works in both hemispheres (vorticity sign flipped in the south) and across the dateline.
"""
import os, sys, glob, warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)
import numpy as np
import xarray as xr
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection
from scipy.interpolate import RegularGridInterpolator
from scipy.ndimage import gaussian_filter
from skimage.measure import marching_cubes
from PIL import Image

CFG = dict(
    # ---- storm-specific (set by run_daily.py) ----
    storm_name   = "Tropical Cyclone",
    title_tag    = "3D Flow & Structure",
    t_start      = None, t_end = None,
    lat_range    = (0.0, 30.0), lon_range = (60.0, 100.0),
    lon360       = False,                # True when the domain crosses 180 deg
    hemi         = 1,                    # +1 north, -1 south (cyclonic = clockwise)
    track_df     = None,                 # official track: DataFrame time, lat, lon, vmax_ms
    landfall     = None,
    vmax         = 40.0,                 # colour-bar max (m/s)
    data_file    = "work/gfs.nc", gfs_cache = "work/gfs_cache", frame_dir = "work/frames",
    out_mp4      = "public/videos/storm.mp4", out_poster = "public/videos/storm.jpg", out_diag = None,
    # ---- general look (same as the Polo / BoB animations) ----
    levels       = [900, 850, 800, 750, 700, 650, 600, 550, 500],
    frames_per_hour = 1,
    n_seeds      = 2000, n_particles = 800,
    stream_steps = 60, stream_dt = 600.0, omega_boost = 0.15,
    vo_iso       = 3e-4, vo_iso_frac = 0.30, w_iso = -1.5,
    orbit        = False,                # True = slowly orbiting camera; False = fixed view (clearer flow)
    camera_elev  = 28,                   # fixed camera: height above the floor (degrees)
    camera_azim  = -60,                  # fixed camera: direction it looks from (degrees)
    follow_storm = True, view_half = 6.0, seed_rmax = 6.0, seed_rmin_rmw = 0.8, env_frac = 0.15,
    particle_s_per_frame = 4000.0, particle_substeps = 5, trail_len = 6,
    show_basemap = True, basemap_res = "50m",
    ocean_color  = (0.04, 0.10, 0.22), land_color = (0.30, 0.24, 0.14), land_alpha = 0.85,
    day_night    = True,
    ocean_day    = (0.06, 0.19, 0.36), land_day = (0.38, 0.31, 0.18),
    ocean_night  = (0.02, 0.04, 0.10), land_night = (0.11, 0.09, 0.06),
    twilight_deg = 6.0, floor_res = 60, show_lst = True, coast_color = (0.75, 0.80, 0.85),
    frame_ms     = 66, hold_last_ms = 2500,
    figsize      = (16, 9), dpi = 80,    # 1280x720 (HD) keeps videos small for the website
    dashboard    = True,
    ri_threshold_kt = 30.0,              # RI flagged ONLY from official intensities
    shear_annulus= (200.0, 800.0),
    show_tilt    = True, show_shear = True,
    n_workers    = None, stream_chunk = 6,
    track_csv    = None, track_anchor = None,
)

def _lon_norm(lon):
    return lon % 360 if CFG["lon360"] else ((lon + 180) % 360) - 180

# ----------------------------------------------------------------------------
# 1. DATA
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# 1b. GFS DOWNLOAD (0.25 deg, NOMADS grib-filter with AWS fallback)
# ----------------------------------------------------------------------------
GFS_VARS   = ["UGRD", "VGRD", "VVEL", "ABSV"]   # u, v, omega (Pa/s), absolute vorticity
ENV_VARS   = ["RH", "TMP", "LAND"]             # environment: humidity, surface (sea) temperature, land mask
ENV_LEVELS = [200]                              # 200 hPa winds for 850-200 hPa deep-layer shear
NOMADS_FLT = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
NOMADS_PUB = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"
AWS_PUB    = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"

def _gfs_name(cyc, fh):
    return f"gfs.{cyc:%Y%m%d}/{cyc:%H}/atmos", f"gfs.t{cyc:%H}z.pgrb2.0p25.f{fh:03d}"

def _exists(url, sess):
    try:
        return sess.head(url, timeout=30).status_code == 200
    except Exception:
        return False

def _cycle_available(cyc, fh, sess):
    d, f = _gfs_name(cyc, fh)
    return (_exists(f"{NOMADS_PUB}/{d}/{f}.idx", sess) or
            _exists(f"{AWS_PUB}/{d}/{f}.idx", sess))

def _fetch_nomads(cyc, fh, out, sess):
    d, f = _gfs_name(cyc, fh)
    la0, la1 = CFG["lat_range"]; lo0, lo1 = CFG["lon_range"]
    prm = {"dir": "/" + d, "file": f, "subregion": "",
           "toplat": la1 + 1, "bottomlat": la0 - 1,
           "leftlon": (lo0 - 1) % 360, "rightlon": (lo1 + 1) % 360}
    for v in GFS_VARS + ENV_VARS:
        prm[f"var_{v}"] = "on"
    for p in list(CFG["levels"]) + ENV_LEVELS:
        prm[f"lev_{p}_mb"] = "on"
    prm["lev_surface"] = "on"
    r = sess.get(NOMADS_FLT, params=prm, timeout=120)
    if r.status_code == 200 and r.content[:4] == b"GRIB":
        open(out, "wb").write(r.content); return True
    return False

def _fetch_aws(cyc, fh, out, sess):
    """Byte-range download of just the needed records (global fields, cropped later)."""
    d, f = _gfs_name(cyc, fh)
    r = sess.get(f"{AWS_PUB}/{d}/{f}.idx", timeout=60)
    if r.status_code != 200:
        return False
    lines = [l.split(":") for l in r.text.strip().splitlines()]
    want = {(v, f"{p} mb") for v in GFS_VARS + ["RH"] for p in CFG["levels"]}
    want |= {("UGRD", "200 mb"), ("VGRD", "200 mb"), ("TMP", "surface"), ("LAND", "surface")}
    chunks = []
    for i, l in enumerate(lines):
        if (l[3], l[4]) in want:
            start = int(l[1]); end = int(lines[i + 1][1]) - 1 if i + 1 < len(lines) else ""
            g = sess.get(f"{AWS_PUB}/{d}/{f}", headers={"Range": f"bytes={start}-{end}"}, timeout=120)
            if g.status_code in (200, 206):
                chunks.append(g.content)
    if not chunks:
        return False
    open(out, "wb").write(b"".join(chunks)); return True

def _read_grib(path):
    """Read one GFS GRIB2 file. Variables may come with different level sets (e.g. winds at 200 hPa but
    vorticity not), so cfgrib splits the file into consistent groups and each variable is picked out."""
    import cfgrib
    def crop(d):
        d = d.assign_coords(longitude=_lon_norm(d.longitude)).sortby("longitude").sortby("latitude")
        return d.sel(latitude=slice(*CFG["lat_range"]), longitude=slice(*CFG["lon_range"]))
    iso, sfc = {}, {}
    for d in cfgrib.open_datasets(path, backend_kwargs={"indexpath": ""}):
        if "isobaricInhPa" in d.coords and d["isobaricInhPa"].ndim == 1:
            d = crop(d.rename({"isobaricInhPa": "pressure_level"}))
            for name in d.data_vars: iso.setdefault(name, d[name])
        elif "isobaricInhPa" in d.coords:                       # a single pressure level (scalar coordinate)
            d = crop(d.expand_dims("isobaricInhPa").rename({"isobaricInhPa": "pressure_level"}))
            for name in d.data_vars:
                iso[name] = d[name] if name not in iso else xr.concat([iso[name], d[name]], "pressure_level").sortby("pressure_level")
        elif d.attrs.get("GRIB_typeOfLevel") == "surface" or "surface" in d.coords:
            d = crop(d)
            for name in d.data_vars: sfc.setdefault(name, d[name])
    def lev(name, levels):
        a = iso[name]; have = [p_ for p_ in levels if p_ in a.pressure_level.values]
        return a.sel(pressure_level=have) if len(have) == len(levels) else None
    main = {k: lev(k, CFG["levels"]) for k in ("u", "v", "w", "absv", "r") if k in iso}
    for k in ("u", "v", "w", "absv"):
        if main.get(k) is None: raise RuntimeError(f"{path}: {k} missing on some of the 900-500 hPa levels")
    f = 2 * 7.292e-5 * np.sin(np.deg2rad(main["u"].latitude))
    out = xr.Dataset(dict(u=main["u"], v=main["v"], w=main["w"], vo=main["absv"] - f))
    nan3 = xr.full_like(out["u"], np.nan); nan2 = nan3.isel(pressure_level=0, drop=True)
    out["rh"] = main["r"] if main.get("r") is not None else nan3
    for k, name in (("u200", "u"), ("v200", "v")):
        a = iso.get(name)
        out[k] = a.sel(pressure_level=200, drop=True) if a is not None and 200 in a.pressure_level.values else nan2
    sst, land = nan2, nan2
    for name, a in sfc.items():
        gname = str(a.attrs.get("GRIB_name", "")).lower()
        if name == "t" or "temperature" in gname: sst = a.reindex_like(nan2, method="nearest") - 273.15
        elif name in ("lsm", "land") or "land" in gname: land = a.reindex_like(nan2, method="nearest")
    out["sst"] = sst; out["land"] = land
    out = out.drop_vars([c for c in out.coords if c not in out.dims])
    return out.astype("float32")

def download_gfs(out=None, cache=None, step_h=3):
    """Analyses (f000) + 3-h forecasts (f003) of each 6-hourly cycle give 3-hourly
    fields. Times not yet analysed are filled from the latest available cycle's
    forecast and flagged, so the GIF title shows [GFS fcst] for those frames.
    NOMADS keeps ~10 days; older dates come from the AWS archive automatically."""
    import requests, time as _time, pandas as pd
    out = out or CFG["data_file"]; cache = cache or CFG["gfs_cache"]
    os.makedirs(cache, exist_ok=True)
    sess = requests.Session(); sess.headers["User-Agent"] = "tc-3d-flow/1.0"
    t0, t1 = pd.Timestamp(CFG["t_start"]), pd.Timestamp(CFG["t_end"])
    targets = pd.date_range(t0, t1, freq=f"{step_h}h")

    # latest cycle that has been published
    now = (pd.Timestamp(CFG["now"]) if CFG.get("now") else pd.Timestamp.now("UTC").tz_localize(None)).floor("6h")   # "now" only for tests
    latest = next((c for c in pd.date_range(now - pd.Timedelta("4D"), now, freq="6h")[::-1]
                   if _cycle_available(c, 0, sess)), None)
    if latest is None:
        sys.exit("Could not reach NOMADS or AWS (no GFS cycle found in the last 4 days). "
                 "Check your internet connection / proxy and try again.")
    print("latest available GFS cycle:", latest)

    fields, flags = [], []
    for t in targets:
        cyc = t.floor("6h"); fh = int((t - cyc) / pd.Timedelta("1h")); fc = 0.0
        if cyc > latest or (fh > 0 and not _cycle_available(cyc, fh, sess)):
            if not CFG.get("allow_forecast", False):
                continue
            cyc = latest; fh = int((t - latest) / pd.Timedelta("1h")); fc = 1.0
        if fh > 384:
            print(f"  {t}: beyond GFS range, stopping"); break
        fn = os.path.join(cache, f"gfs_{cyc:%Y%m%d%H}_f{fh:03d}.grb2")
        if not os.path.exists(fn):
            ok = False
            for attempt in range(3):
                ok = _fetch_nomads(cyc, fh, fn, sess) or _fetch_aws(cyc, fh, fn, sess)
                if ok: break
                _time.sleep(5 * (attempt + 1))
            if not ok:
                print(f"  {t}: download failed, skipping"); continue
            _time.sleep(1.0)                        # be polite to NOMADS
        print(f"  {t:%d-%b %H:%M}  <- cycle {cyc:%Y%m%d %H}z f{fh:03d}{'  (forecast)' if fc else ''}")
        fields.append(_read_grib(fn).expand_dims(time=[t])); flags.append(fc)

    if len(fields) < 2:
        raise RuntimeError("not enough GFS fields downloaded")
    ds = xr.concat(fields, "time")
    ds["is_fcst"] = ("time", np.array(flags, "float32"))
    hourly = pd.date_range(ds.time.values[0], ds.time.values[-1], freq="1h")
    ds = ds.interp(time=hourly)                     # 3-hourly -> hourly for smooth frames
    ds.attrs["source"] = "NCEP GFS 0.25 deg (analysis + 3-h forecasts)"
    ds.to_netcdf(out)
    print(f"saved {out}: {len(ds.time)} hourly steps, "
          f"{int((ds.is_fcst > 0).sum())} of them from forecast")

# ----------------------------------------------------------------------------
# 2. LOAD / SYNTHETIC
# ----------------------------------------------------------------------------
def load(path):
    ds = xr.open_dataset(path)
    ren = {k: v for k, v in {"valid_time": "time", "level": "pressure_level",
                              "isobaricInhPa": "pressure_level",
                              "lat": "latitude", "lon": "longitude"}.items() if k in ds.dims or k in ds.coords}
    ds = ds.rename(ren)
    ds = ds.assign_coords(longitude=_lon_norm(ds.longitude))
    ds = ds.sortby("latitude").sortby("longitude").sortby("pressure_level")
    ds = ds.sel(latitude=slice(*CFG["lat_range"]), longitude=slice(*CFG["lon_range"]),
                pressure_level=slice(min(CFG["levels"]), max(CFG["levels"])),
                time=slice(CFG["t_start"], CFG["t_end"]))
    keep = [v for v in ["u", "v", "w", "vo", "rh", "u200", "v200", "sst", "land", "is_fcst"] if v in ds]
    ds = ds[keep].load()
    if CFG["hemi"] < 0:                      # southern hemisphere: make cyclonic vorticity positive
        ds["vo"] = -ds["vo"]
    return ds

# ----------------------------------------------------------------------------
# 3. TRACK
# ----------------------------------------------------------------------------
def track_from_data(ds):
    """Centre = vorticity-weighted centroid at 850 hPa, followed forward and backward
    in time from an anchor position (avoids locking onto the monsoon trough)."""
    vo = gaussian_filter(ds.vo.sel(pressure_level=850, method="nearest").values, (0, 2, 2))
    lat, lon = ds.latitude.values, ds.longitude.values
    LAT, LON = np.meshgrid(lat, lon, indexing="ij")
    times = ds.time.values; nt = len(times)
    anc = CFG.get("track_anchor")
    if anc:
        i0 = int(np.argmin(np.abs(times - np.datetime64(anc[0])))); c0 = [anc[1], anc[2]]
    else:
        i0 = 0; j, i = np.unravel_index(np.argmax(vo[0]), vo[0].shape); c0 = [lat[j], lon[i]]
    def step(t, c):
        for _ in range(3):
            m = np.hypot((LON - c[1]) * np.cos(np.deg2rad(c[0])), LAT - c[0]) < 2.0
            f = np.where(m, np.clip(vo[t], 0, None), 0) ** 2
            if f.sum() <= 0: break
            c = [(f * LAT).sum() / f.sum(), (f * LON).sum() / f.sum()]
        return c
    out = np.zeros((nt, 2)); c = c0
    for t in range(i0, nt): c = step(t, c); out[t] = c
    c = out[i0]
    for t in range(i0 - 1, -1, -1): c = step(t, c); out[t] = c
    return out

def load_track(ds):
    if CFG.get("track_df") is not None:          # official agency track (TCVitals)
        df = CFG["track_df"].sort_values("time")
        tt = df.time.values.astype("datetime64[s]").astype(float)
        tq = ds.time.values.astype("datetime64[s]").astype(float)
        lon = np.unwrap(np.deg2rad(df.lon.values)); lon = np.rad2deg(np.interp(tq, tt, lon))
        return np.c_[np.interp(tq, tt, df.lat.values), _lon_norm(lon)]
    if CFG["track_csv"] and os.path.exists(CFG["track_csv"]):
        import pandas as pd
        df = pd.read_csv(CFG["track_csv"], parse_dates=["time"]).sort_values("time")
        tt = df.time.values.astype("datetime64[s]").astype(float)
        tq = ds.time.values.astype("datetime64[s]").astype(float)
        return np.c_[np.interp(tq, tt, df.lat.values), np.interp(tq, tt, df.lon.values)]
    return track_from_data(ds)

# ----------------------------------------------------------------------------
# 4. FLOW TOOLS
# ----------------------------------------------------------------------------
class Field:
    def __init__(self, ds, it0, it1, a):
        g = lambda n: (1 - a) * ds[n].isel(time=it0).values + a * ds[n].isel(time=it1).values
        self.p = ds.pressure_level.values.astype(float)
        self.lat = ds.latitude.values; self.lon = ds.longitude.values
        self.u, self.v, self.w, self.vo = g("u"), g("v"), g("w"), g("vo")
        self.spd = np.sqrt(self.u**2 + self.v**2)
        grid = (self.p, self.lat, self.lon)
        mk = lambda a: RegularGridInterpolator(grid, a, bounds_error=False, fill_value=np.nan)
        self.iu, self.iv, self.iw, self.isp = mk(self.u), mk(self.v), mk(self.w), mk(self.spd)

    def vel(self, P):                     # P: (N,3) = lon, lat, p
        q = np.c_[P[:, 2], P[:, 1], P[:, 0]]
        u, v, w = self.iu(q), self.iv(q), self.iw(q)
        coslat = np.cos(np.deg2rad(P[:, 1]))
        return np.c_[u / (111e3 * coslat), v / 111e3, CFG["omega_boost"] * w / 100.0]

    def inside(self, P):
        return ((P[:, 0] >= self.lon[0]) & (P[:, 0] <= self.lon[-1]) &
                (P[:, 1] >= self.lat[0]) & (P[:, 1] <= self.lat[-1]) &
                (P[:, 2] >= self.p[0]) & (P[:, 2] <= self.p[-1]))

    def streamlines(self, seeds):
        dt = CFG["stream_dt"]; P = seeds.copy(); path = [P.copy()]
        for _ in range(CFG["stream_steps"]):
            k1 = self.vel(P); k2 = self.vel(P + 0.5 * dt * k1)
            P = P + dt * k2
            P[~self.inside(P)] = np.nan
            path.append(P.copy())
        return np.stack(path, 1)          # (N, steps+1, 3)

# ----------------------------------------------------------------------------
# 4b. LAND / OCEAN BASEMAP
# ----------------------------------------------------------------------------
_LAND = None
def land_geometry():
    """Natural Earth land polygons for the whole domain (downloaded once by cartopy, then cached)."""
    global _LAND
    if _LAND is None:
        try:
            import cartopy.io.shapereader as shpreader
            from shapely.geometry import box as sbox
            from shapely.ops import unary_union
            from shapely.affinity import translate
            (lo0, lo1), (la0, la1) = CFG["lon_range"], CFG["lat_range"]
            dom = sbox(lo0 - 3, la0 - 3, lo1 + 3, la1 + 3)
            shp = shpreader.natural_earth(resolution=CFG["basemap_res"], category="physical", name="land")
            geoms = []
            for g in shpreader.Reader(shp).geometries():
                for sh in ((0, 360) if CFG["lon360"] else (0,)):
                    gg = translate(g, xoff=sh) if sh else g
                    if gg.intersects(dom): geoms.append(gg.intersection(dom))
            _LAND = unary_union(geoms) if geoms else False
        except Exception as e:
            print(f"\n[basemap] skipped ({e}). Install with:  conda install -c conda-forge cartopy")
            _LAND = False
    return _LAND

def _polys(geom):
    if geom.is_empty: return []
    if geom.geom_type == "Polygon": return [geom]
    return [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]

def sun_elevation(t, lat, lon):
    """Solar elevation angle (deg) for UTC time t (numpy datetime64), NOAA approximation."""
    ts = t.astype("datetime64[s]").astype("int64")
    doy = (ts // 86400 - np.datetime64(str(t)[:4] + "-01-01").astype("datetime64[D]").astype("int64")) + 1
    hr = (ts % 86400) / 3600.0
    g = 2 * np.pi / 365.0 * (doy - 1 + (hr - 12) / 24)
    eqt = 229.18 * (0.000075 + 0.001868 * np.cos(g) - 0.032077 * np.sin(g)
                    - 0.014615 * np.cos(2 * g) - 0.040849 * np.sin(2 * g))
    dec = (0.006918 - 0.399912 * np.cos(g) + 0.070257 * np.sin(g) - 0.006758 * np.cos(2 * g)
           + 0.000907 * np.sin(2 * g) - 0.002697 * np.cos(3 * g) + 0.00148 * np.sin(3 * g))
    lst = hr * 60 + eqt + 4 * lon                          # local solar time, minutes
    ha = np.deg2rad(lst / 4 - 180)
    la = np.deg2rad(lat)
    el = np.arcsin(np.sin(la) * np.sin(dec) + np.cos(la) * np.cos(dec) * np.cos(ha))
    return np.rad2deg(el), (lst / 60) % 24

def draw_basemap(ax, box, z, tnow=None):
    (lo0, lo1), (la0, la1) = box
    land = land_geometry()
    if CFG["day_night"] and tnow is not None:
        n = CFG["floor_res"]
        X, Y = np.meshgrid(np.linspace(lo0, lo1, n), np.linspace(la0, la1, n))
        el, _ = sun_elevation(tnow, Y, X)
        f = np.clip((el + CFG["twilight_deg"] / 2) / CFG["twilight_deg"], 0, 1)[..., None]   # 0 night .. 1 day
        f = f * f * (3 - 2 * f)                                                              # smooth edge
        oc = (1 - f) * np.array(CFG["ocean_night"]) + f * np.array(CFG["ocean_day"])
        ld = (1 - f) * np.array(CFG["land_night"]) + f * np.array(CFG["land_day"])
        if land:
            import shapely
            m = shapely.contains_xy(land, X, Y)[..., None]
            rgb = np.where(m, ld, oc)
        else:
            rgb = oc
        ax.plot_surface(X, Y, np.full_like(X, z), facecolors=np.clip(rgb, 0, 1), rstride=1, cstride=1,
                        shade=False, linewidth=0, antialiased=False, zorder=0)
    else:
        ocean = [[(lo0, la0, z), (lo1, la0, z), (lo1, la1, z), (lo0, la1, z)]]
        ax.add_collection3d(Poly3DCollection(ocean, facecolor=CFG["ocean_color"], edgecolor="none",
                                             alpha=1.0, zorder=0, shade=False))
    if not land: return
    from shapely.geometry import box as sbox
    clip = land.intersection(sbox(lo0, la0, lo1, la1))
    polys = [[(x, y, z) for x, y in p.exterior.coords] for p in _polys(clip)]
    if not polys: return
    if CFG["day_night"] and tnow is not None:                  # land already coloured; coastline only
        segs = np.concatenate([np.stack([np.asarray(q[:-1]), np.asarray(q[1:])], 1) for q in polys if len(q) > 1])
        ax.add_collection3d(Line3DCollection(segs, colors=[CFG["coast_color"]], linewidths=0.8, zorder=1))
    else:
        ax.add_collection3d(Poly3DCollection(polys, facecolor=CFG["land_color"], edgecolor=CFG["coast_color"],
                                             linewidths=0.8, alpha=CFG["land_alpha"], zorder=1, shade=False))

def storm_rmw(F, ctr, prev=None):
    """Radius of maximum azimuthal-mean tangential wind at 850 hPa (deg)."""
    k = int(np.argmin(np.abs(F.p - 850)))
    LAT, LON = np.meshgrid(F.lat, F.lon, indexing="ij")
    dx = (LON - ctr[1]) * np.cos(np.deg2rad(ctr[0])); dy = LAT - ctr[0]
    r = np.hypot(dx, dy) + 1e-6
    vt = (-F.u[k] * dy + F.v[k] * dx) / r
    bins = np.arange(0.125, 4.01, 0.25)
    prof = [np.nanmean(vt[(r >= b - 0.125) & (r < b + 0.125)]) for b in bins]
    rmw = float(np.clip(bins[int(np.nanargmax(prof))], 0.3, 2.5))
    return rmw if prev is None else 0.8 * prev + 0.2 * rmw     # smooth in time

class Seeds:
    """Fixed storm-relative seed set -> smooth, flicker-free streamlines."""
    def __init__(self, n, rng):
        ne = int(n * CFG["env_frac"]); nr = n - ne
        self.q  = rng.uniform(0, 1, nr) ** 0.8        # spread across the vortex, eye kept clear
        self.th = rng.uniform(0, 2 * np.pi, nr)
        self.pz = rng.uniform(0, 1, n)
        self.ex = rng.uniform(-1, 1, (ne, 2))
    def place(self, ctr, rmw, box, p):
        r0 = CFG["seed_rmin_rmw"] * rmw; r1 = CFG["seed_rmax"]
        r = r0 + self.q * (r1 - r0)
        coslat = np.cos(np.deg2rad(ctr[0]))
        xr = ctr[1] + r * np.cos(self.th) / coslat; yr = ctr[0] + r * np.sin(self.th)
        (lo0, lo1), (la0, la1) = box
        xe = 0.5 * (lo0 + lo1) + 0.5 * (lo1 - lo0) * self.ex[:, 0]
        ye = 0.5 * (la0 + la1) + 0.5 * (la1 - la0) * self.ex[:, 1]
        x = np.r_[xr, xe]; y = np.r_[yr, ye]
        return np.c_[x, y, p[0] + self.pz * (p[-1] - p[0])]

def spawn_ring(n, ctr, rmw, p, rng):
    r = CFG["seed_rmin_rmw"] * rmw + rng.uniform(0, 1, n) ** 0.8 * (CFG["seed_rmax"] - rmw)
    th = rng.uniform(0, 2 * np.pi, n)
    return np.c_[ctr[1] + r * np.cos(th) / np.cos(np.deg2rad(ctr[0])),
                 ctr[0] + r * np.sin(th), rng.uniform(p[0], p[-1], n)]

def iso_mesh(F, arr, level, box, stride=1):
    (lo0, lo1), (la0, la1) = box
    jj = (F.lat >= la0) & (F.lat <= la1); ii = (F.lon >= lo0) & (F.lon <= lo1)
    a = gaussian_filter(arr, 0.8)[:, jj][:, :, ii][:, ::stride, ::stride]
    lat = F.lat[jj][::stride]; lon = F.lon[ii][::stride]
    if a.shape[1] < 2 or a.shape[2] < 2 or not (a.min() < level < a.max()):
        return None
    verts, faces, _, _ = marching_cubes(a, level)
    pp = np.interp(verts[:, 0], np.arange(len(F.p)), F.p)
    la = np.interp(verts[:, 1], np.arange(len(lat)), lat)
    lo = np.interp(verts[:, 2], np.arange(len(lon)), lon)
    return np.c_[lo, la, pp][faces]

def in_box(P, box, p):
    (lo0, lo1), (la0, la1) = box
    return ((P[..., 0] >= lo0) & (P[..., 0] <= lo1) & (P[..., 1] >= la0) & (P[..., 1] <= la1) &
            (P[..., 2] >= p[0]) & (P[..., 2] <= p[-1]))

# ----------------------------------------------------------------------------
# 4c. DIAGNOSTICS (vortex centres per level, tilt, shear, Vmax, RMW)
# ----------------------------------------------------------------------------
def _centroid(f2, LAT, LON, ref, rad=1.0, it=3):
    f2 = np.clip(gaussian_filter(np.nan_to_num(f2), 1.0), 0, None) ** 2
    c = np.array(ref, float)
    for _ in range(it):
        m = np.hypot((LON - c[1]) * np.cos(np.deg2rad(c[0])), LAT - c[0]) < rad
        w = np.where(m, f2, 0)
        if w.sum() <= 0: break
        c = np.array([(w * LAT).sum() / w.sum(), (w * LON).sum() / w.sum()])
    return c

def diagnostics(ds, track):
    lat, lon = ds.latitude.values, ds.longitude.values
    p = ds.pressure_level.values.astype(float)
    LAT, LON = np.meshgrid(lat, lon, indexing="ij")
    nt, nk = len(ds.time), len(p)
    k850 = int(np.argmin(np.abs(p - 850))); k500 = int(np.argmin(np.abs(p - 500)))
    kmax = int(np.argmax(p))
    cen = np.zeros((nt, nk, 2)); vmax = np.zeros(nt); rmw = np.zeros(nt); shr = np.zeros((nt, 2))
    a0, a1 = [x / 111.0 for x in CFG["shear_annulus"]]
    U, V, VO = ds.u.values, ds.v.values, ds.vo.values
    bins = np.arange(0.125, 4.01, 0.25)
    shd = np.full((nt, 2), np.nan); rhm = np.full(nt, np.nan); sstc = np.full(nt, np.nan)
    kmid = [k for k, pp in enumerate(p) if 500 <= pp <= 700]
    RH = ds.rh.values if "rh" in ds else None
    U2, V2 = (ds.u200.values, ds.v200.values) if "u200" in ds else (None, None)
    SST = ds.sst.values if "sst" in ds else None
    LND = ds.land.values if "land" in ds else None
    for t in range(nt):
        c850 = _centroid(VO[t, k850], LAT, LON, track[t]); cen[t, k850] = c850
        ref = c850
        for k in range(k850 - 1, -1, -1):              # upward (lower pressure)
            ref = _centroid(VO[t, k], LAT, LON, ref); cen[t, k] = ref
        ref = c850
        for k in range(k850 + 1, nk):                  # downward (900 hPa)
            ref = _centroid(VO[t, k], LAT, LON, ref); cen[t, k] = ref
        dx = (LON - c850[1]) * np.cos(np.deg2rad(c850[0])); dy = LAT - c850[0]; r = np.hypot(dx, dy) + 1e-6
        sp = np.hypot(U[t, kmax], V[t, kmax]); vmax[t] = np.nanmax(np.where(r < 2.5, sp, np.nan))
        vt = CFG["hemi"] * (-U[t, k850] * dy + V[t, k850] * dx) / r
        prof = [np.nanmean(np.where((r >= b - 0.125) & (r < b + 0.125), vt, np.nan)) for b in bins]
        rmw[t] = np.clip(bins[int(np.nanargmax(prof))], 0.25, 3.0)
        ann = (r >= a0) & (r <= a1)
        shr[t] = [np.nanmean(U[t, k500][ann]) - np.nanmean(U[t, k850][ann]),
                  np.nanmean(V[t, k500][ann]) - np.nanmean(V[t, k850][ann])]
        if U2 is not None and np.isfinite(U2[t][ann]).any():          # 850-200 hPa deep-layer shear
            shd[t] = [np.nanmean(U2[t][ann]) - np.nanmean(U[t, k850][ann]), np.nanmean(V2[t][ann]) - np.nanmean(V[t, k850][ann])]
        if RH is not None and kmid and np.isfinite(RH[t][kmid][:, ann]).any():   # 500-700 hPa RH, 200-800 km
            rhm[t] = np.nanmean(RH[t][kmid][:, ann])
        if SST is not None:                                            # ocean-only mean within 150 km
            ocean = r <= 150 / 111.0
            if LND is not None and np.isfinite(LND[t]).any(): ocean = ocean & (LND[t] < 0.5)
            if np.isfinite(SST[t][ocean]).any(): sstc[t] = np.nanmean(SST[t][ocean])
    rmw = gaussian_filter(rmw, 2, mode="nearest")
    shr = np.c_[gaussian_filter(shr[:, 0], 1.5, mode="nearest"), gaussian_filter(shr[:, 1], 1.5, mode="nearest")]
    d = cen[:, k500] - cen[:, k850]
    tilt = np.c_[d[:, 1] * 111 * np.cos(np.deg2rad(cen[:, k850, 0])), d[:, 0] * 111]   # km (east, north)
    tilt = np.c_[gaussian_filter(tilt[:, 0], 1.5, mode="nearest"), gaussian_filter(tilt[:, 1], 1.5, mode="nearest")]
    voff = np.full(nt, np.nan); ri = np.zeros(nt, bool)
    if CFG.get("track_df") is not None:
        df = CFG["track_df"].sort_values("time")
        tt = df.time.values.astype("datetime64[s]").astype(float)
        tq = ds.time.values.astype("datetime64[s]").astype(float)
        voff = np.interp(tq, tt, df.vmax_ms.values, left=np.nan, right=np.nan)
        thr = CFG["ri_threshold_kt"] / 1.944
        for i_, t_ in enumerate(tt):              # RI: official Vmax(t+24h) - Vmax(t) >= 30 kt
            j_ = np.argmin(np.abs(tt - (t_ + 86400)))
            if abs(tt[j_] - t_ - 86400) <= 3 * 3600 and df.vmax_ms.values[j_] - df.vmax_ms.values[i_] >= thr:
                ri |= (tq >= t_) & (tq <= tt[j_])
    sm = lambda x: gaussian_filter(x, 1.5, mode="nearest") if np.isfinite(x).all() else x
    shd = np.c_[sm(shd[:, 0]), sm(shd[:, 1])]; rhm = sm(rhm); sstc = sm(sstc)
    return dict(cen=cen, vmax=vmax, rmw=rmw, shear=shr, tilt=tilt, k850=k850, k500=k500, voff=voff, ri=ri,
                shear_deep=shd, rh_mid=rhm, sst=sstc)

def _diag_json(ds, D, path):
    """Hourly structure and environment series for the website (one small JSON per storm)."""
    import json
    def r(x, n=1): return [None if not np.isfinite(v) else round(float(v), n) for v in x]
    hyp = lambda a: np.hypot(a[:, 0], a[:, 1])
    frm = lambda a: (np.degrees(np.arctan2(a[:, 0], a[:, 1])) + 180) % 360
    t = [str(np.datetime_as_string(x, unit="m")) + "Z" for x in ds.time.values]
    out = dict(time=t, gfs_vmax=r(D["vmax"]), rmw=r(D["rmw"] * 111, 0), tilt=r(hyp(D["tilt"]), 0),
               shear_mid=r(hyp(D["shear"])), shear_mid_from=r(frm(D["shear"]), 0),
               shear_deep=r(hyp(D["shear_deep"])), shear_deep_from=r(frm(D["shear_deep"]), 0),
               rh_mid=r(D["rh_mid"], 0), sst=r(D["sst"], 1))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, separators=(",", ":"))

def _interp(arr, tf):
    i0 = int(np.floor(tf)); i1 = min(i0 + 1, len(arr) - 1); a = tf - i0
    return (1 - a) * arr[i0] + a * arr[i1]

def _compass(fig, rect, shear, tilt):
    ax = fig.add_axes(rect, facecolor="black"); ax.set_aspect("equal"); ax.axis("off")
    th = np.linspace(0, 2 * np.pi, 200)
    for rr in (0.5, 1.0):
        ax.plot(rr * np.cos(th), rr * np.sin(th), color=(0.4, 0.4, 0.4), lw=0.6)
    ax.plot([-1.1, 1.1], [0, 0], color=(0.3, 0.3, 0.3), lw=0.5); ax.plot([0, 0], [-1.1, 1.1], color=(0.3, 0.3, 0.3), lw=0.5)
    for txt, (x, y) in {"N": (0, 1.22), "E": (1.22, 0), "S": (0, -1.25), "W": (-1.25, 0)}.items():
        ax.text(x, y, txt, color="0.7", ha="center", va="center", fontsize=9)
    sm = np.hypot(*shear); tm = np.hypot(*tilt)
    if tm > 1:
        v = np.array(tilt) / tm * min(tm / 150.0, 1.0)            # full circle = 150 km
        ax.annotate("", xy=v, xytext=(0, 0), arrowprops=dict(arrowstyle="-|>", color="white", lw=2.2))
    if sm > 0.1:
        v = np.array(shear) / sm * min(sm / 20.0, 1.0)            # full circle = 20 m/s
        ax.annotate("", xy=v, xytext=(0, 0), arrowprops=dict(arrowstyle="-|>", color="#ff9f1c", lw=2.6))
    ax.set_xlim(-1.35, 1.35); ax.set_ylim(-1.35, 1.35)
    ax.set_title("Shear vs tilt (plan view)", color="white", fontsize=10, pad=2)
    ax.text(-1.35, -1.62, "\u2192 850\u2013500 shear (ring = 20 m/s)", color="#ff9f1c", fontsize=8)
    ax.text(-1.35, -1.85, "\u2192 500 hPa centre vs 850 (ring = 150 km)", color="white", fontsize=8)

def _timeline(fig, rect, times, D, tnow, fc):
    ax = fig.add_axes(rect, facecolor=(0.03, 0.03, 0.03))
    t = times.astype("datetime64[m]").astype("O")
    for sp_ in ax.spines.values(): sp_.set_color("0.4")
    ax.tick_params(colors="0.8", labelsize=8)
    if np.isfinite(D["voff"]).any():
        ri = D["ri"]
        for i in range(len(t) - 1):
            if ri[i]: ax.axvspan(t[i], t[i + 1], color=(0.9, 0.15, 0.1), alpha=0.28, lw=0)
        ax.plot(t, D["voff"], color="white", lw=2.0)
        ax.text(0.005, 0.97, "white: official Vmax  \u00b7  blue: GFS 900 hPa max wind" +
                ("  \u00b7  red: RI (official \u0394V\u2082\u2084 \u2265 30 kt)" if ri.any() else ""),
                transform=ax.transAxes, color="0.85", fontsize=8, va="top")
    # forecast period
    if fc is not None and (fc > 0).any():
        i0 = int(np.argmax(fc > 0)); ax.axvspan(t[i0], t[-1], color="0.5", alpha=0.12, lw=0, hatch="//")
        ax.text(t[i0], 0.97, " GFS forecast", transform=ax.get_xaxis_transform(), color="0.7", fontsize=8, va="top")
    if CFG.get("landfall"):
        lf = np.datetime64(CFG["landfall"], "m").astype("O")
        if t[0] <= lf <= t[-1]:
            ax.axvline(lf, color="#ffd166", lw=1.2, ls=":")
            ax.text(lf, 0.80, " landfall", transform=ax.get_xaxis_transform(), color="#ffd166", fontsize=8)
    ax.plot(t, D["vmax"], color="#35d0ff", lw=1.4)
    ax.set_ylabel("Vmax (m/s)", color="0.85", fontsize=8)
    ax.set_ylim(0, max(CFG["vmax"], np.nanmax(D["vmax"]) * 1.1, np.nanmax(np.r_[D["voff"], 0]) * 1.1))
    ax2 = ax.twinx(); ax2.plot(t, D["rmw"] * 111, color="0.75", lw=1.1, ls="--")
    ax2.set_ylabel("RMW (km)", color="0.75", fontsize=8); ax2.tick_params(colors="0.8", labelsize=8)
    for sp_ in ax2.spines.values(): sp_.set_color("0.4")
    tn = np.datetime64(tnow, "m").astype("O")
    ax.axvline(tn, color="white", lw=1.4)
    ax.set_xlim(t[0], t[-1])
    import matplotlib.dates as mdates
    ax.xaxis.set_major_locator(mdates.DayLocator()); ax.xaxis.set_major_formatter(mdates.DateFormatter("%d-%b"))

# ----------------------------------------------------------------------------
# 5. RENDER  (phase 1: cheap sequential state; phase 2: frames drawn in parallel)
# ----------------------------------------------------------------------------
_CTX = {}
LAST_DIAG = None

def _latlon(c):
    lo = ((c[1] + 180) % 360) - 180
    return f"{abs(c[0]):.1f}\u00b0{'N' if c[0] >= 0 else 'S'} {abs(lo):.1f}\u00b0{'E' if lo >= 0 else 'W'}"
MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

def _stream_pieces(S, k):
    """Split streamlines (N, P, 3) into equal-length pieces of k segments; NaN tails are
    filled with the last valid point so every piece has the same shape (fast to draw)."""
    N, P_, _ = S.shape
    nc = (P_ - 1) // k
    idx = np.arange(nc)[:, None] * k + np.arange(k + 1)[None, :]           # (nc, k+1)
    C = S[:, idx]                                                          # (N, nc, k+1, 3)
    ok = np.isfinite(C).all(-1)                                            # valid prefix per piece
    nvalid = ok.cumprod(-1).sum(-1)                                        # (N, nc)
    keep = nvalid >= 2
    C = C[keep]; nv = nvalid[keep]
    last = C[np.arange(len(C)), nv - 1]                                    # last valid point
    fill = np.arange(k + 1)[None, :] >= nv[:, None]
    C = np.where(fill[..., None], last[:, None, :], C)
    mid = C[np.arange(len(C)), (nv - 1) // 2]
    return C, mid

def _frame_states(ds, track, trs, D):
    """Everything that depends on the previous frame (tracer particles) is done here, quickly."""
    times = ds.time.values; nt = len(times); H = CFG["view_half"]
    nfr = (nt - 1) * CFG["frames_per_hour"] + 1
    rng = np.random.default_rng(42)
    parts = None; trail = []; states = []
    for fr in range(nfr):
        tf = fr / CFG["frames_per_hour"]; it0 = int(np.floor(tf)); it1 = min(it0 + 1, nt - 1); a = tf - it0
        F = Field(ds, it0, it1, a)
        ctr = (1 - a) * track[it0] + a * track[it1]
        vc = (1 - a) * trs[it0] + a * trs[it1]
        rmw = float(_interp(D["rmw"], tf))
        if CFG["follow_storm"]:
            box = ((vc[1] - H / np.cos(np.deg2rad(vc[0])), vc[1] + H / np.cos(np.deg2rad(vc[0]))), (vc[0] - H, vc[0] + H))
        else:
            box = (CFG["lon_range"], CFG["lat_range"])
        if parts is None: parts = spawn_ring(CFG["n_particles"], ctr, rmw, F.p, rng)
        dt = CFG["particle_s_per_frame"] / CFG["particle_substeps"]
        for _ in range(CFG["particle_substeps"]):
            k1 = F.vel(parts); parts = parts + dt * F.vel(parts + 0.5 * dt * k1)
        bad = ~in_box(parts, box, F.p) | ~np.isfinite(parts).all(1)
        trail.append(parts.copy()); trail = trail[-CFG["trail_len"]:]
        if bad.any():
            parts[bad] = spawn_ring(bad.sum(), ctr, rmw, F.p, rng)
            for T in trail: T[bad] = np.nan
            trail[-1][bad] = parts[bad]
        psp = np.nan_to_num(F.isp(np.c_[parts[:, 2], parts[:, 1], parts[:, 0]]))
        states.append(dict(fr=fr, tf=tf, it0=it0, it1=it1, a=a, ctr=ctr, box=box, rmw=rmw,
                           parts=parts.copy(), trail=[T.copy() for T in trail], psp=psp))
    return states

def _draw_frame(st):
    ds, D, track, seeds, fc = _CTX["ds"], _CTX["D"], _CTX["track"], _CTX["seeds"], _CTX["fc"]
    times = ds.time.values; H = CFG["view_half"]
    fr, tf, it0, it1, a = st["fr"], st["tf"], st["it0"], st["it1"], st["a"]
    ctr, box, rmw = st["ctr"], st["box"], st["rmw"]
    cmap = plt.get_cmap("turbo"); norm = plt.Normalize(0, CFG["vmax"])
    dash = CFG["dashboard"]
    clean = CFG.get("clean", False)                     # hero film: flow only, no axes, text or panels
    if clean: dash = False
    main_rect = [-0.10, -0.16, 1.20, 1.32] if clean else [0.0, 0.19, 0.70, 0.76] if dash else [0.02, 0.02, 0.84, 0.90]
    cb_rect   = [0.695, 0.30, 0.010, 0.56] if dash else [0.87, 0.11, 0.018, 0.82]
    F = Field(ds, it0, it1, a)
    shear = _interp(D["shear"], tf); tilt = _interp(D["tilt"], tf)
    cen = _interp(D["cen"], tf); vmx = float(_interp(D["vmax"], tf))
    tnow = times[it0] + (times[it1] - times[it0]) * a if it1 != it0 else times[it0]
    is_fc = fc is not None and max(float(fc[it0]), float(fc[it1])) > 0

    fig = plt.figure(figsize=CFG["figsize"], dpi=CFG["dpi"], facecolor="black")
    ax = fig.add_axes(main_rect, projection="3d", facecolor="black")
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        axis.set_pane_color((0, 0, 0, 1))
        axis._axinfo["grid"].update(color=(0.30, 0.30, 0.30, 0.5), linewidth=0.5)
        axis.line.set_color("0.8")
    ax.tick_params(colors="0.85", labelsize=9)
    if clean: ax.set_axis_off()
    ax.computed_zorder = False
    if CFG["show_basemap"]:
        draw_basemap(ax, box, max(CFG["levels"]), tnow)

    # --- isosurfaces
    k850 = D["k850"]
    vlev = CFG["vo_iso_frac"] * np.nanmax(gaussian_filter(F.vo[k850], 0.8)) if CFG["vo_iso_frac"] else CFG["vo_iso"]
    for arr, lev, col, al in [(F.vo, vlev, (0.85, 0.08, 0.05), 0.16),
                              (-F.w, -CFG["w_iso"], (0.75, 0.75, 0.75), 0.07)]:
        tri = iso_mesh(F, arr, lev, box)
        if tri is not None:
            ax.add_collection3d(Poly3DCollection(tri, facecolor=col, edgecolor="none", alpha=al, zorder=2))

    # --- streamlines, drawn as short multi-point pieces (much faster than single segments)
    S = F.streamlines(seeds.place(ctr, rmw, box, F.p))[:, ::2]
    S[~in_box(S, box, F.p)] = np.nan
    C, mid = _stream_pieces(S, CFG["stream_chunk"])
    sp = np.nan_to_num(F.isp(np.c_[mid[:, 2], mid[:, 1], mid[:, 0]]))
    wgt = np.clip(sp / CFG["vmax"], 0, 1)
    col = cmap(norm(sp)); col[:, 3] = 0.10 + 0.65 * wgt ** 0.7
    ax.add_collection3d(Line3DCollection(C, colors=col, linewidths=0.25 + 0.9 * wgt, zorder=3))

    # --- tracer particles with fading trails (positions from phase 1)
    parts, trail, psp = st["parts"], st["trail"], st["psp"]
    if len(trail) > 1:
        tseg, tcol = [], []
        for n in range(len(trail) - 1):
            sg = np.stack([trail[n], trail[n + 1]], 1)
            ok = np.isfinite(sg).all(axis=(1, 2)) & (np.linalg.norm(sg[:, 1, :2] - sg[:, 0, :2], axis=1) < 3)
            c = cmap(norm(psp[ok])); c[:, 3] = 0.15 + 0.6 * (n + 1) / len(trail)
            tseg.append(sg[ok]); tcol.append(c)
        ax.add_collection3d(Line3DCollection(np.concatenate(tseg), colors=np.concatenate(tcol), linewidths=1.0, zorder=4))
    ax.scatter(parts[:, 0], parts[:, 1], parts[:, 2], s=6, marker="s",
               c=cmap(norm(psp)), depthshade=False, linewidths=0, zorder=5)

    # --- track on the floor
    zf = max(CFG["levels"])
    tk = np.c_[track[:, 1], track[:, 0], np.full(len(track), zf)]
    tk[~in_box(tk, box, [F.p[0], F.p[-1] + 1])] = np.nan
    ax.plot(tk[:, 0], tk[:, 1], tk[:, 2], color=(1, 1, 1, 0.8), lw=1.6, zorder=6)

    # --- vortex tilt axis and mid-level shear arrow
    if CFG["show_tilt"]:
        ax.plot(cen[:, 1], cen[:, 0], F.p, color="white", lw=2.4, zorder=7)
        ax.scatter(cen[:, 1], cen[:, 0], F.p, color="white", s=10, depthshade=False, zorder=7)
    if CFG["show_shear"]:
        sm = np.hypot(*shear); zt = min(CFG["levels"])
        if sm > 0.5:
            L = min(sm / 20.0, 1.0) * 0.6 * H
            dxs = shear[0] / sm * L / np.cos(np.deg2rad(ctr[0])); dys = shear[1] / sm * L
            c5 = cen[D["k500"]]
            ax.quiver(c5[1], c5[0], zt, dxs, dys, 0, color="#ff9f1c", lw=2.5, arrow_length_ratio=0.25, zorder=8)
            ax.text(c5[1] + dxs * 1.1, c5[0] + dys * 1.1, zt, f"{sm:.0f} m/s", color="#ff9f1c", fontsize=9, zorder=8)

    (lo0, lo1), (la0, la1) = box
    ax.set_xlim(lo0, lo1); ax.set_ylim(la0, la1); ax.set_zlim(zf, min(CFG["levels"]))
    ax.set_box_aspect((1, (la1 - la0) / (lo1 - lo0), 0.8), zoom=1.22 if clean else 1.05)
    xt = np.arange(np.ceil(lo0 / 2) * 2, lo1 + 0.01, 2); yt = np.arange(np.ceil(la0 / 2) * 2, la1 + 0.01, 2)
    ax.set_xticks(xt); ax.set_yticks(yt)
    ax.set_xticklabels([f"{abs(((x + 180) % 360) - 180):.0f}\u00b0" + ("" if abs(((x + 180) % 360) - 180) in (0, 180)
                        else ("E" if ((x + 180) % 360) - 180 > 0 else "W")) for x in xt])
    ax.set_yticklabels([f"{abs(y):.0f}\u00b0" + ("" if y == 0 else ("N" if y > 0 else "S")) for y in yt])
    ax.set_zticks(CFG["levels"])
    ph = fr / max(_CTX["nfr"] - 1, 1); e = 0.5 - 0.5 * np.cos(np.pi * ph)
    if clean:                                           # slow, low cinematic orbit
        ax.view_init(elev=CFG["camera_elev"], azim=CFG["camera_azim"]) if not CFG["orbit"] else ax.view_init(elev=20 - 8 * np.sin(np.pi * ph) ** 2, azim=-70 + 120 * e)
        out = os.path.join(CFG["frame_dir"], f"f{fr:04d}.png")
        fig.savefig(out, facecolor="black"); plt.close(fig)
        return out
    if CFG["orbit"]:                                    # optional orbiting camera (off by default)
        ax.view_init(elev=28 - 20 * np.sin(np.pi * ph) ** 2, azim=-60 + 330 * e)
    else:                                               # fixed camera: only the flow moves
        ax.view_init(elev=CFG["camera_elev"], azim=CFG["camera_azim"])

    cax = fig.add_axes(cb_rect)
    cb = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=cmap), cax=cax)
    cb.set_label("Wind speed (m/s)", color="white"); cb.ax.tick_params(colors="white", labelsize=8)
    cb.outline.set_edgecolor("0.6")

    ts = np.datetime_as_string(tnow, unit="m")
    lbl = f"{int(ts[8:10]):02d}-{MONTHS[int(ts[5:7]) - 1]} {ts[11:16]} UTC"
    el_c, lst_c = sun_elevation(tnow, ctr[0], ctr[1])
    hh = int(lst_c); mm = int(round((lst_c - hh) * 60)) % 60
    state = "day" if el_c > 0 else ("twilight" if el_c > -CFG["twilight_deg"] else "night")
    if dash:
        sm = np.hypot(*shear); sdir = (np.degrees(np.arctan2(shear[0], shear[1])) + 360) % 360
        tm = np.hypot(*tilt); tdir = (np.degrees(np.arctan2(tilt[0], tilt[1])) + 360) % 360
        x0, y = 0.745, 0.86
        fig.text(x0, y + 0.03, "DIAGNOSTICS", color="0.6", fontsize=10, fontweight="bold")
        vo_ = float(_interp(D["voff"], tf))
        rows = [("Official Vmax", f"{vo_ * 1.944:.0f} kt ({vo_:.0f} m/s)" if np.isfinite(vo_) else "n/a"),
                ("GFS max wind 900", f"{vmx:.1f} m/s"),
                ("RMW (850 hPa)", f"{rmw * 111:.0f} km"),
                ("Shear 850\u2013500", f"{sm:.1f} m/s from {(sdir + 180) % 360:03.0f}\u00b0"),
                ("Tilt 850\u2192500", f"{tm:.0f} km toward {tdir:03.0f}\u00b0"),
                ("Local solar time", f"{hh:02d}:{mm:02d}  ({state})"),
                ("Centre", _latlon(ctr))]
        for k_, v_ in rows:
            y -= 0.045
            fig.text(x0, y, k_, color="0.65", fontsize=10)
            fig.text(x0 + 0.095, y, v_, color="white", fontsize=10)
        if D["ri"][it0] or D["ri"][it1]:
            fig.text(x0, y - 0.055, "  RAPID INTENSIFICATION (official)  ", color="white", fontsize=11, fontweight="bold",
                     bbox=dict(facecolor=(0.8, 0.1, 0.08), edgecolor="none", boxstyle="round,pad=0.35"))
        _compass(fig, [0.78, 0.07, 0.17, 0.30], shear, tilt)
        _timeline(fig, [0.055, 0.05, 0.60, 0.13], times, D, tnow, fc)
    elif CFG["show_lst"]:
        fig.text(0.04, 0.04, f"Local solar time at centre: {hh:02d}:{mm:02d}  ({state})", color="white", fontsize=11)

    fig.suptitle(f"{CFG['storm_name']}  |  {CFG['title_tag']}  |  {lbl}" + ("   [GFS forecast]" if is_fc else ""),
                 color="white", fontsize=15, fontweight="bold", x=0.36 if dash else 0.5, y=0.975)
    fig.text(0.36 if dash else 0.5, 0.935, "GFS 0.25\u00b0 analyses, 900\u2013500 hPa, track and intensity from NCEP TCVitals.  "
             "Colour: wind speed.  Red: vortex core.  Grey: strong ascent.  White: vortex axis.",
             color="0.6", fontsize=8.5, ha="center")
    out = os.path.join(CFG["frame_dir"], f"f{fr:04d}.png")
    fig.savefig(out, facecolor="black"); plt.close(fig)
    return out

def render(ds, skip_hours=0, max_hours=None):
    import time as _t
    t_start = _t.time()
    os.makedirs(CFG["frame_dir"], exist_ok=True)
    for f in glob.glob(os.path.join(CFG["frame_dir"], "*.png")): os.remove(f)
    track = load_track(ds)
    print("computing diagnostics ...")
    D = diagnostics(ds, track)
    trs = np.c_[gaussian_filter(track[:, 0], 2, mode="nearest"), gaussian_filter(track[:, 1], 2, mode="nearest")]
    # structure summary for the written description (last analysis vs 24 h earlier)
    global LAST_DIAG
    tt = ds.time.values; n = len(tt) - 1
    k24 = int(np.argmin(np.abs(tt - (tt[n] - np.timedelta64(24, "h")))))
    has24 = abs((tt[n] - tt[k24]) / np.timedelta64(1, "h") - 24) <= 3
    shv = D["shear"][n]; tv = D["tilt"][n]
    LAST_DIAG = dict(shear_now=float(np.hypot(*shv)), shear_from=float((np.degrees(np.arctan2(shv[0], shv[1])) + 180) % 360),
                     tilt_now=float(np.hypot(*tv)), tilt_dir=float((np.degrees(np.arctan2(tv[0], tv[1])) + 360) % 360),
                     tilt_24=float(np.hypot(*D["tilt"][k24])) if has24 else None,
                     rmw_now=float(D["rmw"][n] * 111), rmw_24=float(D["rmw"][k24] * 111) if has24 else None,
                     gfs_vmax=float(D["vmax"][n]),
                     shear_deep=float(np.hypot(*D["shear_deep"][n])) if np.isfinite(D["shear_deep"][n]).all() else None,
                     rh_mid=float(D["rh_mid"][n]) if np.isfinite(D["rh_mid"][n]) else None,
                     sst=float(D["sst"][n]) if np.isfinite(D["sst"][n]) else None)
    if CFG.get("out_diag"):
        try: _diag_json(ds, D, CFG["out_diag"])
        except Exception as e: print("diagnostics export skipped:", e)
    print("advecting tracer particles ...")
    states = _frame_states(ds, track, trs, D)
    nfr = len(states)
    f0 = skip_hours * CFG["frames_per_hour"]
    f1 = nfr if max_hours is None else min(nfr, (skip_hours + max_hours) * CFG["frames_per_hour"] + 1)
    todo = states[f0:f1]
    _CTX.update(ds=ds, D=D, track=track, seeds=Seeds(CFG["n_seeds"], np.random.default_rng(42)),
                fc=ds.is_fcst.values if "is_fcst" in ds else None, nfr=nfr)

    nw = CFG["n_workers"] or max(1, (os.cpu_count() or 2) - 1)
    nw = min(nw, len(todo))
    files = []
    if nw > 1:
        import multiprocessing as mp
        try:
            ctx = mp.get_context("fork")            # Linux: workers inherit the data, no copying
        except ValueError:
            ctx = None
        if ctx is not None:
            print(f"drawing {len(todo)} frames on {nw} cores ...")
            with ctx.Pool(nw) as pool:
                for n, f in enumerate(pool.imap(_draw_frame, todo, chunksize=2)):
                    files.append(f); print(f"frame {n + 1}/{len(todo)}", end="\r")
        else:
            nw = 1
    if nw == 1:
        print(f"drawing {len(todo)} frames ...")
        for n, st in enumerate(todo):
            files.append(_draw_frame(st)); print(f"frame {n + 1}/{len(todo)}", end="\r")

    # --- MP4 + poster only
    import imageio_ffmpeg
    os.makedirs(os.path.dirname(CFG["out_mp4"]) or ".", exist_ok=True)
    fps = 1000.0 / CFG["frame_ms"]
    w, h = Image.open(files[0]).size; w2, h2 = w - w % 2, h - h % 2
    wr = imageio_ffmpeg.write_frames(CFG["out_mp4"], (w2, h2), fps=fps, codec="libx264", pix_fmt_out="yuv420p",
                                     quality=7, macro_block_size=1, output_params=["-movflags", "+faststart"])
    wr.send(None)
    for f in files + [files[-1]] * int(CFG["hold_last_ms"] / CFG["frame_ms"]):
        wr.send(np.ascontiguousarray(np.asarray(Image.open(f).convert("RGB"))[:h2, :w2]))
    wr.close()
    Image.open(files[-1]).convert("RGB").save(CFG["out_poster"], quality=82)
    print(f"\nsaved {CFG['out_mp4']}")
    print(f"done in {(_t.time() - t_start) / 60:.1f} min")

