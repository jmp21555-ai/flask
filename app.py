
# force redeploy 2
from flask import Flask, request, jsonify

from flask import Flask, request, jsonify
import os, logging, json, okx_client as okx 

app = Flask(__name__) 
logging.basicConfig(level=logging.INFO)

WEBHOOK_SECRET = os.environ['WEBHOOK_SECRET']
SYMBOL         = os.environ.get('TRADING_PAIR', 'BTC-USDC')
PCT_EQUITY     = float(os.environ.get('ORDER_PCT_EQUITY', 10.0))  # % de l'équité par trade
SL_PCT         = float(os.environ.get('SL_PCT', 5.0))             # -5% anti-crash
MIN_NOTIONAL   = float(os.environ.get('MIN_NOTIONAL_USDC', 10.0))

# Seuil pour considérer qu'on "a" une position (poussière BTC exclue)
BTC_DUST_THRESHOLD = 0.0001


@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()

    # ── Sécurité ──────────────────────────────────────────
    if not data or data.get('secret') != WEBHOOK_SECRET:
        logging.warning("Webhook rejeté : secret invalide")
        return jsonify({"error": "unauthorized"}), 401

    side   = data.get('side', '').upper()
    symbol = data.get('symbol', SYMBOL)

    # ── BUY ───────────────────────────────────────────────
    if side == 'BUY':
        # 1 trade à la fois : on vérifie qu'il n'y a pas déjà une position ouverte
        current_btc = okx.get_btc_balance()
        if current_btc > BTC_DUST_THRESHOLD:
            logging.warning(f"BUY ignoré : position déjà ouverte ({current_btc} BTC)")
            return jsonify({"status": "skip", "reason": "position déjà ouverte"}), 200

        # 2. Calcul du montant en % de l'équité
        equity = okx.get_equity_usdc()
        notional = round(equity * (PCT_EQUITY / 100.0), 2)

        if notional < MIN_NOTIONAL:
            logging.warning(f"BUY ignoré : montant {notional} USDC sous le seuil mini")
            return jsonify({"status": "skip", "reason": "montant insuffisant"}), 200

        # 3. Ordre d'achat Market
        buy_result = okx.place_market_buy(notional, inst_id=symbol)
        logging.info(f"BUY envoyé : {buy_result}")

        if buy_result.get("code") != "0":
            logging.error(f"Erreur BUY OKX : {buy_result}")
            return jsonify({"status": "error", "detail": buy_result}), 500

        order_id = buy_result["data"][0]["ordId"]

        # 4. Récupère le prix moyen d'exécution réel
        order_details = okx.get_order_details(order_id, inst_id=symbol)
        try:
            fill = order_details["data"][0]
            entry_price = float(fill["avgPx"])
            qty_btc     = float(fill["accFillSz"])
        except (KeyError, IndexError, ValueError):
            logging.error(f"Impossible de lire les détails de l'ordre : {order_details}")
            return jsonify({"status": "error", "detail": "lecture ordre échouée"}), 500

        # 5. Calcul et pose du Stop Loss
        sl_price = round(entry_price * (1 - SL_PCT / 100.0), 1)
        sl_result = okx.place_stop_loss(qty_btc, sl_price, inst_id=symbol)
        logging.info(f"SL posé à {sl_price} USDC : {sl_result}")

        return jsonify({
            "status": "ok",
            "side": "BUY",
            "entry_price": entry_price,
            "qty_btc": qty_btc,
            "notional_usdc": notional,
            "sl_price": sl_price,
            "sl_result": sl_result
        })

    # ── SELL ──────────────────────────────────────────────
    elif side == 'SELL':
        qty_btc = okx.get_btc_balance()
        if qty_btc < BTC_DUST_THRESHOLD:
            logging.warning("SELL ignoré : pas de BTC en position")
            return jsonify({"status": "no_position"}), 200

        # 1. Annule le(s) Stop Loss ouverts avant de vendre
        cancel_result = okx.cancel_all_algo_orders(inst_id=symbol)
        logging.info(f"SL annulés : {cancel_result}")

        # 2. Vente Market de tout le BTC disponible
        sell_result = okx.place_market_sell(qty_btc, inst_id=symbol)
        logging.info(f"SELL exécuté : {sell_result}")

        if sell_result.get("code") != "0":
            logging.error(f"Erreur SELL OKX : {sell_result}")
            return jsonify({"status": "error", "detail": sell_result}), 500

        return jsonify({
            "status": "ok",
            "side": "SELL",
            "qty_btc": qty_btc,
            "sell_result": sell_result
        })

    return jsonify({"error": "side invalide"}), 400


