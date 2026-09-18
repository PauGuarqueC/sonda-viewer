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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_utils import decrypt_str, encrypt_str
from fetch_era5 import fetch_era5_profile

ERA5_MIN_AGE_DAYS = 6  # marge de seguretat per sobre del retard tipic d'ERA5T (~5 dies)
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "incendis"


def needs_era5(sonda_data):
    if sonda_data.get("era5_profile"):
        return False
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", sonda_data["sond_id"])
    if not m:
        return False
    launch_date = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - launch_date).days
    return age_days >= ERA5_MIN_AGE_DAYS and bool(sonda_data.get("launch"))


def fetch_for(sonda_data):
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", sonda_data["sond_id"])
    hh_match = re.match(r"^(\d{2}):", sonda_data.get("launch_time_utc") or "")
    hour = int(hh_match.group(1)) if hh_match else 12
    return fetch_era5_profile(
        sonda_data["launch"]["lat"], sonda_data["launch"]["lon"],
        f"{m.group(1)}-{m.group(2)}-{m.group(3)}", hour
    )


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
                sonda_data["era5_profile"] = fetch_for(sonda_data)
                print(f"[ok] ERA5 obtingut per {sonda_data['sond_id']} ({path.stem.replace('.json','')})")
                changed = True
                total_updated += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[avis] ERA5 no disponible per {sonda_data['sond_id']}: {exc}")

        if changed:
            path.write_text(encrypt_str(json.dumps(incendi_data, ensure_ascii=False), key), encoding="utf-8")

    print(f"Total sondes actualitzades amb ERA5: {total_updated}")


if __name__ == "__main__":
    main()
