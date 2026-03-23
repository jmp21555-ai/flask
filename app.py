import os, json, logging
from flask import Flask, request, jsonify
from binance.client import Client

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
API_KEY        = os.environ.get("BINANCE_API_KEY", "")
API_SECRET     = os.environ.get("BINANCE_SECRET", "")

client = Client(API_KEY, API_SECRET)

in_position = False
qty_bought  = 0.0
symbol_held = ""

@app.route("/webhook", methods=["POST"])
def webhook():
    global in_position, qty_bought, symbol_held

    token = request.args.get("token") or request.headers.get("X-TV-Token")
    if token != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 403

    try:
        data = request.get_json(force=True)
        symbol = data["symbol"]
        side   = data["side"]
    except (KeyError, TypeError) as e:
        return jsonify({"error": f"payload invalide: {e}"}), 400

    if side == "BUY":
        if in_position:
            logging.info("BUY ignoré — déjà en position")
            return jsonify({"status": "skipped"}), 200
        try:
            quote_qty = float(data.get("quote_qty", 100))
            order = client.order_market_buy(
                symbol=symbol,
                quoteOrderQty=quote_qty
            )
            in_position = True
            qty_bought  = float(order["executedQty"])
            symbol_held = symbol
            logging.info(f"BUY exécuté: {order['orderId']} | {symbol} | {quote_qty} USDC")
            return jsonify({"status": "ok", "orderId": order["orderId"]}), 200
        except Exception as e:
            logging.error(f"Erreur BUY: {e}")
            return jsonify({"error": str(e)}), 500

    elif side == "SELL":
        if not in_position:
            logging.info("SELL ignoré — pas en position")
            return jsonify({"status": "skipped"}), 200
        try:
            order = client.order_market_sell(
                symbol=symbol_held,
                quantity=qty_bought
            )
            in_position = False
            qty_bought  = 0.0
            logging.info(f"SELL exécuté: {order['orderId']} | {symbol_held}")
            return jsonify({"status": "ok", "orderId": order["orderId"]}), 200
        except Exception as e:
            logging.error(f"Erreur SELL: {e}")
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "side invalide"}), 400

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "in_position": in_position}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
