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
        })
    return rows


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


def build_sonda_json(zip_path, incendi=None, tipus=None):
    zip_path = Path(zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        sounding_name = _open_member(zf, ".sounding.csv")
        flight_name = _open_member(zf, ".raw_flight_history.csv")
        kml_name = _open_member(zf, ".kml")

        if not sounding_name or not flight_name:
            raise ValueError("Falten sounding.csv o raw_flight_history.csv al zip")

        # sond_id ve del nom real del .sounding.csv dins el zip (el que genera
        # sempre el Windsond amb el mateix format), no del nom del ZIP en si
        # -- aixi no depen de com algu hagi anomenat/canviat el nom del fitxer
        # pujat.
        sond_id = Path(sounding_name).name
        for suf in (".sounding.csv",):
            if sond_id.lower().endswith(suf):
                sond_id = sond_id[: -len(suf)]
                break

        sounding_text = zf.read(sounding_name).decode("utf-8", errors="replace")
        flight_text = zf.read(flight_name).decode("utf-8", errors="replace")
        kml_text = zf.read(kml_name).decode("utf-8", errors="replace") if kml_name else None

    profile = parse_sounding_csv(sounding_text)
    flight_rows = parse_flight_history_csv(flight_text)
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

    output = {
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
    }
    return output


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path")
    ap.add_argument("--incendi", default=None)
    ap.add_argument("--tipus", choices=["ambient", "columna"], default=None)
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args()

    data = build_sonda_json(args.zip_path, incendi=args.incendi, tipus=args.tipus)

    out_path = args.output or (Path(args.zip_path).stem + ".sonda.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"OK -> {out_path}")
    print(f"  sond_id: {data['sond_id']}")
    print(f"  launch_time_utc (real, no del nom de fitxer): {data['launch_time_utc']}")
    print(f"  punts de perfil: {len(data['profile'])}")
    print(f"  punts de track 3D (kml): {len(data['flight_path_3d'])}")
    print(f"  pic AGL: {data['peak_agl_m']} m")
    with_rise = sum(1 for p in data["profile"] if p["rise_speed_ms"] is not None)
    print(f"  punts del perfil amb rise_speed interpolat: {with_rise}/{len(data['profile'])}")


if __name__ == "__main__":
    main()
