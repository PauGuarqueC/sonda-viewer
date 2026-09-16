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
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from merge_sonda import build_sonda_json
from crypto_utils import encrypt_str, decrypt_str

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
    entry = {
        "slug": slug,
        "nom": incendi_nom,
        "n_sondes": len(incendi_data["sondes"]),
        "actualitzat": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    coords = _avg_launch_coords(incendi_data)
    if coords:
        entry["lat"], entry["lon"] = coords
    index.append(entry)
    index.sort(key=lambda it: it["actualitzat"], reverse=True)
    write_encrypted_json(INDEX_PATH, index)

def backfill_index_locations():
    """Omple lat/lon a l'index per a incendis processats abans que
    guardessim la ubicacio (no cal cap sonda nova per activar-ho)."""
    index = read_encrypted_json(INDEX_PATH, [])
    changed = False
    for entry in index:
        if entry.get("lat") is not None:
            continue
        incendi_path = DATA_DIR / f"{entry['slug']}.json.enc"
        incendi_data = read_encrypted_json(incendi_path, None)
        if not incendi_data:
            continue
        coords = _avg_launch_coords(incendi_data)
        if coords:
            entry["lat"], entry["lon"] = coords
            changed = True
    if changed:
        write_encrypted_json(INDEX_PATH, index)
        print("[ok] ubicacions retroactivament omplertes a l'index")

def main():
    _require_key()

    if not INCOMING.exists():
        print("Cap carpeta incoming/, res a fer.")
        return

    processed = 0
    for zip_path in sorted(INCOMING.glob("*/*.zip")):
        slug = zip_path.parent.name
        sond_id = zip_path.stem
        meta_path = zip_path.with_name(sond_id + ".meta.json")

        if not meta_path.exists():
            print(f"[avis] {zip_path} no te fitxer .meta.json parell, l'ignoro")
            continue

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        incendi_nom = meta.get("incendi", slug)
        tipus = meta.get("tipus")

        if already_processed(slug, sond_id):
            continue

        try:
            sonda_data = build_sonda_json(zip_path, incendi=incendi_nom, tipus=tipus)
        except Exception as exc:  # noqa: BLE001
            print(f"[error] no s'ha pogut processar {zip_path}: {exc}")
            continue

        incendi_data = update_incendi(slug, incendi_nom, sonda_data)
        update_index(slug, incendi_nom, incendi_data)
        processed += 1
        print(f"[ok] {incendi_nom} / {sond_id} ({tipus}) processat i xifrat")

    backfill_index_locations()
    print(f"Total processades: {processed}")


if __name__ == "__main__":
    main()
