#!/usr/bin/env python3
"""
Unipap's - Dashboard TV
=======================

Reproduit le dashboard "commandes a traiter" en grand format pour une TV :
  - Nombre de commandes a traiter + plateaux + temps de traitement estime
  - Repartition par transporteur (tags Shopify)
  - Precommandes en cours + plateaux + temps de traitement estime

Regles :
  - "Commandes a traiter"  = commandes ouvertes, non expediees ou
    partiellement expediees (fulfillment_status: unshipped/partial),
    SANS le tag "Precommande"
  - "Precommandes en cours" = commandes ouvertes avec le tag "Precommande"
  - "Plateaux"  = nombre de commandes / 30
  - "Temps de traitement" = plateaux x MINUTES_PAR_PLATEAU (defaut 45 min,
    ajustable ci-dessous ou via variable d'environnement)
  - Transporteur = tag pose sur la commande (Chronopost, Colissimo, ...)

La page se rafraichit toute seule toutes les 15 minutes (configurable).

------------------------------------------------------------------
INSTALLATION (sur le mini-PC branche a la TV)
------------------------------------------------------------------
1) python3 --version   (deja installe sur la plupart des Mac/Linux/Windows)
2) pip3 install requests
3) Appli Shopify (Dev Dashboard) deja creee et installee sur la boutique,
   avec le scope "read_orders". Recuperer Client ID + Secret.
4) Variables d'environnement :
       export SHOPIFY_STORE="noeudspapillon.myshopify.com"
       export SHOPIFY_CLIENT_ID="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
       export SHOPIFY_CLIENT_SECRET="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
       export REFRESH_MINUTES=15          # optionnel
       export PORT=8765                   # optionnel
       export MINUTES_PER_PLATEAU=45      # optionnel
5) python3 unipaps_tv_dashboard.py
6) Navigateur en plein ecran sur http://localhost:8765
------------------------------------------------------------------
"""

import os
import time
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------
SHOP = os.environ.get("SHOPIFY_STORE", "noeudspapillon.myshopify.com")
CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID", "REMPLACE_MOI_client_id")
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "REMPLACE_MOI_client_secret")
REFRESH_MINUTES = float(os.environ.get("REFRESH_MINUTES", "15"))
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", str(int(REFRESH_MINUTES * 60))))
PORT = int(os.environ.get("PORT", "8765"))
API_VERSION = "2024-10"

COMMANDES_PAR_PLATEAU = 30
MINUTES_PAR_PLATEAU = float(os.environ.get("MINUTES_PER_PLATEAU", "45"))
SUCCESS_MESSAGE = os.environ.get("SUCCESS_MESSAGE", "RAS, bravo l'équipe !")

# Filtre de base : commandes ouvertes, non expediees ou partiellement
# expediees.
BASE_FILTER = 'fulfillment_status:"unshipped,partial" status:"open"'

# Transporteurs a afficher : (emoji, libelle, tag Shopify)
CARRIERS = [
    ("🚀", "Chronopost", "Chronopost"),
    ("📮", "Colissimo", "Colissimo"),
    ("🏬", "Mondial Relay", "Mondial Relay"),
    ("📬", "Lettre Suivie", "Lettre Suivie"),
    ("✉️", "Lettre Non Suivie", "Lettre Non Suivie"),
    ("🌍", "Lettre Suivie Internationale", "Lettre Suivie Internationale"),
    ("🌐", "Lettre Non Suivie Internationale", "Lettre Non Suivie Internationale"),
]

