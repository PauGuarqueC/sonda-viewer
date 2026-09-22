"""
Repassa tots els incendis ja processats i afegeix el perfil ERA5 a les
sondes que ja tinguin prou dies (i que encara no en tinguin cap). Es fa
servir des d'una Action SEPARADA de "Processa sondes pujades" -- mai
bloqueja ni alenteix una pujada nova, ja que el CDS pot trigar (des de
segons fins a bastants minuts en hores punta).

Variables d'entorn necessaries: DATA_ENCRYPT_KEY, CDSAPI_URL, CDSAPI_KEY.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_utils import decrypt_str, encrypt_str
from fetch_era5 import fetch_era5_profile

ERA5_MIN_AGE_DAYS = 6  # marge de seguretat per sobre del retard tipic d'ERA5T (~5 dies)
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "incendis"


def needs_era5(sonda_data):
    existing = sonda_data.get("era5_profile")
    if existing:
        # perfils obtinguts abans que fetch_era5_profile inclogués
        # pressure_mb/temp_c, abans que es desés era5_target_hour, o amb
        # l'hora mal truncada (cap avall sempre, en lloc d'arrodonida a la
        # mes propera) es tornen a demanar un cop -- launch_time_utc encara
        # es conserva a la sonda, aixi que es pot recalcular quina hauria
        # de ser l'hora correcta i comparar-la amb la que ja hi ha desada.
        complete = all("pressure_mb" in pt and "temp_c" in pt for pt in existing)
        stored_hour = sonda_data.get("era5_target_hour")
        if complete and stored_hour:
            rounded = rounded_launch_hour(sonda_data)
            expected_hour = f"{rounded.strftime('%Y-%m-%d')}T{rounded.hour:02d}" if rounded else None
            if expected_hour is not None and stored_hour == expected_hour:
                return False
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", sonda_data["sond_id"])
    if not m:
        return False
    launch_date = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - launch_date).days
    return age_days >= ERA5_MIN_AGE_DAYS and bool(sonda_data.get("launch"))


def rounded_launch_hour(sonda_data):
    """
    Arrodoneix la data/hora de llançament a l'hora ERA5 (horaria) mes
    propera -- p.ex. 13:20 UTC -> 13:00, 13:50 UTC -> 14:00 -- en lloc de
    truncar sempre cap avall. Fet amb datetime (no nomes substituint el
    camp "hora") perque un arrodoniment cap amunt a les 23:xx passi
    correctament al dia seguent a les 00:00.
    """
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", sonda_data["sond_id"])
    if not m:
        return None
    hh_mm = re.match(r"^(\d{2}):(\d{2})", sonda_data.get("launch_time_utc") or "")
    hh = int(hh_mm.group(1)) if hh_mm else 12
    mm = int(hh_mm.group(2)) if hh_mm else 0
    launch_dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), hh, mm, tzinfo=timezone.utc)
    rounded = launch_dt.replace(minute=0, second=0, microsecond=0)
    if mm >= 30:
        rounded += timedelta(hours=1)
    return rounded


def fetch_for(sonda_data):
    rounded = rounded_launch_hour(sonda_data)
    date_str = rounded.strftime("%Y-%m-%d")
    profile = fetch_era5_profile(
        sonda_data["launch"]["lat"], sonda_data["launch"]["lon"],
        date_str, rounded.hour
    )
    # mateix format "YYYY-MM-DDTHH" que el targetHour dels models en directe
    # (queryModelHost a index.html), perque el visor el pugui mostrar igual.
    target_hour = f"{date_str}T{rounded.hour:02d}"
    return profile, target_hour


def main():
    key = os.environ["DATA_ENCRYPT_KEY"]
    total_updated = 0

    if not DATA_DIR.exists():
        print("Cap incendi processat encara, res a fer.")
        return

    for path in sorted(DATA_DIR.glob("*.json.enc")):
        try:
            incendi_data = json.loads(decrypt_str(path.read_text(encoding="utf-8"), key))
        except ValueError as exc:
            print(f"[error] no s'ha pogut desxifrar {path.name}: {exc}")
            continue

        changed = False
        for sonda_data in incendi_data.get("sondes", []):
            if not needs_era5(sonda_data):
                continue
            try:
                profile, target_hour = fetch_for(sonda_data)
                sonda_data["era5_profile"] = profile
                sonda_data["era5_target_hour"] = target_hour
                print(f"[ok] ERA5 obtingut per {sonda_data['sond_id']} ({path.stem.replace('.json','')}, {target_hour}h UTC)")
                changed = True
                total_updated += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[avis] ERA5 no disponible per {sonda_data['sond_id']}: {exc}")

        if changed:
            path.write_text(encrypt_str(json.dumps(incendi_data, ensure_ascii=False), key), encoding="utf-8")

    print(f"Total sondes actualitzades amb ERA5: {total_updated}")


if __name__ == "__main__":
    main()
