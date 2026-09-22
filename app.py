from flask import Flask, request, jsonify
import os, logging, json, time, hmac, threading
import okx_client as okx

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

WEBHOOK_SECRET = os.environ['WEBHOOK_SECRET']
SYMBOL         = os.environ.get('TRADING_PAIR', 'BTC-USDC')
PCT_EQUITY     = float(os.environ.get('ORDER_PCT_EQUITY', 10.0))
SL_PCT         = float(os.environ.get('SL_PCT', 5.0))
MIN_NOTIONAL   = float(os.environ.get('MIN_NOTIONAL_USDC', 10.0))

BTC_DUST_THRESHOLD = 0.0001

# Un seul trade traité à la fois, pour éviter tout chevauchement BUY/SELL
trade_lock = threading.Lock()


def check_secret(value):
    return hmac.compare_digest(str(value or ""), WEBHOOK_SECRET)


@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(silent=True) or {}

    if not check_secret(data.get('secret')):
        logging.warning("Webhook rejeté : secret invalide")
        return jsonify({"error": "unauthorized"}), 401

    side = data.get('side', '').upper()
    symbol = data.get('symbol', SYMBOL)

    if symbol != SYMBOL:
        logging.warning(f"Webhook rejeté : symbole non autorisé ({symbol})")
        return jsonify({"error": "symbole non autorisé"}), 400

    if side not in ('BUY', 'SELL'):
        return jsonify({"error": "side invalide"}), 400

    # Réponse immédiate à TradingView (évite le timeout ~3s) ; traitement en fond.
    threading.Thread(target=process_signal, args=(side, symbol), daemon=True).start()
    return jsonify({"status": "accepted", "side": side}), 200


def process_signal(side, symbol):
    with trade_lock:
        try:
            if side == 'BUY':
                handle_buy(symbol)
            else:
                handle_sell(symbol)
        except Exception:
            logging.exception(f"Erreur non gérée pendant le traitement du signal {side}")


def handle_buy(symbol):
    # 1. Position ouverte ? -> solde TOTAL (disponible + bloqué par un SL existant)
    current_btc_total = okx.get_btc_total()
    if current_btc_total > BTC_DUST_THRESHOLD:
        logging.warning(
            f"BUY ignoré : position déjà ouverte ({current_btc_total} BTC, "
            f"dont bloqué : {okx.get_btc_frozen()})"
        )
        return

    # 2. Montant en % de l'équité
    equity = okx.get_equity_usdc()
    notional = round(equity * (PCT_EQUITY / 100.0), 2)
    if notional < MIN_NOTIONAL:
        logging.warning(f"BUY ignoré : montant {notional} USDC sous le seuil mini")
        return

    # 3. Ordre d'achat Market
    cl_ord_id = okx.new_cl_ord_id("buy")
    buy_result = okx.place_market_buy(notional, inst_id=symbol, cl_ord_id=cl_ord_id)
    logging.info(f"BUY envoyé ({cl_ord_id}) : {buy_result}")

    if buy_result.get("code") != "0":
        logging.error(f"Erreur BUY OKX : {buy_result}")
        return

    order_id = buy_result["data"][0]["ordId"]

    # 4. Prix moyen d'exécution réel
    order_details = okx.get_order_details(order_id, inst_id=symbol)
    try:
        fill = order_details["data"][0]
        entry_price = float(fill["avgPx"])
    except (KeyError, IndexError, ValueError):
        logging.error(f"Impossible de lire les détails de l'ordre : {order_details}")
        return

    # 5. Quantité réelle détenue (le fill peut différer légèrement des frais BTC prélevés)
    time.sleep(1)
    real_qty_btc = okx.get_btc_total()
    logging.info(f"Solde BTC réel avant pose SL : {real_qty_btc}")

    if real_qty_btc <= BTC_DUST_THRESHOLD:
        logging.error("BTC introuvable après achat, SL non posé")
        return

    # 6. Pose du Stop Loss + vérification du résultat
    sl_price = round(entry_price * (1 - SL_PCT / 100.0), 1)
    sl_cl_ord_id = okx.new_cl_ord_id("sl")
    sl_result = okx.place_stop_loss(real_qty_btc, sl_price, inst_id=symbol, cl_ord_id=sl_cl_ord_id)
    logging.info(f"SL posé ({sl_cl_ord_id}) : {sl_result}")

    if sl_result.get("code") != "0":
        logging.error(f"ECHEC pose SL ({sl_result}) -> vente immédiate de sécurité")
        emergency_sell(symbol)


