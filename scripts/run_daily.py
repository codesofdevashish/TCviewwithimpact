#!/usr/bin/env python3
"""
Daily job (run by GitHub Actions):
  1. find every active tropical cyclone in the world (NCEP TCVitals)
  2. for each: download GFS 0.25 deg for its region, render the 3D flow video
  3. delete all downloaded data and frames straight away
  4. carry forward videos of past storms from the live site (archive)
  5. write public/ = website + videos + storms.json   <- the ONLY thing that gets hosted
"""
import argparse, json, math, os, shutil, sys, time, traceback
import numpy as np
import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "scripts"))
import storms as S
import narrative as N
import impact as I
import tc3d.render as R


def _domain(track, pad=9.0, min_span=22.0):
    lat = track.lat.values; lon = track.lon.values
    lon180 = ((lon + 180) % 360) - 180; lon360 = lon % 360
    use360 = np.ptp(lon360) < np.ptp(lon180)                 # dateline storms
    lo = lon360 if use360 else lon180
    la0, la1 = lat.min() - pad, lat.max() + pad
    lo0, lo1 = lo.min() - pad, lo.max() + pad
    if la1 - la0 < min_span: c = (la0 + la1) / 2; la0, la1 = c - min_span / 2, c + min_span / 2
    if lo1 - lo0 < min_span: c = (lo0 + lo1) / 2; lo0, lo1 = c - min_span / 2, c + min_span / 2
    la0, la1 = max(la0, -65.0), min(la1, 65.0)
    return (round(la0), round(la1)), (math.floor(lo0), math.ceil(lo1)), bool(use360)


def render_storm(st, latest, work, public, max_days, hero=False):
    key = st["key"]; wdir = os.path.join(work, key)
    t_end = min(st["last_seen"], latest).floor("3h")
    t_start = max(st["first_seen"].floor("3h"), t_end - pd.Timedelta(days=max_days))
    if t_end - t_start < pd.Timedelta(hours=12):
        t_start = t_end - pd.Timedelta(hours=12)
    tr = st["track"]; tr = tr[(tr.time >= t_start - pd.Timedelta("6h")) & (tr.time <= t_end + pd.Timedelta("6h"))]
    latr, lonr, use360 = _domain(tr)
    peak_ms = float(tr.vmax_ms.max())
    R._LAND = None                                             # reset coastline cache per storm
    R._CTX.clear()
    R.CFG.update(
        storm_name=f"{st['label']} {st['name']} ({st['sid']})" if st["name"] != st["sid"] else f"{st['label']} {st['sid']}",
        t_start=str(t_start), t_end=str(t_end), lat_range=latr, lon_range=lonr, lon360=use360,
        hemi=1 if tr.lat.mean() >= 0 else -1, track_df=tr.copy(),
        vmax=float(np.clip(math.ceil(peak_ms * 1.2 / 10) * 10, 30, 80)),
        data_file=os.path.join(wdir, "gfs.nc"), gfs_cache=os.path.join(wdir, "cache"),
        frame_dir=os.path.join(wdir, "frames"),
        out_mp4=os.path.join(public, "videos", f"{key}.mp4"),
        out_poster=os.path.join(public, "videos", f"{key}.jpg"),
        out_diag=os.path.join(public, "data", "storms", f"{key}.json"))
    os.makedirs(wdir, exist_ok=True)
    try:
        R.download_gfs()
        ds = R.load(R.CFG["data_file"])
        R.LAST_DIAG = None
        R.render(ds)
        tt = pd.to_datetime(ds.time.values)
        diag_keep = R.LAST_DIAG
        if hero:                                   # cinematic backdrop for the opening of the site
            try:
                saved = {k: R.CFG[k] for k in ("clean", "figsize", "dpi", "frame_dir", "out_mp4", "out_poster", "out_diag", "show_tilt", "show_shear") if k in R.CFG}
                R.CFG.update(clean=True, figsize=(16, 9), dpi=120, frame_dir=os.path.join(wdir, "hero_frames"),
                             out_mp4=os.path.join(public, "videos", "hero.mp4"), out_poster=os.path.join(public, "videos", "hero.jpg"),
                             out_diag=None, show_tilt=False, show_shear=False)
                R._CTX.clear(); R.render(ds)
                print("  hero film written")
            except Exception:
                traceback.print_exc()
            finally:
                R.CFG.update(saved); R.CFG["clean"] = False; R.LAST_DIAG = diag_keep
        return dict(window_start=tt[0].strftime("%Y-%m-%dT%H:%MZ"), window_end=tt[-1].strftime("%Y-%m-%dT%H:%MZ"))
    finally:
        shutil.rmtree(wdir, ignore_errors=True)                 # delete data + frames immediately


def _json_track(tr):
    return [[t.strftime("%Y-%m-%dT%H:%MZ"), round(float(a), 2), round(float(o), 2), round(float(v) * 1.944)]
            for t, a, o, v in zip(tr.time, tr.lat, tr.lon, tr.vmax_ms)]


