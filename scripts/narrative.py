"""
Plain-language description of a storm, built only from data:
  official track/intensity (TCVitals) + GFS structure diagnostics (tc3d.render) + Natural Earth geography.
Every sentence is a direct reading of a number, so nothing is invented.
"""
import math
import numpy as np
import pandas as pd

MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
PTS = ["north", "north-northeast", "northeast", "east-northeast", "east", "east-southeast", "southeast",
       "south-southeast", "south", "south-southwest", "southwest", "west-southwest", "west",
       "west-northwest", "northwest", "north-northwest"]

def _d(t):  return f"{t.day} {MON[t.month - 1]}"
def _dt(t): return f"{t.day} {MON[t.month - 1]} {t:%H} UTC"
def _pos(lat, lon):
    lo = ((lon + 180) % 360) - 180
    return f"{abs(lat):.1f}°{'N' if lat >= 0 else 'S'} {abs(lo):.1f}°{'E' if lo >= 0 else 'W'}"
def _km(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2); dl = math.radians(lo2 - lo1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(a)))
def _bearing(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2); dl = math.radians(lo2 - lo1)
    x = math.sin(dl) * math.cos(p2); y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360
def _compass(b): return PTS[int((b + 11.25) // 22.5) % 16]

def stage(kt, basin):
    if kt >= 64:
        return {"L": "hurricane", "E": "hurricane", "C": "hurricane", "W": "typhoon"}.get(basin, "cyclone")
    if kt >= 34:
        return "cyclonic storm" if basin in "AB" else "tropical storm"
    return "depression" if basin in "AB" else "tropical depression"


# ---------------------------------------------------------------- geography (optional)
_GEO = None
def geography():
    """(land union, [(country name, geometry)]) from Natural Earth via cartopy; None if unavailable."""
    global _GEO
    if _GEO is None:
        try:
            import cartopy.io.shapereader as shp
            from shapely.ops import unary_union
            land = unary_union(list(shp.Reader(shp.natural_earth("50m", "physical", "land")).geometries()))
            ctry = [(r.attributes.get("NAME_EN") or r.attributes.get("NAME"), r.geometry)
                    for r in shp.Reader(shp.natural_earth("50m", "cultural", "admin_0_countries")).records()]
            _GEO = (land, ctry)
        except Exception as e:
            print("geography unavailable:", e); _GEO = False
    return _GEO or None

def _nearest_country(lat, lon, geo, max_km=1500):
    from shapely.geometry import Point
    from shapely.ops import nearest_points
    land, ctry = geo
    lo = ((lon + 180) % 360) - 180; best = None
    for name, g in ctry:
        if g is None: continue
        for sh in (0, 360, -360):                     # storms near the dateline
            p = Point(lo + sh, lat)
            if g.distance(p) > max_km / 111 * 1.6: continue
            q = nearest_points(g, p)[0]
            d = _km(lat, lo + sh, q.y, q.x)
            if best is None or d < best[0]:
                best = (d, name, q.y, q.x - sh)
    return best

def _on_land(lat, lon, geo):
    from shapely.geometry import Point
    return geo[0].contains(Point(((lon + 180) % 360) - 180, lat))


# ---------------------------------------------------------------- description
def describe(st, diag=None, geo=None, ended=False):
    tr = st["track"].sort_values("time").reset_index(drop=True)
    t = pd.to_datetime(tr.time); v = (tr.vmax_ms * 1.944).round().astype(int).values
    lat, lon = tr.lat.values, tr.lon.values; basin = st["basin"]; name = st["name"]
    now = t.iloc[-1]; out = {}

    # --- motion (last ~12 h)
    j = int(np.argmin(np.abs((t - (now - pd.Timedelta("12h"))).dt.total_seconds().values)))
    motion = None
    if j < len(t) - 1:
        hrs = (now - t.iloc[j]).total_seconds() / 3600
        d = _km(lat[j], lon[j], lat[-1], lon[-1]); spd = d / max(hrs, 1)
        motion = "nearly stationary" if spd < 3 else f"moving {_compass(_bearing(lat[j], lon[j], lat[-1], lon[-1]))} at {spd:.0f} km/h"
        out["motion"] = motion
    dist = sum(_km(lat[i], lon[i], lat[i + 1], lon[i + 1]) for i in range(len(t) - 1))

    # --- intensity change
    def v_ago(h):
        k = np.where(np.abs((t - (now - pd.Timedelta(hours=h))).dt.total_seconds().values) <= 3 * 3600)[0]
        return int(v[k[0]]) if len(k) else None
    v24, v48 = v_ago(24), v_ago(48)
    dv24 = None if v24 is None else int(v[-1] - v24); out["dv24"] = dv24
    if dv24 is None: trend = None
    elif dv24 >= 30: trend = "rapidly intensifying"
    elif dv24 >= 10: trend = "intensifying"
    elif dv24 <= -30: trend = "rapidly weakening"
    elif dv24 <= -10: trend = "weakening"
    else: trend = "holding steady"
    out["trend"] = trend

    # --- official RI episodes (>= 30 kt / 24 h)
    ri = None
    for i in range(len(t)):
        k = np.where(np.abs((t - (t.iloc[i] + pd.Timedelta("24h"))).dt.total_seconds().values) <= 3 * 3600)[0]
        if len(k) and v[k[0]] - v[i] >= 30:
            gain = int(v[k[0]] - v[i])
            if ri is None or gain > ri[2]: ri = (t.iloc[i], t.iloc[k[0]], gain, int(v[i]), int(v[k[0]]))

    # --- paragraph 1: track and intensity
    p = []
    p.append(f"{name} was first tracked on {_d(t.iloc[0])} near {_pos(lat[0], lon[0])} as a "
             f"{stage(v[0], basin)} with winds of {v[0]} kt.")
    ip = int(np.argmax(v))
    if ended:                                             # storm has dissipated: past tense throughout
        if len(t) > 1:
            p.append(f"It travelled about {dist:,.0f} km before its last advisory on {_dt(t.iloc[-1])}, when it had winds of {v[-1]} kt.")
        if ip != len(v) - 1 and v[ip] > v[-1]:
            p.append(f"It peaked at {v[ip]} kt on {_dt(t.iloc[ip])}.")
        else:
            p.append("That was also its peak intensity.")
        if ri:
            p.append(f"The warning agency's intensities show rapid intensification from {_dt(ri[0])} to {_dt(ri[1])}, "
                     f"when winds rose from {ri[3]} to {ri[4]} kt ({ri[2]:+d} kt in 24 hours).")
        elif v.max() >= 34:
            p.append("Official intensities show no 24-hour increase of 30 kt or more, so it did not undergo rapid intensification.")
        if geo is not None:
            try:
                over = [_on_land(a, o, geo) for a, o in zip(lat, lon)]
                for i in range(1, len(over)):
                    if over[i] and not over[i - 1]:
                        nc = _nearest_country(lat[i], lon[i], geo, 300)
                        p.append(f"It crossed the coast{(' over ' + nc[1]) if nc else ''} between {_dt(t.iloc[i - 1])} and "
                                 f"{_dt(t.iloc[i])} at about {int(round((v[i - 1] + v[i]) / 2))} kt.")
                if over[-1]:
                    nc = _nearest_country(lat[-1], lon[-1], geo, 50)
                    p.append(f"Its final advisory placed it over land{(' in ' + nc[1]) if nc else ''}.")
            except Exception as e:
                print("geography sentence skipped:", e)
        out["story"] = [" ".join(p)]
        if diag:
            sh, tl = diag["shear_now"], diag["tilt_now"]
            word = "light" if sh < 5 else "moderate" if sh < 10 else "strong"
            out["story"].append(
                f"In the last GFS analysis of its video the 850–500 hPa shear was "
                + ("negligible" if sh < 1 or "shear_from" not in diag else f"{word}, {sh:.0f} m/s from the {_compass(diag['shear_from'])}")
                + f", and the vortex was {'nearly upright' if tl < 50 else 'moderately tilted' if tl < 150 else 'strongly tilted'}"
                + ("." if tl < 10 or "tilt_dir" not in diag else f", with the 500 hPa centre {tl:.0f} km {_compass(diag['tilt_dir'])} of the 850 hPa centre."))
            out["env"] = {k: round(float(val), 1) for k, val in diag.items() if val is not None}
        out.pop("motion", None); out["trend"] = None; out["dv24"] = None
        return out
    if len(t) > 1:
        p.append(f"It has since travelled about {dist:,.0f} km" + (f" and is now {motion}." if motion else "."))
    tail = (f", and has been {trend} over the past day ({dv24:+d} kt in 24 hours)."
            if dv24 is not None and trend != "holding steady" else ".")
    if v[-1] >= v[ip]:
        p.append(f"At {v[-1]} kt it is at its strongest so far" + tail)
    elif v[-1] >= v[ip] - 5:
        p.append(f"At {v[-1]} kt it is close to its peak of {v[ip]} kt, reached on {_dt(t.iloc[ip])}" + tail)
    else:
        p.append(f"It peaked at {v[ip]} kt on {_dt(t.iloc[ip])} and is now {v[-1]} kt"
                 + (f", {trend} ({dv24:+d} kt in 24 hours)." if dv24 is not None else "."))
    if ri:
        p.append(f"The warning agency's intensities show rapid intensification from {_dt(ri[0])} to {_dt(ri[1])}, "
                 f"when winds rose from {ri[3]} to {ri[4]} kt ({ri[2]:+d} kt in 24 hours).")
    elif v.max() >= 34:
        p.append("Official intensities show no 24-hour increase of 30 kt or more, so it has not undergone rapid intensification.")
    if not np.isnan(tr.pmin.values[-1]):
        p.append(f"The latest central pressure is {tr.pmin.values[-1]:.0f} hPa.")

    # --- geography
    if geo is not None:
        try:
            over = [_on_land(a, o, geo) for a, o in zip(lat, lon)]
            for i in range(1, len(over)):
                if over[i] and not over[i - 1]:
                    nc = _nearest_country(lat[i], lon[i], geo, 300)
                    where = f" over {nc[1]}" if nc else ""
                    p.append(f"It crossed the coast{where} between {_dt(t.iloc[i - 1])} and {_dt(t.iloc[i])} "
                             f"at about {int(round((v[i - 1] + v[i]) / 2))} kt.")
            if over[-1]:
                nc = _nearest_country(lat[-1], lon[-1], geo, 50)
                p.append(f"The centre is now over land{(' in ' + nc[1]) if nc else ''}.")
            else:
                nc = _nearest_country(lat[-1], lon[-1], geo)
                if nc:
                    p.append(f"The centre is about {nc[0]:,.0f} km {_compass(_bearing(nc[2], nc[3], lat[-1], lon[-1]))} "
                             f"of the nearest land in {nc[1]}.")
        except Exception as e:
            print("geography sentence skipped:", e)
    out["story"] = [" ".join(p)]

    # --- paragraph 2: structure in GFS
    if diag:
        q = []
        sh = diag["shear_now"]
        word = "light" if sh < 5 else "moderate" if sh < 10 else "strong"
        q.append(f"In the latest GFS analysis the 850–500 hPa shear over {name} is "
                 + ("negligible." if sh < 1 else f"{word}, {sh:.0f} m/s from the {_compass(diag['shear_from'])}."))
        tl = diag["tilt_now"]
        tw = "nearly upright" if tl < 50 else "moderately tilted" if tl < 150 else "strongly tilted"
        s = (f"The vortex is {tw}, with the 850 and 500 hPa centres stacked within 10 km" if tl < 10 else
             f"The vortex is {tw}, with the 500 hPa centre {tl:.0f} km {_compass(diag['tilt_dir'])} of the 850 hPa centre")
        if diag.get("tilt_24") is not None:
            d = tl - diag["tilt_24"]
            s += (", and it has become more aligned over the past day" if d < -30 else
                  ", and the tilt has grown over the past day" if d > 30 else "")
        q.append(s + ".")
        if diag.get("rmw_24") is not None:
            d = diag["rmw_now"] - diag["rmw_24"]
            if d < -15 and not over_land_now(st, geo):
                q.append(f"The radius of maximum wind at 850 hPa has contracted from {diag['rmw_24']:.0f} to {diag['rmw_now']:.0f} km, "
                         "a sign of an organising inner core.")
            elif d > 15:
                q.append(f"The radius of maximum wind at 850 hPa has expanded from {diag['rmw_24']:.0f} to {diag['rmw_now']:.0f} km.")
        env = []
        if diag.get("shear_deep") is not None:
            dsh = diag["shear_deep"]
            env.append(f"850–200 hPa deep-layer shear is {'weak' if dsh < 5 else 'moderate' if dsh < 10 else 'strong'} ({dsh:.0f} m/s)")
        if diag.get("rh_mid") is not None:
            rh = diag["rh_mid"]
            env.append(f"the mid troposphere is {'moist' if rh >= 70 else 'moderately moist' if rh >= 55 else 'dry'} ({rh:.0f}% relative humidity at 500–700 hPa)")
        if diag.get("sst") is not None:
            sst = diag["sst"]
            env.append(f"the sea surface under the core is {sst:.1f} °C" + (", below the 26.5 °C usually needed to sustain a tropical cyclone" if sst < 26.5 else ""))
        if env:
            q.append(cap_first("; ".join(env)) + ".")
        fav = [x for x in [diag.get("shear_deep") is not None and diag["shear_deep"] < 10, diag.get("rh_mid") is not None and diag["rh_mid"] >= 70,
                           diag.get("sst") is not None and diag["sst"] >= 28, tl < 50]]
        if len([x for x in [diag.get("shear_deep"), diag.get("rh_mid"), diag.get("sst")] if x is not None]) == 3:
            q.append(f"{sum(fav)} of 4 conditions often linked to intensification are met (deep shear under 10 m/s, mid-level humidity of at least 70%, "
                     "sea surface at least 28 °C, an aligned vortex).")
        elif word == "light" and tl < 50 and st["vmax_kt"] < 137 and not over_land_now(st, geo):
            q.append("Low shear and an aligned vortex are both conditions that favour further strengthening.")
        elif word == "strong" or tl >= 150:
            q.append("Shear and tilt of this size usually limit intensification.")
        out["story"].append(" ".join(q))
        out["env"] = {k: round(float(val), 1) for k, val in diag.items() if val is not None}
    return out

def cap_first(x):
    return x[:1].upper() + x[1:] if x else x

def over_land_now(st, geo):
    if geo is None: return False
    try: return _on_land(st["track"].lat.values[-1], st["track"].lon.values[-1], geo)
    except Exception: return False