@app.route('/test-okx', methods=['GET'])
def test_okx():
    equity = okx.get_equity_usdc()
    btc_bal = okx.get_btc_balance()
    return jsonify({
        "status": "ok",
        "equity_usdc": equity,
        "btc_balance": btc_bal
    })
    
@app.route('/test-sell-small', methods=['GET'])
def test_sell_small():
    if request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    qty = request.args.get('qty', '0.001')
    symbol = request.args.get('symbol', SYMBOL)
    result = okx.place_market_sell(qty, inst_id=symbol)
    logging.info(f"TEST SELL small ({qty} BTC) : {result}")
    return jsonify(result)


@app.route('/test-buy-small', methods=['GET'])
def test_buy_small():
    if request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    notional = float(request.args.get('notional', '20'))
    symbol = request.args.get('symbol', SYMBOL)
    result = okx.place_market_buy(notional, inst_id=symbol)
    logging.info(f"TEST BUY small ({notional} USDC) : {result}")
    return jsonify(result)

@app.route('/test-stop-loss', methods=['GET'])
def test_stop_loss():
    if request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    qty = request.args.get('qty', '0.001')
    trigger = float(request.args.get('trigger', '50000'))
    symbol = request.args.get('symbol', 'BTC-EUR')
    result = okx.place_stop_loss(qty, trigger, inst_id=symbol)
    return jsonify(result)
    
@app.route('/debug-config', methods=['GET'])
def debug_config():
    api_key = os.environ.get('OKX_API_KEY', '')
    secret = os.environ.get('OKX_SECRET_KEY', '')
    passphrase = os.environ.get('OKX_PASSPHRASE', '')
    demo = os.environ.get('OKX_DEMO', 'NON_DEFINI')
    return jsonify({
        "OKX_DEMO_value": demo,
        "api_key_length": len(api_key),
        "api_key_start": api_key[:4] if api_key else "VIDE",
        "api_key_end": api_key[-4:] if api_key else "VIDE",
        "api_key_has_space": api_key != api_key.strip(),
        "secret_length": len(secret),
        "secret_has_space": secret != secret.strip(),
        "passphrase_length": len(passphrase),
        "passphrase_has_space": passphrase != passphrase.strip(),
    })

@app.route('/debug-balance', methods=['GET'])
def debug_balance():
    import requests as req
    path = "/api/v5/account/balance"
    r = req.get(okx.BASE_URL + path, headers=okx._headers("GET", path))
    return jsonify(r.json())

@app.route('/debug-reset-btc', methods=['GET'])
def debug_reset_btc():
    if request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    import requests as req
    amt = request.args.get('amt', '0.99059359')
    adj_type = request.args.get('type', 'reduce')
    path = "/api/v5/account/demo-adjust-balance"
    body = json.dumps({
        "type": adj_type,
        "adjustments": [
            {"ccy": "BTC", "amt": amt}
        ]
    })
    r = req.post(okx.BASE_URL + path, headers=okx._headers("POST", path, body), data=body)
    return jsonify(r.json())

@app.route('/debug-book', methods=['GET'])
def debug_book():
    import requests as req
    inst_id = request.args.get('symbol', SYMBOL)
    sz = request.args.get('sz', '10')
    path = f"/api/v5/market/books?instId={inst_id}&sz={sz}"
    r = req.get(okx.BASE_URL + path, headers=okx._headers("GET", path))
    return jsonify(r.json())
    
@app.route('/check-order/<order_id>', methods=['GET'])
def check_order(order_id):
    symbol = request.args.get('symbol', 'BTC-USDC')
    result = okx.get_order_details(order_id, inst_id=symbol)
    return jsonify(result)

@app.route('/cleanup-algo', methods=['GET'])
def cleanup_algo():
    if request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    symbol = request.args.get('symbol', 'BTC-EUR')
    result = okx.cancel_all_algo_orders(inst_id=symbol)
    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port) 