# --------------------------------------------------------------------
# Prevision "commandes a traiter d'ici 15h"
# --------------------------------------------------------------------
# Principe : on regarde combien de commandes sont deja arrivees
# AUJOURD'HUI (nombre reel, interroge en direct sur Shopify), on compare
# cette valeur a la part du volume qu'on recoit habituellement a cette
# heure-ci (courbe horaire moyenne calculee sur l'historique Shopify
# Analytics 16/03/2024 - 02/09/2026), puis on extrapole jusqu'a 15h avec
# une simple regle de trois :
#
#   total_prevu_aujourd_hui = commandes_du_jour_reel / %_recu_a_cette_heure
#                              x %_recu_a_15h
#   commandes_attendues_en_plus = total_prevu_aujourd_hui - commandes_du_jour_reel
#   prevision_backlog = commandes_a_traiter_actuelles + commandes_attendues_en_plus
#
# Exemple : 28 commandes recues a 13h13. La courbe horaire dit qu'on a
# habituellement recu ~30,7% du volume du jour a 13h13, et ~41,7% a 15h.
# -> total_prevu = 28 / 30,7% x 41,7% = 38 -> +10 commandes attendues.
TIMEZONE = os.environ.get("TZ_NAME", "Europe/Paris")
PREDICTION_HOUR = int(os.environ.get("PREDICTION_HOUR", "15"))

# Nombre de commandes par heure (index 0 = 00h-01h, ... 23 = 23h-00h),
# agrege sur toute la periode. Sert uniquement a estimer la FORME de la
# courbe (part du volume recue a telle heure), pas le volume absolu.
HOURLY_ORDERS = [
    568, 204, 80, 47, 38, 59, 219, 598, 1162, 1844, 2070, 2130, 1989,
    2234, 2371, 2126, 2100, 2268, 2442, 2616, 2749, 3222, 2817, 1527,
]
_HOURLY_TOTAL = sum(HOURLY_ORDERS)


def _cumulative_pct(hour, minute):
    """% cumule (0-100) du volume moyen d'une journee, recu entre 00h00
    et l'heure:minute donnee."""
    hour = max(0, min(24, hour))
    done = sum(HOURLY_ORDERS[:hour])
    if hour < 24:
        done += HOURLY_ORDERS[hour] * (minute / 60)
    return 100 * done / _HOURLY_TOTAL


def compute_prediction(a_traiter, commandes_du_jour):
    """Renvoie (prediction_totale, commandes_attendues_en_plus) ou None
    si l'heure de reference est deja depassee."""
    now = datetime.now(ZoneInfo(TIMEZONE))
    if now.hour >= PREDICTION_HOUR:
        return None

    pct_now = _cumulative_pct(now.hour, now.minute)
    pct_target = _cumulative_pct(PREDICTION_HOUR, 0)

    if pct_now <= 0 or commandes_du_jour <= 0:
        # Pas assez de donnees ce matin pour extrapoler : rien a ajouter.
        return a_traiter, 0

    total_prevu_aujourd_hui = commandes_du_jour * pct_target / pct_now
    expected_new = max(0, total_prevu_aujourd_hui - commandes_du_jour)
    return a_traiter + round(expected_new), round(expected_new)


