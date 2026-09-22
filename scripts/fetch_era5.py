"""
Descarrega un perfil vertical ERA5 (nivells de pressió) del CDS per a un
punt i hora concrets, i el processa al mateix format que ja fem servir per
als altres "perfils de model" (Open-Meteo).

Necessita cdsapi configurat via variables d'entorn CDSAPI_URL i CDSAPI_KEY
(el token personal del CDS -- veure https://cds.climate.copernicus.eu/profile).
Cal haver acceptat, un cop, els termes del dataset "reanalysis-era5-pressure-levels"
a la pàgina web del CDS.
"""
import math
import tempfile
from pathlib import Path

ERA5_LEVELS = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100]
G0 = 9.80665  # gravetat estandard, per convertir geopotencial -> alcada geopotencial


def _theta_v(temp_c, rh_pct, pressure_mb):
    if temp_c is None or rh_pct is None or pressure_mb is None or pressure_mb <= 0:
        return None
    t_k = temp_c + 273.15
    theta = t_k * (1000.0 / pressure_mb) ** 0.286
    es = 6.112 * math.exp((17.67 * temp_c) / (temp_c + 243.5))
    e = es * (rh_pct / 100.0)
    denom = pressure_mb - e
    w = 0.622 * e / denom if denom > 0 else 0.0
    return theta * (1 + 0.61 * w)


def _wind_dir_from_uv(u, v):
    # Conveni meteorologic: direccio d'ON VE el vent, no cap on va
    return (180.0 + math.degrees(math.atan2(u, v))) % 360.0


def fetch_era5_profile(lat, lon, date_str, hour):
    """
    date_str: "YYYY-MM-DD". hour: enter 0-23 (ERA5 es horari, s'agafa l'hora
    sencera mes propera al llançament).

    Torna una llista de punts {alt_msl_m, pressure_mb, temp_c, theta_v_k,
    rh_pct, wind_speed_ms, wind_dir_deg}, ordenada per alcada creixent.
    pressure_mb i temp_c calen perque el visor dibuixi aquest perfil a
    l'Skew-T (les corbes de T/Td i les barbes de vent s'hi indexen per
    pressio, no per alcada); sense aquests dos camps el perfil es mostra
    igualment als 4 grafics de dalt (nomes fan servir theta_v_k/rh_pct/
    alt_msl_m) pero queda buit a l'Skew-T.
    CDS no respon be (llicencia no acceptada, token invalid, etc.) -- es
    responsabilitat de qui ho crida decidir si aixo ha de fer fallar tot el
    proces o nomes ballar-se aquest perfil concret.
    """
    import cdsapi
    import xarray as xr

    year, month, day = date_str.split("-")
    time_str = f"{hour:02d}:00"

    client = cdsapi.Client()

    request = {
        "product_type": ["reanalysis"],
        "variable": [
            "temperature",
            "relative_humidity",
            "geopotential",
            "u_component_of_wind",
            "v_component_of_wind",
        ],
        "pressure_level": [str(p) for p in ERA5_LEVELS],
        "year": [year],
        "month": [month],
        "day": [day],
        "time": [time_str],
        # petita caixa al voltant del punt (N, W, S, E) -- nomes cal el punt
        # mes proper, pero l'API de CDS no permet demanar un unic punt exacte
        "area": [lat + 0.3, lon - 0.3, lat - 0.3, lon + 0.3],
        "data_format": "netcdf",
    }

    with tempfile.TemporaryDirectory() as tmp:
        target = str(Path(tmp) / "era5.nc")
        client.retrieve("reanalysis-era5-pressure-levels", request, target)

        ds = xr.open_dataset(target)
        point = ds.sel(latitude=lat, longitude=lon, method="nearest")

        profile = []
        for p in ERA5_LEVELS:
            lvl = point.sel(pressure_level=p)
            t_k = float(lvl["t"].values.squeeze())
            rh = float(lvl["r"].values.squeeze())
            z = float(lvl["z"].values.squeeze())
            u = float(lvl["u"].values.squeeze())
            v = float(lvl["v"].values.squeeze())

            temp_c = t_k - 273.15
            alt_msl_m = z / G0
            speed = math.sqrt(u * u + v * v)

            profile.append({
                "alt_msl_m": alt_msl_m,
                "pressure_mb": float(p),
                "temp_c": temp_c,
                "theta_v_k": _theta_v(temp_c, rh, p),
                "rh_pct": rh,
                "wind_speed_ms": speed,
                "wind_dir_deg": _wind_dir_from_uv(u, v),
            })

        ds.close()

    profile.sort(key=lambda pt: pt["alt_msl_m"])
    return profile
