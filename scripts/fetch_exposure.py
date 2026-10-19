#!/usr/bin/env python3
"""
Prepare the exposure and vulnerability data for the impact layer (run once, then cached).

  population  GHSL GHS-POP R2023A, epoch 2025, 30 arcsec (EU JRC, open, no login)
  countries   Natural Earth admin-0 (via cartopy), rasterised to the same grid
  economy     World Bank WDI: GDP per capita, PPP (current international $)
  vulnerability  World Bank WDI, country level (transparent, coarse first version):
                 GDP per capita PPP, under-5 mortality, access to electricity

Output (in --out, default ~/.cache/tc-exposure):
  pop_0p1.npy       float32 [1800, 3600] people per 0.1° cell, row 0 = 90°N, col 0 = 180°W
  country_0p1.npy   int16   [1800, 3600] index into countries.json (-1 = no country)
  countries.json    [{iso3, name, gdppc, u5mr, elec, vuln}]
"""
import argparse, io, json, math, os, sys, zipfile
import numpy as np
import requests

GHSL = ("https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_POP_GLOBE_R2023A/"
        "GHS_POP_E2025_GLOBE_R2023A_4326_30ss/V1-0/GHS_POP_E2025_GLOBE_R2023A_4326_30ss_V1_0.zip")
WB = "https://api.worldbank.org/v2/country/all/indicator/{code}?format=json&mrnev=1&per_page=100&page={page}"
INDICATORS = {"gdppc": "NY.GDP.PCAP.PP.CD", "u5mr": "SH.DYN.MORT", "elec": "EG.ELC.ACCS.ZS"}
NY, NX, D = 1800, 3600, 0.1


