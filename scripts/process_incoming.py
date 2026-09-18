"""
process_incoming.py — s'executa dins la GitHub Action.

Recorre incoming/<incendi-slug>/<sond_id>.zip (cadascun amb un
<sond_id>.meta.json parell amb {"incendi": ..., "tipus": ...}, pujats pel
formulari web via el worker de Cloudflare) i, per als que encara no
existeixen, els processa amb merge_sonda.build_sonda_json i actualitza
data/incendis/<slug>.json.enc i data/incendis_index.json.enc.

Els fitxers .json.enc estan XIFRATS (AES, compatible amb CryptoJS al
navegador) amb la contrasenya DATA_ENCRYPT_KEY (una GitHub Secret) — mai es
desa cap versió en clar al repo. Idempotent: si es torna a executar sense
fitxers nous, no canvia res.
"""

import json
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from merge_sonda import build_sonda_json
from crypto_utils import encrypt_str, decrypt_str, decrypt_bytes
from fetch_era5 import fetch_era5_profile

ERA5_MIN_AGE_DAYS = 6  # marge de seguretat per sobre del retard tipic d'ERA5T (~5 dies)


def maybe_attach_era5(sonda_data):
    """Si la sonda ja te prou dies (ERA5 hauria d'estar disponible), demana
    el perfil al CDS i l'enganxa a sonda_data['era5_profile']. No fa fallar
    el proces si el CDS no respon be -- simplement es queda sense ERA5."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", sonda_data["sond_id"])
    if not m:
        return
    launch_date = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - launch_date).days
    if age_days < ERA5_MIN_AGE_DAYS:
        return
    if not sonda_data.get("launch"):
        return

    hh_match = re.match(r"^(\d{2}):", sonda_data.get("launch_time_utc") or "")
    hour = int(hh_match.group(1)) if hh_match else 12

    try:
        sonda_data["era5_profile"] = fetch_era5_profile(
            sonda_data["launch"]["lat"], sonda_data["launch"]["lon"],
            f"{m.group(1)}-{m.group(2)}-{m.group(3)}", hour
        )
        print(f"    [ok] ERA5 obtingut per {sonda_data['sond_id']}")
    except Exception as exc:  # noqa: BLE001
        print(f"    [avis] ERA5 no disponible per {sonda_data['sond_id']}: {exc}")

ROOT = Path(__file__).resolve().parent.parent
INCOMING = ROOT / "incoming"
DATA_DIR = ROOT / "data" / "incendis"
INDEX_PATH = ROOT / "data" / "incendis_index.json.enc"

DATA_ENCRYPT_KEY = os.environ.get("DATA_ENCRYPT_KEY")


def _require_key():
    if not DATA_ENCRYPT_KEY:
        print("[error] falta la variable d'entorn DATA_ENCRYPT_KEY", file=sys.stderr)
        sys.exit(1)


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "incendi"


def read_encrypted_json(path, default):
    if not path.exists():
        return default
    ciphertext = path.read_text(encoding="utf-8")
    try:
        plaintext = decrypt_str(ciphertext, DATA_ENCRYPT_KEY)
    except ValueError as exc:
        raise SystemExit(
            f"[error] no s'ha pogut desxifrar {path}: {exc}\n"
            f"Molt probablement DATA_ENCRYPT_KEY ha canviat des que es va "
            f"escriure aquest fitxer (o és el fitxer de mostra amb la clau "
            f"de demo). Esborra {path} del repo (i el seu index si escau) "
            f"i torna a executar — es crearà de nou amb la clau actual."
        )
    return json.loads(plaintext)


def write_encrypted_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    plaintext = json.dumps(obj, ensure_ascii=False)
    ciphertext = encrypt_str(plaintext, DATA_ENCRYPT_KEY)
    path.write_text(ciphertext, encoding="utf-8")


def already_processed(slug, sond_id):
    path = DATA_DIR / f"{slug}.json.enc"
    data = read_encrypted_json(path, None)
    if data is None:
        return False
    return any(s["sond_id"] == sond_id for s in data.get("sondes", []))


def update_incendi(slug, incendi_nom, sonda_data):
    path = DATA_DIR / f"{slug}.json.enc"
    data = read_encrypted_json(path, {"incendi": incendi_nom, "sondes": []})
    data["sondes"] = [s for s in data["sondes"] if s["sond_id"] != sonda_data["sond_id"]]
    data["sondes"].append(sonda_data)
    write_encrypted_json(path, data)
    return data


def _avg_launch_coords(incendi_data):
    lats = [s["launch"]["lat"] for s in incendi_data["sondes"] if s.get("launch")]
    lons = [s["launch"]["lon"] for s in incendi_data["sondes"] if s.get("launch")]
    if not lats:
        return None
    return sum(lats) / len(lats), sum(lons) / len(lons)


def update_index(slug, incendi_nom, incendi_data):
    index = read_encrypted_json(INDEX_PATH, [])
    index = [it for it in index if it["slug"] != slug]
    sondes = incendi_data["sondes"]
    anys = sorted({s["sond_id"][:4] for s in sondes if s["sond_id"][:4].isdigit()})
    entry = {
        "slug": slug,
        "nom": incendi_nom,
        "n_sondes": len(sondes),
        "actualitzat": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "any": anys[0] if anys else None,
        "te_ambient": any(s.get("tipus") == "ambient" for s in sondes),
    }
    coords = _avg_launch_coords(incendi_data)
    if coords:
        entry["lat"], entry["lon"] = coords
    index.append(entry)
    index.sort(key=lambda it: it["actualitzat"], reverse=True)
    write_encrypted_json(INDEX_PATH, index)


def backfill_index_locations():
    """Omple lat/lon, any i te_ambient a l'index per a incendis processats
    abans que guardessim aquestes dades (no cal cap sonda nova per activar-ho)."""
    index = read_encrypted_json(INDEX_PATH, [])
    changed = False
    for entry in index:
        needs_coords = entry.get("lat") is None
        needs_meta = "any" not in entry or "te_ambient" not in entry
        if not needs_coords and not needs_meta:
            continue
        incendi_path = DATA_DIR / f"{entry['slug']}.json.enc"
        incendi_data = read_encrypted_json(incendi_path, None)
        if not incendi_data:
            continue
        if needs_coords:
            coords = _avg_launch_coords(incendi_data)
            if coords:
                entry["lat"], entry["lon"] = coords
                changed = True
        if needs_meta:
            sondes = incendi_data["sondes"]
            anys = sorted({s["sond_id"][:4] for s in sondes if s["sond_id"][:4].isdigit()})
            entry["any"] = anys[0] if anys else None
            entry["te_ambient"] = any(s.get("tipus") == "ambient" for s in sondes)
            changed = True
    if changed:
        write_encrypted_json(INDEX_PATH, index)
        print("[ok] ubicacions/any/ambient retroactivament omplerts a l'index")


def main():
    _require_key()

    if not INCOMING.exists():
        print("Cap carpeta incoming/, res a fer.")
        return

    all_enc = sorted(INCOMING.glob("*/*.zip.enc"))
    print(f"[debug] fitxers .zip.enc trobats ({len(all_enc)}):")
    for p in all_enc:
        print(f"[debug]   {p}")

    processed = 0
    for zip_enc_path in all_enc:
        slug = zip_enc_path.parent.name
        batch_id = zip_enc_path.name[: -len(".zip.enc")]
        meta_enc_path = zip_enc_path.with_name(batch_id + ".meta.json.enc")

        if not meta_enc_path.exists():
            print(f"[avis] {zip_enc_path} no te fitxer .meta.json.enc parell, l'ignoro (buscava {meta_enc_path})")
            continue

        try:
            zip_bytes = decrypt_bytes(
                zip_enc_path.read_text(encoding="utf-8"), DATA_ENCRYPT_KEY
            )
            meta = json.loads(
                decrypt_str(meta_enc_path.read_text(encoding="utf-8"), DATA_ENCRYPT_KEY)
            )
        except ValueError as exc:
            print(f"[error] no s'ha pogut desxifrar {zip_enc_path}: {exc}")
            continue

        incendi_nom = meta.get("incendi", slug)
        tipus = meta.get("tipus")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_zip_path = Path(tmp_dir) / f"{batch_id}.zip"
            tmp_zip_path.write_bytes(zip_bytes)

            try:
                # Una pujada (un .zip) pot portar mes d'una sonda a dins si
                # no s'han separat en fitxers diferents -- build_sonda_json
                # les detecta totes soles i les torna com una llista.
                sonda_list = build_sonda_json(tmp_zip_path, incendi=incendi_nom, tipus=tipus)
            except Exception as exc:  # noqa: BLE001
                print(f"[error] no s'ha pogut processar {zip_enc_path}: {exc}")
                continue

        incendi_data = None
        for sonda_data in sonda_list:
            sond_id = sonda_data["sond_id"]
            if already_processed(slug, sond_id):
                print(f"[debug] {sond_id} ja processat anteriorment, l'ignoro")
                continue
            maybe_attach_era5(sonda_data)
            incendi_data = update_incendi(slug, incendi_nom, sonda_data)
            processed += 1
            print(f"[ok] {incendi_nom} / {sond_id} ({sonda_data['tipus']}) processat i xifrat")

        if incendi_data is not None:
            update_index(slug, incendi_nom, incendi_data)

    backfill_index_locations()

    print(f"Total processades: {processed}")


if __name__ == "__main__":
    main()
