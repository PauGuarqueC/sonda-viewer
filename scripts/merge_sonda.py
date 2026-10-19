"""
merge_sonda.py

Fusiona les 3 fonts d'un llançament de sonda (Windsond) en un únic JSON
llest per alimentar el visor web:

  - <nom>.sounding.csv           -> perfil vertical net, corregit per radiacio
                                     (Height AGL, P, T, RH, vent) -> font base
  - <nom>.raw_flight_history.csv -> serie temporal amb GPS i velocitat
                                     d'ascens -> per treure launch_time real,
                                     rise_speed i lat/lon interpolats per alcada
  - <nom>.kml                    -> launch point, track 3D (lon,lat,alt MSL),
                                     pic (peak) i prediccio d'aterratge

Us:
    python merge_sonda.py <carpeta_o_zip_del_llancament> [--incendi NOM] [--tipus ambient|columna]

Produeix <sond_id>.sonda.json a la carpeta de sortida.
"""

import argparse
import csv
import io
import json
import math
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

KML_NS = {"k": "http://www.opengis.net/kml/2.1"}


def _open_member(zf, suffix):
    """Troba dins el zip el primer fitxer que acaba amb `suffix`."""
    for name in zf.namelist():
        if name.lower().endswith(suffix.lower()):
            return name
    return None


def _group_sonde_members(zf):
    """Agrupa els fitxers del zip per sonda, pel seu nom base compartit.

    Permet que un mateix ZIP contingui els fitxers de diverses sondes
    barrejats (p.ex. quan es puja tot de cop sense separar per carpetes):
    cada .sounding.csv marca una sonda, i es busca el .raw_flight_history.csv
    i el .kml que comparteixin exactament el mateix nom base.
    """
    names = zf.namelist()
    groups = []
    for name in names:
        if name.lower().endswith(".sounding.csv"):
            stem = Path(name).name
            stem = stem[: -len(".sounding.csv")]
            flight_name = next(
                (n for n in names if Path(n).name.lower() == (stem + ".raw_flight_history.csv").lower()),
                None,
            )
            kml_name = next(
                (n for n in names if Path(n).name.lower() == (stem + ".kml").lower()),
                None,
            )
            groups.append({"stem": stem, "sounding_name": name, "flight_name": flight_name, "kml_name": kml_name})

    # Sondes sense .sounding.csv (p.ex. nomes es va guardar el KML i el
    # raw_flight_history): el perfil es reconstrueix del raw (vegeu
    # profile_from_raw_flight). Nomes les que no tenen ja un grup amb sounding.
    with_sounding = {g["stem"].lower() for g in groups}
    suffix = ".raw_flight_history.csv"
    for name in names:
        if not name.lower().endswith(suffix):
            continue
        stem = Path(name).name[: -len(suffix)]
        if stem.lower() in with_sounding:
            continue
        kml_name = next(
            (n for n in names if Path(n).name.lower() == (stem + ".kml").lower()),
            None,
        )
        groups.append({"stem": stem, "sounding_name": None, "flight_name": name, "kml_name": kml_name})

    # Si el zip porta una sola sonda i un sol .kml amb un altre nom, el fem servir.
    kmls = [n for n in names if n.lower().endswith(".kml")]
    if len(groups) == 1 and groups[0]["kml_name"] is None and len(kmls) == 1:
        groups[0]["kml_name"] = kmls[0]
    return groups


def _guess_tipus(stem, default_tipus):
    """Si el nom de fitxer de la sonda inclou 'columna' (com ja fem al
    navegador en pujar), ho fem servir per sobre del tipus per defecte del
    formulari -- util quan una mateixa pujada barreja ambient i columna."""
    return "columna" if "column" in stem.lower() else default_tipus


def parse_sounding_csv(text):
    """Perfil vertical net (Height AGL, P, T, RH, vent). Ignora linies de comentari (#)."""
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    reader.fieldnames = [h.strip() for h in reader.fieldnames]
    profile = []
    for row in reader:
        row = {k.strip(): v.strip() for k, v in row.items()}
        try:
            profile.append({
                "height_agl_m": float(row["Height (m AGL)"]),
                "pressure_mb": float(row["Pressure (mb)"]),
                "temp_c": float(row["Temperature (C)"]),
                "rh_pct": float(row["Relative humidity (%)"]),
                "wind_speed_ms": float(row["Wind speed (m/s)"]),
                "wind_dir_deg": float(row["Wind direction (true deg)"]),
            })
        except (KeyError, ValueError):
            continue
    profile.sort(key=lambda r: r["height_agl_m"])
    return profile


