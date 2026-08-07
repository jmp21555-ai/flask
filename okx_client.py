import hmac, base64, hashlib, time, json, os, requests

BASE_URL = "https://www.okx.com"
DEMO = os.environ.get('OKX_DEMO', '0') == '1'

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

def get_equity_usdc():
    """Retourne l'équité totale du compte en USDC."""
    path = "/api/v5/account/balance?ccy=USDC"
    r = requests.get(BASE_URL + path, headers=_headers("GET", path))
    data = r.json()
    try:
        return float(data["data"][0]["details"][0]["eq"])
    except (KeyError, IndexError, TypeError):
        return 0.0

def get_btc_balance():
    """Retourne le solde BTC disponible (position actuelle)."""
    path = "/api/v5/account/balance?ccy=BTC"
    r = requests.get(BASE_URL + path, headers=_headers("GET", path))
    data = r.json()
    try:
        return float(data["data"][0]["details"][0]["availBal"])
    except (KeyError, IndexError, TypeError):
        return 0.0

def get_ticker(inst_id="BTC-USDC"):
    path = f"/api/v5/market/ticker?instId={inst_id}"
    r = requests.get(BASE_URL + path, headers=_headers("GET", path))
    data = r.json()
    return float(data["data"][0]["last"])

def place_market_buy(notional_usdc, inst_id="BTC-USDC"):
    """Achat Market en montant USDC (comme quoteOrderQty sur Binance)."""
    path = "/api/v5/trade/order"
    body = json.dumps({
        "instId": inst_id,
        "tdMode": "cash",
        "side": "buy",
        "ordType": "market",
        "sz": str(round(notional_usdc, 2)),
        "tgtCcy": "quote_ccy"   # sz exprimé en USDC
    })
    r = requests.post(BASE_URL + path, headers=_headers("POST", path, body), data=body)
    return r.json()

def place_market_sell(qty_btc, inst_id="BTC-USDC"):
    """Vente Market en quantité BTC."""
    path = "/api/v5/trade/order"
    body = json.dumps({
        "instId": inst_id,
        "tdMode": "cash",
        "side": "sell",
        "ordType": "market",
        "sz": str(qty_btc),
        "tgtCcy": "base_ccy"
    })
    r = requests.post(BASE_URL + path, headers=_headers("POST", path, body), data=body)
    return r.json()

def get_order_details(order_id, inst_id="BTC-USDC"):
    """Récupère le détail d'un ordre (prix moyen rempli, qty, etc.)."""
    path = f"/api/v5/trade/order?instId={inst_id}&ordId={order_id}"
    r = requests.get(BASE_URL + path, headers=_headers("GET", path))
    return r.json()

def place_stop_loss(qty_btc, trigger_price, inst_id="BTC-USDC"):
    """Pose un ordre stop (algo order) qui se déclenche en Market à trigger_price."""
    path = "/api/v5/trade/order-algo"
    body = json.dumps({
        "instId": inst_id,
        "tdMode": "cash",
        "side": "sell",
        "ordType": "conditional",
        "sz": str(qty_btc),
        "triggerPx": str(round(trigger_price, 1)),
        "orderPx": "-1"          # -1 = exécution Market au déclenchement
    })
    r = requests.post(BASE_URL + path, headers=_headers("POST", path, body), data=body)
    return r.json()

def cancel_all_algo_orders(inst_id="BTC-USDC"):
    """Annule tous les ordres stop (algo) ouverts sur l'instrument."""
    path = f"/api/v5/trade/orders-algo-pending?instType=SPOT&instId={inst_id}&ordType=conditional"
    r = requests.get(BASE_URL + path, headers=_headers("GET", path))
    open_algos = r.json().get("data", [])
    if not open_algos:
        return []
    cancel_path = "/api/v5/trade/cancel-algos"
    body = json.dumps([{"algoId": o["algoId"], "instId": inst_id} for o in open_algos])
    r2 = requests.post(BASE_URL + cancel_path, headers=_headers("POST", cancel_path, body), data=body)
    return r2.json()
