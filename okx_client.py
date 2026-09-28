import hmac, base64, hashlib, time, json, os, uuid, requests, logging


class OkxError(Exception):
    """Erreur renvoyée par l'API OKX (authentification, IP, maintenance...)."""

BASE_URL = "https://my.okx.com"
DEMO = os.environ.get('OKX_DEMO', '0') == '1'

# Pas de quantité BTC pour BTC-USDC (8 décimales). Si besoin, remplacer par un
# appel à /api/v5/public/instruments pour lire lotSz dynamiquement.
BTC_LOT_SIZE = 0.00000001


def _timestamp():
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()) + f".{int(time.time()*1000)%1000:03d}Z"


def _headers(method, path, body=""):
    ts = _timestamp()
    msg = ts + method.upper() + path + body
    sig = base64.b64encode(
        hmac.new(os.environ['OKX_SECRET_KEY'].encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    h = {
        "OK-ACCESS-KEY": os.environ['OKX_API_KEY'],
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": os.environ['OKX_PASSPHRASE'],
        "Content-Type": "application/json"
    }
    if DEMO:
        h["x-simulated-trading"] = "1"
    return h


def new_cl_ord_id(prefix="bot"):
    """Identifiant client unique par ordre, pour éviter les doublons OKX."""
    return f"{prefix}{uuid.uuid4().hex[:20]}"


def round_down_lot(qty_btc, lot_size=BTC_LOT_SIZE):
    """Arrondit vers le bas au pas de quantité de l'instrument."""
    steps = int(qty_btc / lot_size)
    return round(steps * lot_size, 8)


def _balance_details():
    """Détails du solde. Lève OkxError si OKX répond par une erreur,
    pour ne JAMAIS confondre une lecture échouée avec un solde nul."""
    path = "/api/v5/account/balance"
    r = requests.get(BASE_URL + path, headers=_headers("GET", path), timeout=10)
    try:
        data = r.json()
    except ValueError:
        raise OkxError(f"balance: réponse non JSON (HTTP {r.status_code})")
    if data.get("code") != "0":
        msg = f"balance: code={data.get('code')} msg={data.get('msg')}"
        logging.error(f"Lecture du solde refusée par OKX : {msg}")
        raise OkxError(msg)
    rows = data.get("data") or []
    if not rows:
        return []
    return rows[0].get("details") or []


def get_equity_usdc():
    """Équité USDC. 0.0 uniquement si OKX confirme qu'il n'y a pas d'USDC."""
    for d in _balance_details():
        if d.get("ccy") == "USDC":
            return float(d.get("eq") or 0.0)
    return 0.0


def get_btc_balance():
    """Solde BTC DISPONIBLE (hors BTC bloqué par un stop). Ne pas utiliser pour détecter une position."""
    return _get_btc_field("availBal")


def get_btc_total():
    """Solde BTC TOTAL (disponible + bloqué). À utiliser pour savoir si une position existe."""
    return _get_btc_field("cashBal")


def get_btc_frozen():
    """Solde BTC bloqué (ex: réservé par un stop-loss ouvert)."""
    return _get_btc_field("frozenBal")


def _get_btc_field(field):
    for d in _balance_details():
        if d.get("ccy") == "BTC":
            return float(d.get(field) or 0.0)
    return 0.0


def get_ticker(inst_id="BTC-USDC"):
    path = f"/api/v5/market/ticker?instId={inst_id}"
    r = requests.get(BASE_URL + path, headers=_headers("GET", path), timeout=10)
    data = r.json()
    return float(data["data"][0]["last"])


def place_market_buy(notional_usdc, inst_id="BTC-USDC", cl_ord_id=None):
    """Achat Market en montant USDC (comme quoteOrderQty sur Binance)."""
    path = "/api/v5/trade/order"
    body_dict = {
        "instId": inst_id,
        "tdMode": "cash",
        "side": "buy",
        "ordType": "market",
        "sz": str(round(notional_usdc, 2)),
        "tgtCcy": "quote_ccy",
        "slippagePct": "0.05"
    }
    if cl_ord_id:
        body_dict["clOrdId"] = cl_ord_id
    body = json.dumps(body_dict)
    r = requests.post(BASE_URL + path, headers=_headers("POST", path, body), data=body, timeout=10)
    return r.json()


def place_market_sell(qty_btc, inst_id="BTC-USDC", cl_ord_id=None):
    """Vente Market en quantité BTC (arrondie au pas de l'instrument)."""
    qty = round_down_lot(float(qty_btc))
    path = "/api/v5/trade/order"
    body_dict = {
        "instId": inst_id,
        "tdMode": "cash",
        "side": "sell",
        "ordType": "market",
        "sz": str(qty),
        "tgtCcy": "base_ccy",
        "slippagePct": "0.05"
    }
    if cl_ord_id:
        body_dict["clOrdId"] = cl_ord_id
    body = json.dumps(body_dict)
    r = requests.post(BASE_URL + path, headers=_headers("POST", path, body), data=body, timeout=10)
    return r.json()


def get_order_details(order_id, inst_id="BTC-USDC"):
    """Récupère le détail d'un ordre (prix moyen rempli, qty, etc.)."""
    path = f"/api/v5/trade/order?instId={inst_id}&ordId={order_id}"
    r = requests.get(BASE_URL + path, headers=_headers("GET", path), timeout=10)
    return r.json()


def place_stop_loss(qty_btc, trigger_price, inst_id="BTC-USDC", cl_ord_id=None):
    """Pose un ordre stop (algo order) qui se déclenche en Market à trigger_price."""
    qty = round_down_lot(float(qty_btc))
    path = "/api/v5/trade/order-algo"
    body_dict = {
        "instId": inst_id,
        "tdMode": "cash",
        "side": "sell",
        "ordType": "conditional",
        "sz": str(qty),
        "slTriggerPx": str(round(trigger_price, 1)),
        "slOrdPx": "-1"
    }
    if cl_ord_id:
        body_dict["algoClOrdId"] = cl_ord_id
    body = json.dumps(body_dict)
    r = requests.post(BASE_URL + path, headers=_headers("POST", path, body), data=body, timeout=10)
    return r.json()


def cancel_all_algo_orders(inst_id="BTC-USDC"):
    """Annule tous les ordres stop (algo) ouverts sur l'instrument."""
    path = f"/api/v5/trade/orders-algo-pending?instType=SPOT&instId={inst_id}&ordType=conditional"
    r = requests.get(BASE_URL + path, headers=_headers("GET", path), timeout=10)
    open_algos = r.json().get("data", [])
    if not open_algos:
        return []
    cancel_path = "/api/v5/trade/cancel-algos"
    body = json.dumps([{"algoId": o["algoId"], "instId": inst_id} for o in open_algos])
    r2 = requests.post(BASE_URL + cancel_path, headers=_headers("POST", cancel_path, body), data=body, timeout=10)
    return r2.json()