def population(cache, sess):
    """GHSL 30" -> 0.1° grid (sum of people per cell)."""
    import rasterio
    zpath = os.path.join(cache, "ghs_pop_2025_30ss.zip")
    if not os.path.exists(zpath):
        print("downloading GHSL population (about 450 MB, once)...")
        with sess.get(GHSL, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(zpath + ".part", "wb") as f:
                for chunk in r.iter_content(1 << 22): f.write(chunk)
        os.replace(zpath + ".part", zpath)
    with zipfile.ZipFile(zpath) as z:
        tif = [n for n in z.namelist() if n.lower().endswith(".tif")][0]
        tpath = os.path.join(cache, os.path.basename(tif))
        if not os.path.exists(tpath): z.extract(tif, cache); os.replace(os.path.join(cache, tif), tpath)
    out = np.zeros(NY * NX, np.float64)
    with rasterio.open(tpath) as src:
        T = src.transform; W, Hh = src.width, src.height
        lon_c = T.c + (np.arange(W) + 0.5) * T.a
        col = np.clip(np.floor((lon_c + 180) / D).astype(np.int64), 0, NX - 1)
        nod = src.nodata
        for r0 in range(0, Hh, 600):
            win = rasterio.windows.Window(0, r0, W, min(600, Hh - r0))
            a = src.read(1, window=win).astype(np.float64)
            if nod is not None: a[a == nod] = 0
            a[~np.isfinite(a) | (a < 0)] = 0
            lat_c = T.f + (np.arange(r0, r0 + a.shape[0]) + 0.5) * T.e
            row = np.clip(np.floor((90 - lat_c) / D).astype(np.int64), 0, NY - 1)
            idx = (row[:, None] * NX + col[None, :]).ravel()
            out += np.bincount(idx, weights=a.ravel(), minlength=NY * NX)
            print(f"  population rows {r0}/{Hh}", end="\r")
    print(f"\n  world population on the grid: {out.sum() / 1e9:.2f} billion")
    return out.reshape(NY, NX).astype(np.float32)


def countries_raster():
    import cartopy.io.shapereader as shp
    from shapely import contains_xy
    recs = list(shp.Reader(shp.natural_earth("50m", "cultural", "admin_0_countries")).records())
    lat = 90 - (np.arange(NY) + 0.5) * D; lon = -180 + (np.arange(NX) + 0.5) * D
    cid = np.full((NY, NX), -1, np.int16); meta = []
    for i, r in enumerate(recs):
        a = r.attributes; iso = a.get("ISO_A3_EH") or a.get("ISO_A3") or a.get("ADM0_A3")
        meta.append(dict(iso3=iso, name=a.get("NAME_EN") or a.get("NAME")))
        g = r.geometry
        if g is None: continue
        x0, y0, x1, y1 = g.bounds
        js = np.where((lat >= y0) & (lat <= y1))[0]; is_ = np.where((lon >= x0) & (lon <= x1))[0]
        if not len(js) or not len(is_): continue
        LON, LAT = np.meshgrid(lon[is_], lat[js])
        m = contains_xy(g, LON, LAT)
        sub = cid[np.ix_(js, is_)]; sub[m] = i; cid[np.ix_(js, is_)] = sub
    return cid, meta


def world_bank(sess, tries=5):
    """Latest non-empty value per country for each indicator, in small pages with retries.
    Returns None if the World Bank service cannot be reached."""
    import time
    vals = {}
    for key, code in INDICATORS.items():
        page, pages = 1, 1
        while page <= pages:
            for attempt in range(tries):
                try:
                    r = sess.get(WB.format(code=code, page=page), timeout=(20, 90)); r.raise_for_status()
                    meta, rows = r.json()[0], (r.json()[1] or [])
                    break
                except Exception as e:
                    wait = 10 * (attempt + 1)
                    print(f"  World Bank {code} page {page}: {type(e).__name__}, retrying in {wait} s")
                    time.sleep(wait)
            else:
                return None
            pages = int(meta.get("pages", 1))
            for row in rows:
                iso = row.get("countryiso3code"); v = row.get("value")
                if iso and v is not None: vals.setdefault(iso, {})[key] = float(v)
            page += 1
        print(f"  World Bank {code}: {sum(key in v for v in vals.values())} countries")
    return vals


def vulnerability(d):
    """0 = least, 1 = most vulnerable. Mean of the components that are available."""
    parts = []
    if "gdppc" in d: parts.append(1 - (math.log10(max(d["gdppc"], 1)) - 3) / 2)       # $1k -> 1, $100k -> 0
    if "u5mr" in d: parts.append(d["u5mr"] / 100)                                       # deaths per 1000 births
    if "elec" in d: parts.append(1 - d["elec"] / 100)
    parts = [min(1, max(0, p)) for p in parts]
    return round(sum(parts) / len(parts), 3) if parts else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/.cache/tc-exposure"))
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    f_pop, f_cid, f_base, f_cty = (os.path.join(a.out, f) for f in ("pop_0p1.npy", "country_0p1.npy", "countries_base.json", "countries.json"))
    sess = requests.Session(); sess.headers["User-Agent"] = "tc3d-site/1.0"

    # 1. the expensive, stable part: population and country grids (saved at once, never repeated)
    if not (os.path.exists(f_pop) and os.path.exists(f_cid) and os.path.exists(f_base)):
        pop = population(a.out, sess); np.save(f_pop, pop); del pop
        cid, meta = countries_raster(); np.save(f_cid, cid); json.dump(meta, open(f_base, "w"))
        for f in os.listdir(a.out):                                # keep the cache small: drop the big source files
            if f.endswith((".zip", ".tif")): os.remove(os.path.join(a.out, f))
        print("population and country grids saved")
    else:
        print("population and country grids already prepared")

    # 2. the small, refreshable part: World Bank indicators -> vulnerability (tried on every run)
    meta = json.load(open(f_base))
    wb = world_bank(sess)
    if wb is None:
        if os.path.exists(f_cty):
            print("World Bank service unavailable: keeping the previous country table"); return
        print("World Bank service unavailable: using a neutral vulnerability (0.5) until the next run")
        wb, source = {}, "fallback"
    else:
        source = "worldbank"
    vs = [vulnerability(wb.get(m["iso3"], {})) for m in meta]
    known = [v for v in vs if v is not None]; fill = float(np.median(known)) if known else 0.5
    for m, v in zip(meta, vs):
        d = wb.get(m["iso3"], {})
        m.update(gdppc=d.get("gdppc"), u5mr=d.get("u5mr"), elec=d.get("elec"), vuln=v if v is not None else fill,
                 vuln_filled=v is None, source=source)
    json.dump(meta, open(f_cty, "w"))
    print(f"exposure data ready: {a.out} (vulnerability from {source})")


if __name__ == "__main__":
    main()