def handle_sell(symbol):
    # 1. Annule le(s) Stop Loss AVANT de lire le solde : sinon le BTC bloqué
    #    par le SL est invisible et le signal est ignoré à tort.
    cancel_result = okx.cancel_all_algo_orders(inst_id=symbol)
    logging.info(f"SL annulés : {cancel_result}")
    time.sleep(0.5)

    qty_btc = okx.get_btc_total()
    if qty_btc < BTC_DUST_THRESHOLD:
        logging.warning("SELL ignoré : pas de BTC en position")
        return

    cl_ord_id = okx.new_cl_ord_id("sell")
    sell_result = okx.place_market_sell(qty_btc, inst_id=symbol, cl_ord_id=cl_ord_id)
    logging.info(f"SELL envoyé ({cl_ord_id}) : {sell_result}")

    if sell_result.get("code") != "0":
        logging.error(f"Erreur SELL OKX : {sell_result}")


def emergency_sell(symbol):
    """Vente de sécurité si la pose du SL a échoué : ne pas laisser une position sans protection."""
    qty_btc = okx.get_btc_total()
    if qty_btc <= BTC_DUST_THRESHOLD:
        return
    cl_ord_id = okx.new_cl_ord_id("emrg")
    result = okx.place_market_sell(qty_btc, inst_id=symbol, cl_ord_id=cl_ord_id)
    logging.error(f"VENTE D'URGENCE ({cl_ord_id}) : {result}")


# ── Routes de debug : protégées par le secret, à retirer avant le passage en réel ──

@app.route('/test-okx', methods=['GET'])
def test_okx():
    if not check_secret(request.args.get('secret')):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({
        "status": "ok",
        "equity_usdc": okx.get_equity_usdc(),
        "btc_available": okx.get_btc_balance(),
        "btc_total": okx.get_btc_total(),
        "btc_frozen": okx.get_btc_frozen(),
    })


@app.route('/debug-config', methods=['GET'])
def debug_config():
    if not check_secret(request.args.get('secret')):
        return jsonify({"error": "unauthorized"}), 401
    api_key = os.environ.get('OKX_API_KEY', '')
    secret = os.environ.get('OKX_SECRET_KEY', '')
    passphrase = os.environ.get('OKX_PASSPHRASE', '')
    demo = os.environ.get('OKX_DEMO', 'NON_DEFINI')
    return jsonify({
        "OKX_DEMO_value": demo,
        "api_key_length": len(api_key),
        "api_key_has_space": api_key != api_key.strip(),
        "secret_length": len(secret),
        "secret_has_space": secret != secret.strip(),
        "passphrase_length": len(passphrase),
        "passphrase_has_space": passphrase != passphrase.strip(),
    })


@app.route('/check-order/<order_id>', methods=['GET'])
def check_order(order_id):
    if not check_secret(request.args.get('secret')):
        return jsonify({"error": "unauthorized"}), 401
    symbol = request.args.get('symbol', SYMBOL)
    return jsonify(okx.get_order_details(order_id, inst_id=symbol))


@app.route('/cleanup-algo', methods=['GET'])
def cleanup_algo():
    if not check_secret(request.args.get('secret')):
        return jsonify({"error": "unauthorized"}), 401
    symbol = request.args.get('symbol', SYMBOL)
    return jsonify(okx.cancel_all_algo_orders(inst_id=symbol))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
