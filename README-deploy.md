# Desplegament — visor de sondes (tot a GitHub, sense labfire)

Aquesta versió no necessita cap servidor propi. Tot passa a GitHub:

```
navegador (upload.html) --API GitHub--> incoming/<incendi>/<sonda>.zip + .meta.json
                                              |
                                              v
                                   GitHub Actions (push a incoming/**)
                                              |
                                     scripts/process_incoming.py
                                              |
                                              v
                                data/incendis/<incendi>.json  (commit + push)
                                              |
                                              v
                                  GitHub Pages (index.html / incendi.html)
```

## 1. Crea el repo

```bash
gh repo create PauGuarqueC/sonda-viewer --public
git clone https://github.com/PauGuarqueC/sonda-viewer.git
cp -r sonda-viewer-package/* sonda-viewer/      # contingut d'aquest lliurament
cd sonda-viewer
git add . && git commit -m "Primera versió: visor + upload + Action" && git push
```

Activa **Settings → Pages → Deploy from branch → main / (root)**.

## 2. Ajusta `upload.html`

Al principi del `<script>` d'`upload.html` hi ha:

```js
const OWNER = "PauGuarqueC";
const REPO = "sonda-viewer";
const BRANCH = "main";
const WORKER_URL = "https://sonda-upload-proxy.YOUR-SUBDOMAIN.workers.dev";
```

Deixa `OWNER`/`REPO`/`BRANCH` com estan si fas servir aquests noms, i
`WORKER_URL` l'acabaràs d'omplir al pas següent.

## 3. Desplega el proxy (Cloudflare Worker) — evita exposar el token

**Important**: `upload.html` NO parla directament amb l'API de GitHub amb
un token — un token amb permís d'escriptura posat al JavaScript d'una
pàgina pública es podria llegir obrint les eines de desenvolupador del
navegador. En comptes d'això, hi ha un petit proxy gratuït (Cloudflare
Worker) que guarda el token com a secret al servidor; el navegador només
coneix una contrasenya compartida, molt menys sensible.

Tot el pas a pas (5 minuts, sense costos) és a `worker/README-worker.md`
d'aquest mateix lliurament. Resum:

```bash
cd worker
wrangler deploy
wrangler secret put GITHUB_TOKEN       # el token de GitHub, nomes aqui
wrangler secret put UPLOAD_PASSWORD    # contrasenya per a qui pugui pujar
```

## 3b. Xifrat de les dades (perquè no siguin llegibles obrint el repo)

Els fitxers `data/incendis/*.json.enc` es xifren automàticament dins la
GitHub Action, amb la contrasenya `DATA_ENCRYPT_KEY`. Sense aquesta
contrasenya, obrir el fitxer directament a GitHub només mostra text
il·legible — la pàgina el desxifra al navegador un cop la persona que la
visita introdueix la contrasenya.

Configura-la com a secret del **repositori** (no del worker, aquesta és
diferent):

**Settings → Secrets and variables → Actions → New repository secret**
- Nom: `DATA_ENCRYPT_KEY`
- Valor: una contrasenya llarga i única (no reutilitzis `UPLOAD_PASSWORD`)

Aquesta mateixa contrasenya és la que hauran d'introduir totes les persones
que vulguin **veure** el visor (`index.html`/`incendi.html` la demanen la
primera vegada i la guarden al seu navegador). És a dir, hi ha ara dues
contrasenyes amb rols diferents:
- `UPLOAD_PASSWORD` (al worker) → qui pot **pujar** sondes noves
- `DATA_ENCRYPT_KEY` (secret del repo) → qui pot **veure** les dades

Poden ser la mateixa persona o no, segons qui vulgueu que hi tingui accés.

Un cop desplegat, torna a `upload.html` i posa la URL que t'ha donat
`wrangler deploy` a `WORKER_URL`.

## 4. Prova el cicle complet

1. Obre `https://<usuari>.github.io/sonda-viewer/upload.html`
2. Enganxa el token, puja un ZIP, posa l'incendi i el tipus
3. Ves a `github.com/<usuari>/sonda-viewer/actions` — hauries de veure el
   workflow "Processa sondes pujades" corrent (triga uns 20-40 segons)
4. Quan acabi, `data/incendis/<slug>.json` tindrà un commit nou
5. Obre `index.html` — l'incendi hi hauria d'aparèixer (pot trigar un minut
   més perquè GitHub Pages redesplegui)

## Limitacions a tenir en compte

- **Repo públic** (per tenir GitHub Pages gratis sense pla de pagament):
  el codi i l'estructura són visibles, però el **contingut** de les dades
  de sondes queda xifrat (`.json.enc`) — qui obri el fitxer directament a
  GitHub només veu text il·legible sense la contrasenya `DATA_ENCRYPT_KEY`.
- **És una contrasenya compartida, no un compte per persona**: qui la
  conegui pot desxifrar tot. És una barrera real (a diferència del simple
  gate de contrasenya que vau veure a l'exemple de `javimiroo`, que només
  amagava la interfície, no les dades), però no aïlla l'accés persona a
  persona — si cal això, faria falta un sistema d'autenticació més complex.
- **Contrasenya compartida al worker** (`UPLOAD_PASSWORD`): tothom que
  pugi sondes fa servir la mateixa. En el pitjor cas, algú podria disparar
  pujades no desitjades, però mai obtenir el token real de GitHub.
- **L'autocompletar d'incendis coneguts al formulari de pujada ja no
  funciona** (abans llegia l'índex en clar; ara està xifrat i el
  formulari no en té la contrasenya). Cal escriure el nom de l'incendi
  igual cada vegada (mateixos accents/majúscules) perquè s'agrupi bé.
- **Mida de fitxer**: l'API de GitHub accepta fins a 100 MB per fitxer;
  els ZIPs de sonda (desenes-centenars de KB) hi són molt per sota.
- **Cap admin de labfire necessari** — aquest és l'avantatge principal.

## Fitxers d'aquest lliurament

```
index.html                        llista d'incendis (demana contrasenya de visualització)
incendi.html                      visor de superposició (fetch + desxifrat de data/)
upload.html                       formulari (client-side, parla amb el worker)
scripts/merge_sonda.py            fusió sounding+raw_flight_history+kml -> JSON
scripts/crypto_utils.py           xifrat/desxifrat AES compatible amb CryptoJS
scripts/process_incoming.py       driver que corre dins la GitHub Action
.github/workflows/process-sonda.yml
data/incendis/alfarras.json.enc   dades de mostra XIFRADES (clau demo:
                                   DEMO-Alfarras-2026 — NOMÉS per provar,
                                   canvia-la de seguida amb dades reals)
data/incendis_index.json.enc
incoming/.gitkeep                 carpeta on cauen els ZIPs pujats (buida)

worker/                           NO va dins el repo del visor — es
                                   desplega a part, a Cloudflare (veure
                                   worker/README-worker.md)
```