def _ri_any(tr, thr_kt=30):
    tt = tr.time.values.astype("datetime64[h]").astype(int); v = tr.vmax_ms.values * 1.944
    for i, t in enumerate(tt):
        j = np.where(np.abs(tt - (t + 24)) <= 3)[0]
        if len(j) and (v[j] - v[i]).max() >= thr_kt:
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-url", default="", help="live site, used to carry forward archived videos")
    ap.add_argument("--public", default=os.path.join(ROOT, "public"))
    ap.add_argument("--work", default=os.path.join(ROOT, "work"))
    ap.add_argument("--max-days", type=float, default=5, help="length of each video window (days)")
    ap.add_argument("--max-storms", type=int, default=12)
    ap.add_argument("--archive-max", type=int, default=40)
    ap.add_argument("--include-invests", action="store_true")
    ap.add_argument("--exposure", default=os.environ.get("TC_EXPOSURE", os.path.expanduser("~/.cache/tc-exposure")),
                    help="folder prepared by scripts/fetch_exposure.py")
    ap.add_argument("--max-new-archive", type=int, default=6, help="most ended storms rendered fresh per day")
    a = ap.parse_args()
    EXP = I.load_exposure(a.exposure)                                     # population, countries, vulnerability

    shutil.rmtree(a.public, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "site"), a.public)
    os.makedirs(os.path.join(a.public, "videos"), exist_ok=True); os.makedirs(os.path.join(a.public, "data"), exist_ok=True)
    build = pd.Timestamp.now("UTC").strftime("%Y%m%d%H%M")        # cache-busting: browsers always load today's files
    idx = os.path.join(a.public, "index.html")
    html = open(idx, encoding="utf-8").read()
    with open(idx, "w", encoding="utf-8") as f:
        f.write(html.replace("__BUILD__", build))
    sess = requests.Session(); sess.headers["User-Agent"] = "tc3d-site/1.0"

    # previous site data (for the archive and as a fallback if a render fails)
    prev = {}; prev_hero = None
    if a.site_url:
        try:
            r = sess.get(a.site_url.rstrip("/") + "/data/storms.json", timeout=30)
            if r.ok:
                pj = r.json(); prev = {s["key"]: s for s in pj.get("storms", [])}; prev_hero = pj.get("hero")
        except Exception as e:
            print("no previous site data:", e)

    def carry(s):
        """Copy an existing video + poster (and diagnostics, if any) from the live site into public/."""
        ok = True
        for fld in ("diag", "impact"):                                   # small per-storm data files
            if not s.get(fld): continue
            try:
                r = sess.get(a.site_url.rstrip("/") + "/" + s[fld], timeout=60)
                if r.ok:
                    os.makedirs(os.path.dirname(os.path.join(a.public, s[fld])), exist_ok=True)
                    open(os.path.join(a.public, s[fld]), "wb").write(r.content)
                else:
                    s.pop(fld, None)
            except Exception:
                s.pop(fld, None)
        for ext in ("mp4", "jpg"):
            dst = os.path.join(a.public, "videos", f"{s['key']}.{ext}")
            try:
                r = sess.get(a.site_url.rstrip("/") + f"/videos/{s['key']}.{ext}", timeout=120)
                if r.ok: open(dst, "wb").write(r.content)
                else: ok = False
            except Exception:
                ok = False
        return ok

    active, ended, latest = S.find_storms(include_invests=a.include_invests)
    print(f"latest TCVitals cycle: {latest}")
    print(f"  active storms: {[s['key'] for s in active]}")
    print(f"  ended in the last 8 days: {[s['key'] for s in ended]}")
    active = sorted(active, key=lambda s: -s["vmax_kt"])[:a.max_storms]

    def entry(st, info, desc, status):
        return dict(key=st["key"], sid=st["sid"], year=st["year"], name=st["name"], label=st["label"],
                    category=st["category"], basin=st["basin"], basin_name=st["basin_name"], centre=st["centre"],
                    status=status, lat=st["lat"], lon=st["lon"], vmax_kt=st["vmax_kt"], pmin=st["pmin"],
                    peak_kt=st["peak_kt"], ri=_ri_any(st["track"]),
                    first_seen=st["first_seen"].strftime("%Y-%m-%dT%H:%MZ"),
                    last_seen=st["last_seen"].strftime("%Y-%m-%dT%H:%MZ"),
                    track=_json_track(st["track"]), video=f"videos/{st['key']}.mp4", poster=f"videos/{st['key']}.jpg",
                    rendered=pd.Timestamp.now("UTC").strftime("%Y-%m-%dT%H:%MZ"),
                    story=desc.get("story", []), motion=desc.get("motion"), dv24=desc.get("dv24"),
                    trend=desc.get("trend"), env=desc.get("env"),
                    video_fps=round(1000.0 / R.CFG["frame_ms"], 4), video_fph=R.CFG["frames_per_hour"],
                    **({"diag": f"data/storms/{st['key']}.json"} if os.path.exists(os.path.join(a.public, "data", "storms", f"{st['key']}.json")) else {}),
                    **info)

    def make(st, status, hero=False):
        """Render (or, if that fails, copy yesterday's video) and describe one storm."""
        ended_ = status == "archived"
        print(f"\n=== {st['key']}  {st['label']} {st['name']}  {st['vmax_kt']} kt  ({status}) ===")
        t0 = time.time(); info = {}
        try:
            info = render_storm(st, latest, a.work, a.public, a.max_days, hero=hero); ok = True
        except Exception:
            traceback.print_exc(); ok = st["key"] in prev and carry(prev[st["key"]])
        if not ok:
            print("  skipped (no video)"); return None
        try:
            desc = N.describe(st, R.LAST_DIAG if info else None, N.geography(), ended=ended_)
        except Exception:
            traceback.print_exc(); desc = {}
        if not info and st["key"] in prev:                         # render failed: keep yesterday's structure text
            old = prev[st["key"]].get("story", [])
            if len(old) > 1 and len(desc.get("story", [])) == 1: desc["story"].append(old[1])
            for k in ("window_start", "window_end"):
                if prev[st["key"]].get(k): info[k] = prev[st["key"]][k]
        imp_fields = {}
        if EXP is not None:
            try:
                imp = I.compute(st["track"], EXP, south=float(st["track"].lat.mean()) < 0)
                if imp:
                    path = f"data/impact/{st['key']}.json"
                    os.makedirs(os.path.join(a.public, "data", "impact"), exist_ok=True)
                    json.dump(imp, open(os.path.join(a.public, path), "w"), separators=(",", ":"))
                    sm = imp["summary"]
                    imp_fields = dict(impact=path, impact_summary=dict(pop34=sm["pop34"], pop64=sm["pop64"],
                                      top=imp["countries"][0]["name"] if imp["countries"] else None))
                    line = I.headline(imp)
                    if line and desc.get("story"): desc["story"][0] = desc["story"][0] + " " + line
                    print(f"  impact: {sm['pop34']:,} people in winds of 34 kt or more")
            except Exception:
                traceback.print_exc()
        elif st["key"] in prev and prev[st["key"]].get("impact"):         # keep yesterday's impact layer if today's failed
            imp_fields = dict(impact=prev[st["key"]]["impact"], impact_summary=prev[st["key"]].get("impact_summary"))
        print(f"  done in {(time.time() - t0) / 60:.1f} min")
        e = entry(st, info, desc, status); e.update(imp_fields)
        return e

    out = []
    for n, st in enumerate(active):
        e = make(st, "active")
        if e: out.append(e)
    hero = None

    # archive 1: storms that ended in the last 8 days (from TCVitals history, so this works on a new site)
    done = {s["key"] for s in out}
    new_renders = 0
    for st in sorted(ended, key=lambda s: s["last_seen"], reverse=True):
        if st["key"] in done: continue
        old = prev.get(st["key"])
        if old and old.get("status") == "archived" and carry(old):    # already archived earlier: just copy it
            out.append(old); done.add(st["key"]); continue
        if old and old.get("status") == "active" and carry(old):      # ended since yesterday: keep its last video,
            try:                                                      # but rewrite the text in the past tense
                env = old.get("env") or {}
                diag = env if "shear_now" in env and "tilt_now" in env else None
                d = N.describe(st, diag, N.geography(), ended=True)
                if d.get("story"): old["story"] = d["story"]
            except Exception:
                traceback.print_exc()
            old.update(status="archived", motion=None, trend=None, dv24=None, label=st["label"])
            out.append(old); done.add(st["key"]); continue
        if new_renders >= a.max_new_archive: continue
        e = make(st, "archived"); new_renders += 1                    # never on the site: render it now
        if e: out.append(e); done.add(st["key"])

    # archive 2: older storms kept from the previous site
    arch = sorted([s for k, s in prev.items() if k not in done], key=lambda s: s.get("last_seen", ""), reverse=True)
    n_arch = sum(s["status"] == "archived" for s in out)
    for s in arch[:max(0, a.archive_max - n_arch)]:
        if carry(s):
            s["status"] = "archived"; out.append(s)

    if False:                                              # opening film retired (the Earth is the opening now)
        try:
            ok = True
            for ext in ("mp4", "jpg"):
                r = sess.get(a.site_url.rstrip("/") + f"/videos/hero.{ext}", timeout=120)
                if r.ok: open(os.path.join(a.public, "videos", f"hero.{ext}"), "wb").write(r.content)
                else: ok = False
            if ok: hero = prev_hero
        except Exception:
            pass
    json.dump(dict(hero=hero, generated=pd.Timestamp.now("UTC").strftime("%Y-%m-%dT%H:%MZ"),
                   latest_cycle=str(latest) if latest is not None else None, storms=out),
              open(os.path.join(a.public, "data", "storms.json"), "w"), indent=1)
    shutil.rmtree(a.work, ignore_errors=True)
    n_act = sum(s["status"] == "active" for s in out)
    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(a.public) for f in fs) / 1e6
    print(f"\nsite ready: {n_act} active, {len(out) - n_act} archived, {size:.0f} MB in {a.public}")


if __name__ == "__main__":
    main()
    # Everything is written. Exit straight away: letting Python unload the compiled GRIB / netCDF /
    # PROJ libraries at shutdown can abort with "double free or corruption" (exit 134).
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0)