GRAPHQL_QUERY = """
query($query: String!, $cursor: String) {
  orders(first: 250, after: $cursor, query: $query) {
    edges {
      cursor
      node { id }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# --------------------------------------------------------------------
# Etat partage
# --------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache = {
    "a_traiter": 0,
    "precommandes": 0,
    "carriers": {},
    "commandes_du_jour": 0,
    "updated_at": None,
    "error": None,
    "frozen_a_traiter": None,
    "frozen_date": None,
    "frozen_carriers": None,
}


def get_access_token():
    """Recupere un jeton d'acces Admin API via le client credentials grant."""
    url = f"https://{SHOP}/admin/oauth/access_token"
    resp = requests.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def count_orders(token, search_query):
    url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }
    total = 0
    cursor = None
    while True:
        payload = {
            "query": GRAPHQL_QUERY,
            "variables": {"query": search_query, "cursor": cursor},
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(str(data["errors"]))
        block = data["data"]["orders"]
        total += len(block["edges"])
        if block["pageInfo"]["hasNextPage"]:
            cursor = block["pageInfo"]["endCursor"]
        else:
            break
    return total


def refresh_cache():
    try:
        token = get_access_token()

        a_traiter = count_orders(token, f'{BASE_FILTER} tag_not:"Précommande"')
        precommandes = count_orders(token, 'status:"open" tag:"Précommande"')

        carriers = {}
        for emoji, label, tag in CARRIERS:
            q = f'{BASE_FILTER} tag_not:"Précommande" tag:"{tag}"'
            carriers[label] = {"emoji": emoji, "count": count_orders(token, q)}

        now = datetime.now(ZoneInfo(TIMEZONE))
        today = now.strftime("%Y-%m-%d")
        commandes_du_jour = count_orders(token, f'created_at:>=\'{today}\'')

        with _cache_lock:
            # Le compteur "Commandes a traiter" se fige a sa valeur de
            # PREDICTION_HOUR (15h par defaut) jusqu'au lendemain : passe
            # cette heure, on ne le recalcule plus, on garde la 1ere
            # valeur capturee ce jour-la.
            # Meme principe pour la repartition par transporteur : figee
            # a 15h jusqu'au lendemain. Les precommandes, elles, restent
            # toujours en direct (pas de gel).
            if now.hour >= PREDICTION_HOUR:
                if _cache["frozen_date"] != today:
                    _cache["frozen_a_traiter"] = a_traiter
                    _cache["frozen_carriers"] = carriers
                    _cache["frozen_date"] = today
                a_traiter_affiche = _cache["frozen_a_traiter"]
                carriers_affiches = _cache["frozen_carriers"]
            else:
                _cache["frozen_date"] = None
                a_traiter_affiche = a_traiter
                carriers_affiches = carriers

            _cache["a_traiter"] = a_traiter_affiche
            _cache["precommandes"] = precommandes
            _cache["carriers"] = carriers_affiches
            _cache["commandes_du_jour"] = commandes_du_jour
            _cache["updated_at"] = time.strftime("%d/%m/%Y %H:%M:%S")
            _cache["error"] = None
    except Exception as exc:  # noqa: BLE001
        with _cache_lock:
            _cache["error"] = str(exc)
            _cache["updated_at"] = time.strftime("%d/%m/%Y %H:%M:%S")


def background_refresher():
    while True:
        refresh_cache()
        time.sleep(max(5, REFRESH_SECONDS))


# --------------------------------------------------------------------
# Aides d'affichage
# --------------------------------------------------------------------
def format_minutes(total_minutes):
    total_minutes = round(total_minutes)
    h, m = divmod(total_minutes, 60)
    if h:
        return f"{h}h {m:02d}min" if m else f"{h}h"
    return f"{m}min"


def format_plateaux(count):
    return f"{count / COMMANDES_PAR_PLATEAU:.1f}".replace(".", ",")


# --------------------------------------------------------------------
# Rendu HTML
# --------------------------------------------------------------------
def render_html():
    with _cache_lock:
        a_traiter = _cache["a_traiter"]
        precommandes = _cache["precommandes"]
        carriers = dict(_cache["carriers"])
        commandes_du_jour = _cache["commandes_du_jour"]
        updated_at = _cache["updated_at"] or "..."
        error = _cache["error"]

    plateaux_a_traiter = a_traiter / COMMANDES_PAR_PLATEAU
    temps_a_traiter = plateaux_a_traiter * MINUTES_PAR_PLATEAU
    plateaux_precommandes = precommandes / COMMANDES_PAR_PLATEAU
    temps_precommandes = plateaux_precommandes * MINUTES_PAR_PLATEAU

    if a_traiter == 0:
        a_traiter_card = f"""
    <div class="card">
      <div class="icon-box icon-green">✅</div>
      <div>
        <div class="stat-label">Commandes à traiter</div>
        <div class="stat-value green" style="font-size:26px;">{SUCCESS_MESSAGE}</div>
      </div>
    </div>"""
    else:
        a_traiter_card = f"""
    <div class="card">
      <div class="icon-box icon-orange">📦</div>
      <div>
        <div class="stat-label">Commandes à traiter</div>
        <div class="stat-row">
          <div class="stat-value orange">{a_traiter}</div>
          <div class="divider"></div>
          <div>
            <div class="stat-sub">🛒 {format_plateaux(a_traiter)}</div>
            <div class="stat-sub-label">Plateaux</div>
          </div>
        </div>
      </div>
    </div>"""

    prediction = compute_prediction(a_traiter, commandes_du_jour)
    if prediction:
        pred_total, pred_new = prediction
        prediction_card = f"""
    <div class="card">
      <div class="icon-box icon-blue">🔮</div>
      <div>
        <div class="stat-label">Prévision à {PREDICTION_HOUR}h</div>
        <div class="stat-row">
          <div class="stat-value blue">{pred_total}</div>
          <div class="divider"></div>
          <div>
            <div class="stat-sub blue">🛒 {format_plateaux(pred_total)}</div>
            <div class="stat-sub-label">Plateaux</div>
          </div>
        </div>
      </div>
    </div>"""
    else:
        prediction_card = f"""
    <div class="card">
      <div class="icon-box icon-blue">🔮</div>
      <div>
        <div class="stat-label">Prévision à {PREDICTION_HOUR}h</div>
        <div class="stat-value blue" style="font-size:20px;">Heure dépassée</div>
      </div>
    </div>"""

    max_count = max([c["count"] for c in carriers.values()] + [1])
    carrier_rows = ""
    for emoji, label, tag in CARRIERS:
        info = carriers.get(label, {"emoji": emoji, "count": 0})
        count = info["count"]
        pct = int(100 * count / max_count) if max_count else 0
        carrier_rows += f"""
        <div class="carrier-row">
          <div class="carrier-label"><span class="carrier-emoji">{emoji}</span>{label}</div>
          <div class="carrier-bar-track"><div class="carrier-bar-fill" style="width:{pct}%"></div></div>
          <div class="carrier-count">{count}</div>
        </div>"""

    error_banner = f'<div class="error">Erreur de mise a jour : {error}</div>' if error else ""

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Unipap's - Dashboard</title>
<style>
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0; padding: 0; min-height: 100%;
    background: #f2f4f7; color: #1a1f29;
    font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
  }}
  .wrap {{ max-width: 1500px; margin: 0 auto; padding: 18px 36px; min-height: 100vh; box-sizing: border-box; display: flex; flex-direction: column; justify-content: center; }}
  @media (max-width: 700px) {{
    .wrap {{ padding: 16px 16px 28px; min-height: 0; justify-content: flex-start; }}
    .grid-top, .grid-bottom {{ grid-template-columns: 1fr; }}
    .carrier-row {{ grid-template-columns: 1fr 34px; grid-template-areas: "label count" "bar bar"; row-gap: 4px; }}
    .carrier-label {{ grid-area: label; }}
    .carrier-count {{ grid-area: count; }}
    .carrier-bar-track {{ grid-area: bar; }}
  }}
  .grid-top {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 18px; margin-bottom: 18px; }}
  .grid-bottom {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 18px; }}
  .card {{
    background: #ffffff; border-radius: 16px; padding: 18px 24px;
    box-shadow: 0 2px 10px rgba(20,30,50,0.06);
    display: flex; align-items: center; gap: 16px;
  }}
  .icon-box {{
    width: 52px; height: 52px; border-radius: 13px;
    display: flex; align-items: center; justify-content: center;
    font-size: 26px; flex-shrink: 0;
  }}
  .icon-orange {{ background: #fdeee0; }}
  .icon-green {{ background: #e3f6ea; }}
  .icon-purple {{ background: #eee9fb; }}
  .icon-blue {{ background: #e2eefc; }}
  .stat-label {{ font-size: 13px; letter-spacing: 0.05em; color: #8b95a5; font-weight: 700; text-transform: uppercase; margin-bottom: 2px; }}
  .stat-value {{ font-size: 32px; font-weight: 800; }}
  .stat-value.orange {{ color: #e8792b; }}
  .stat-value.green {{ color: #2fa860; }}
  .stat-value.purple {{ color: #6c4fd6; }}
  .stat-value.blue {{ color: #2b7fe0; }}
  .stat-row {{ display: flex; align-items: baseline; gap: 12px; }}
  .stat-sub {{ display: flex; align-items: baseline; gap: 6px; font-size: 20px; font-weight: 700; color: #e8792b; }}
  .stat-sub.purple {{ color: #6c4fd6; }}
  .stat-sub.blue {{ color: #2b7fe0; }}
  .stat-sub-label {{ font-size: 12px; color: #8b95a5; font-weight: 600; text-transform: uppercase; }}
  .divider {{ width: 1px; height: 36px; background: #e5e9f0; margin: 0 4px; }}

  .carriers-card {{ background: #ffffff; border-radius: 16px; padding: 16px 28px; margin-bottom: 18px; box-shadow: 0 2px 10px rgba(20,30,50,0.06); }}
  .carriers-title {{ font-size: 16px; font-weight: 800; margin-bottom: 8px; }}
  .carriers-title span {{ color: #8b95a5; font-weight: 500; font-size: 13px; }}
  .carrier-row {{ display: grid; grid-template-columns: 280px 1fr 40px; align-items: center; gap: 14px; padding: 6px 0; }}
  .carrier-label {{ font-size: 15px; display: flex; align-items: center; gap: 8px; }}
  .carrier-emoji {{ font-size: 17px; }}
  .carrier-bar-track {{ height: 10px; background: #eef1f5; border-radius: 6px; overflow: hidden; }}
  .carrier-bar-fill {{ height: 100%; background: linear-gradient(90deg, #f6c877, #eba54b); border-radius: 6px; }}
  .carrier-count {{ font-size: 16px; font-weight: 800; text-align: right; }}

  .updated {{ text-align: center; font-size: 13px; color: #8b95a5; margin-top: 2px; }}
  .error {{ background: #fbe0e0; color: #a52323; text-align: center; padding: 10px; font-size: 15px; border-radius: 10px; margin-bottom: 12px; }}
  @media (max-width: 1100px) {{ .grid-top, .grid-bottom {{ grid-template-columns: 1fr; }} .carrier-row {{ grid-template-columns: 180px 1fr 40px; }} }}
</style>
</head>
<body>
<div class="wrap">
  {error_banner}
  <div class="grid-top">
    {a_traiter_card}
    <div class="card">
      <div class="icon-box icon-green">⏱️</div>
      <div>
        <div class="stat-label">Temps de traitement estimé</div>
        <div class="stat-value green">{format_minutes(temps_a_traiter)}</div>
      </div>
    </div>
    {prediction_card}
  </div>

  <div class="carriers-card">
    <div class="carriers-title">Commandes par transporteur <span>(à traiter uniquement)</span></div>
    {carrier_rows}
  </div>

  <div class="grid-bottom">
    <div class="card">
      <div class="icon-box icon-purple">🕐</div>
      <div>
        <div class="stat-label">Précommandes en cours</div>
        <div class="stat-row">
          <div class="stat-value purple">{precommandes}</div>
          <div class="divider"></div>
          <div>
            <div class="stat-sub purple">🛒 {format_plateaux(precommandes)}</div>
            <div class="stat-sub-label">Plateaux</div>
          </div>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="icon-box icon-purple">⏱️</div>
      <div>
        <div class="stat-label">Temps de traitement précommandes</div>
        <div class="stat-value purple">{format_minutes(temps_precommandes)}</div>
      </div>
    </div>
  </div>

  <div class="updated">Dernière mise à jour : {updated_at} (rafraîchissement auto toutes les {REFRESH_SECONDS} sec)</div>
</div>
</body>
</html>"""


# --------------------------------------------------------------------
# Serveur HTTP minimal
# --------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        html = render_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, fmt, *args):
        return


def main():
    if CLIENT_ID.startswith("REMPLACE_MOI") or CLIENT_SECRET.startswith("REMPLACE_MOI"):
        print("!! Pense a definir SHOPIFY_CLIENT_ID et SHOPIFY_CLIENT_SECRET !!")
    refresh_cache()
    t = threading.Thread(target=background_refresher, daemon=True)
    t.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Dashboard TV Unipap's disponible sur http://localhost:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