def parse_flight_history_csv(text):
    """Serie temporal amb AGL, rise speed i GPS. Retorna llista ordenada per temps."""
    lines = [l for l in text.splitlines() if l.strip()]
    reader = csv.DictReader(lines)
    reader.fieldnames = [h.strip() for h in reader.fieldnames]
    rows = []
    for row in reader:
        row = {k.strip(): v.strip() for k, v in row.items()}

        def f(key):
            v = row.get(key, "")
            try:
                return float(v)
            except ValueError:
                return None

        rows.append({
            "utc_time": row.get("UTC time", "").strip(),
            "alt_msl_m": f("Altitude (m MSL)"),
            "alt_agl_m": f("Altitude (m AGL)"),
            "temp_c": f("Temperature (C)"),
            "rh_pct": f("Relative humidity (%)"),
            "lat": f("Latitude"),
            "lon": f("Longitude"),
            "rise_speed_ms": f("Rise speed (m/s)"),
            # usats nomes per reconstruir el perfil quan no hi ha .sounding.csv
            "pressure_pa": f("Pressure (Pascal)"),
            "speed_ms": f("Speed (m/s)"),
            "heading_deg": f("Heading (degrees)"),
        })
    return rows


def profile_from_raw_flight(flight_rows, step_m=20.0, half_window_m=10.0, max_gap_m=300.0):
    """Reconstrueix un perfil equivalent al del .sounding.csv NOMES del
    raw_flight_history: una fila cada `step_m` metres AGL (20, 40, ...) del
    tram d'ascens.

    Per cada nivell es fa la mitjana de les mostres dins de +-half_window_m;
    si no n'hi ha cap (la sonda puja rapid i a certes alcades el raw nomes
    registra una fila cada pocs segons, amb mes de 20 m entre files) s'interpola
    linealment entre la mostra de sota i la de dalt, sempre que no hi hagi mes
    de max_gap_m entre elles -- si el forat es mes gran, el nivell es descarta
    en lloc d'inventar-se'l.

    Validat contra .sounding.csv / .raw_flight_history reals: la temperatura en
    vol coincideix amb la del sounding (diferencia mitjana +-0.04 C, tambe per
    sota de 300 m), pressio -0.04 mb i HR +-0.1 %; el vent es una mica mes
    sorollos. El vent surt del rumb i la velocitat del GPS (direccio = rumb +
    180, es a dir, d'on bufa), tractat com a vector (u, v) perque la direccio
    no salti a 0/360."""
    ascent = ascent_leg(flight_rows)
    heights = [r["alt_agl_m"] for r in ascent if r["alt_agl_m"] is not None]
    if not heights:
        return []
    top = max(heights)

    def series(getter):
        pts = []
        for r in ascent:
            if r["alt_agl_m"] is None:
                continue
            v = getter(r)
            if v is not None:
                pts.append((r["alt_agl_m"], v))
        pts.sort(key=lambda t: t[0])
        return pts

    def value_at(pts, h):
        near = [v for a, v in pts if abs(a - h) <= half_window_m]
        if near:
            return sum(near) / len(near)
        below = [(a, v) for a, v in pts if a < h]
        above = [(a, v) for a, v in pts if a > h]
        if not below or not above:
            return None
        a0, v0 = below[-1]
        a1, v1 = above[0]
        if a1 - a0 > max_gap_m or a1 == a0:
            return None
        return v0 + (h - a0) / (a1 - a0) * (v1 - v0)

    def uv(r):
        if r["speed_ms"] is None or r["heading_deg"] is None:
            return None
        frm = math.radians(r["heading_deg"] + 180.0)  # direccio DES D'ON bufa
        return (-r["speed_ms"] * math.sin(frm), -r["speed_ms"] * math.cos(frm))

    s_p = series(lambda r: r["pressure_pa"])
    s_t = series(lambda r: r["temp_c"])
    s_rh = series(lambda r: r["rh_pct"])
    s_u = series(lambda r: (uv(r) or (None, None))[0])
    s_v = series(lambda r: (uv(r) or (None, None))[1])

    profile = []
    h = step_m
    while h <= top:
        p_pa, t, rh = value_at(s_p, h), value_at(s_t, h), value_at(s_rh, h)
        u, v = value_at(s_u, h), value_at(s_v, h)
        if None not in (p_pa, t, rh, u, v):
            profile.append({
                "height_agl_m": h,
                "pressure_mb": p_pa / 100.0,
                "temp_c": t,
                "rh_pct": rh,
                "wind_speed_ms": math.hypot(u, v),
                "wind_dir_deg": (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0,
            })
        h += step_m
    return profile


def find_launch_time(flight_rows):
    """Primera fila amb dades meteo reals (temp i RH no buides) -> hora de llancament real,
    en comptes de l'hora del nom de fitxer (que es quan es reconnecta amb el receptor)."""
    for row in flight_rows:
        if row["temp_c"] is not None and row["rh_pct"] is not None and row["utc_time"]:
            return row["utc_time"]
    return None


def ascent_leg(flight_rows):
    """Retalla nomes el tram d'ascens: des de l'inici fins a la primera vegada
    que s'assoleix l'alcada AGL maxima (abans que comenci a baixar)."""
    agls = [r["alt_agl_m"] for r in flight_rows]
    valid = [(i, a) for i, a in enumerate(agls) if a is not None]
    if not valid:
        return flight_rows
    peak_idx = max(valid, key=lambda t: t[1])[0]
    return flight_rows[:peak_idx + 1]


def interp_at_height(ascent_rows, target_agl, field):
    """Interpolacio lineal d'un camp (rise_speed_ms, lat, lon...) segons alt_agl_m."""
    pts = [(r["alt_agl_m"], r[field]) for r in ascent_rows
           if r["alt_agl_m"] is not None and r[field] is not None]
    if not pts:
        return None
    pts.sort(key=lambda p: p[0])
    if target_agl <= pts[0][0]:
        return pts[0][1]
    if target_agl >= pts[-1][0]:
        return pts[-1][1]
    for (h0, v0), (h1, v1) in zip(pts, pts[1:]):
        if h0 <= target_agl <= h1:
            if h1 == h0:
                return v0
            frac = (target_agl - h0) / (h1 - h0)
            return v0 + frac * (v1 - v0)
    return None


def virtual_potential_temp(temp_c, rh_pct, pressure_mb):
    """Temperatura potencial virtual (K) a partir de T(C), HR(%) i P(mb).
    theta = T_K * (1000/P)^0.286 ; theta_v = theta * (1 + 0.61*w)
    w = raó de mescla (kg/kg) a partir de la pressió de vapor saturant (Tetens)."""
    if temp_c is None or rh_pct is None or pressure_mb is None or pressure_mb <= 0:
        return None
    t_k = temp_c + 273.15
    theta = t_k * (1000.0 / pressure_mb) ** 0.286
    es = 6.112 * (2.718281828 ** (17.67 * temp_c / (temp_c + 243.5)))  # hPa
    e = es * (rh_pct / 100.0)
    denom = pressure_mb - e
    w = 0.622 * e / denom if denom > 0 else 0.0
    return theta * (1 + 0.61 * w)


def parse_kml(text):
    """Treu launch point, track 3D (com puja), pic i prediccio d'aterratge."""
    root = ET.fromstring(text)
    result = {"launch": None, "flight_path_3d": [], "peak": None, "landing_prediction": None}

    for pm in root.iter("{http://www.opengis.net/kml/2.1}Placemark"):
        name_el = pm.find("k:name", KML_NS)
        name = name_el.text if name_el is not None else ""

        point = pm.find(".//k:Point/k:coordinates", KML_NS)
        line = pm.find(".//k:LineString/k:coordinates", KML_NS)

        if point is not None:
            lon, lat, alt = [float(x) for x in point.text.strip().split(",")]
            entry = {"name": name, "lon": lon, "lat": lat, "alt_msl_m": alt}
            low = name.lower()
            if "launch" in low:
                result["launch"] = entry
            elif "peak" in low:
                result["peak"] = entry
            elif "landing prediction" in low and "original" not in low:
                result["landing_prediction"] = entry

        if line is not None:
            coords = []
            for triplet in line.text.strip().split():
                lon, lat, alt = [float(x) for x in triplet.split(",")]
                coords.append([lon, lat, alt])
            if "flight path" in name.lower():
                result["flight_path_3d"] = coords

    return result


def _build_one_sonda(sounding_text, flight_text, kml_text, sond_id, incendi, tipus):
    flight_rows = parse_flight_history_csv(flight_text)
    perfil_font = "sounding" if sounding_text is not None else "raw_flight_history"
    if sounding_text is not None:
        profile = parse_sounding_csv(sounding_text)
    else:
        profile = profile_from_raw_flight(flight_rows)
        if not profile:
            raise ValueError(f"No s'ha pogut reconstruir el perfil de {sond_id} del raw_flight_history")
    launch_time_utc = find_launch_time(flight_rows)
    ascent = ascent_leg(flight_rows)

    # Enriquir cada punt del perfil amb rise_speed i lat/lon interpolats per alcada
    for point in profile:
        h = point["height_agl_m"]
        point["rise_speed_ms"] = interp_at_height(ascent, h, "rise_speed_ms")
        point["alt_msl_m"] = interp_at_height(ascent, h, "alt_msl_m")
        point["lat"] = interp_at_height(ascent, h, "lat")
        point["lon"] = interp_at_height(ascent, h, "lon")
        point["theta_v_k"] = virtual_potential_temp(
            point["temp_c"], point["rh_pct"], point["pressure_mb"]
        )

    kml_info = parse_kml(kml_text) if kml_text else {
        "launch": None, "flight_path_3d": [], "peak": None, "landing_prediction": None
    }

    peak_agl = None
    valid_agls = [r["alt_agl_m"] for r in flight_rows if r["alt_agl_m"] is not None]
    if valid_agls:
        peak_agl = max(valid_agls)

    return {
        "sond_id": sond_id,
        "incendi": incendi,
        "tipus": tipus,  # "ambient" | "columna"
        "launch_time_utc": launch_time_utc,
        "launch": kml_info["launch"],
        "peak": kml_info["peak"] or ({"alt_msl_m": None} if peak_agl is None else None),
        "peak_agl_m": peak_agl,
        "landing_prediction": kml_info["landing_prediction"],
        "flight_path_3d": kml_info["flight_path_3d"],
        "profile": profile,
        # "sounding" = .sounding.csv original; "raw_flight_history" = perfil
        # reconstruit del raw perque no hi havia .sounding.csv (vent una mica
        # mes sorollos). Camp informatiu: el visor actual no el fa servir.
        "perfil_font": perfil_font,
    }


def build_sonda_json(zip_path, incendi=None, tipus=None):
    """Torna una LLISTA de sondes (normalment 1, pero pot haver-n'hi mes
    d'una si el ZIP porta els fitxers de diverses sondes barrejats sense
    separar-los -- es detecten agrupant-los pel seu nom base compartit)."""
    zip_path = Path(zip_path)

    parsed = []
    with zipfile.ZipFile(zip_path) as zf:
        groups = _group_sonde_members(zf)
        if not groups:
            raise ValueError("Cap .sounding.csv ni .raw_flight_history.csv trobat al zip")

        for g in groups:
            if not g["flight_name"]:
                raise ValueError(f"Falta el .raw_flight_history.csv de {g['stem']} al zip")
            sounding_text = (
                zf.read(g["sounding_name"]).decode("utf-8", errors="replace")
                if g["sounding_name"] else None
            )
            flight_text = zf.read(g["flight_name"]).decode("utf-8", errors="replace")
            kml_text = zf.read(g["kml_name"]).decode("utf-8", errors="replace") if g["kml_name"] else None
            sonda_tipus = _guess_tipus(g["stem"], tipus)
            parsed.append(_build_one_sonda(sounding_text, flight_text, kml_text, g["stem"], incendi, sonda_tipus))

    return parsed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path")
    ap.add_argument("--incendi", default=None)
    ap.add_argument("--tipus", choices=["ambient", "columna"], default=None)
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args()

    sonda_list = build_sonda_json(args.zip_path, incendi=args.incendi, tipus=args.tipus)

    out_path = args.output or (Path(args.zip_path).stem + ".sonda.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(sonda_list, f, ensure_ascii=False, indent=2)

    print(f"OK -> {out_path} ({len(sonda_list)} sonda{'es' if len(sonda_list) != 1 else ''} al zip)")
    for data in sonda_list:
        print(f"  sond_id: {data['sond_id']} (tipus: {data['tipus']})")
        print(f"    launch_time_utc (real, no del nom de fitxer): {data['launch_time_utc']}")
        print(f"    punts de perfil: {len(data['profile'])}")
        print(f"    punts de track 3D (kml): {len(data['flight_path_3d'])}")
        print(f"    pic AGL: {data['peak_agl_m']} m")
        with_rise = sum(1 for p in data["profile"] if p["rise_speed_ms"] is not None)
        print(f"    punts del perfil amb rise_speed interpolat: {with_rise}/{len(data['profile'])}")


if __name__ == "__main__":
    main()
