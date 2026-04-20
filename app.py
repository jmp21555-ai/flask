from flask import Flask, request, jsonify
from binance.client import Client
from binance.enums import *
import os, logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

client = Client(
    os.environ['BINANCE_API_KEY'],
    os.environ['BINANCE_API_SECRET']
)

WEBHOOK_SECRET  = os.environ['WEBHOOK_SECRET']
SYMBOL          = os.environ.get('TRADING_PAIR', 'BTCUSDC')
QUOTE_QTY       = float(os.environ.get('ORDER_AMOUNT_USDC', 2000))
SL_PCT          = float(os.environ.get('SL_PCT', 5.0))  # -5% anti-crash

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()

    # ── Sécurité ──────────────────────────────────────────
    if not data or data.get('secret') != WEBHOOK_SECRET:
        logging.warning("Webhook rejeté : secret invalide")
        return jsonify({"error": "unauthorized"}), 401

    side = data.get('side', '').upper()
    symbol = data.get('symbol', SYMBOL)

    # ── BUY ───────────────────────────────────────────────
    if side == 'BUY':
        # 1. Ordre d'achat market
        buy_order = client.order_market_buy(
            symbol=symbol,
            quoteOrderQty=QUOTE_QTY
        )
        logging.info(f"BUY exécuté : {buy_order}")

        # 2. Prix d'entrée réel obtenu
        entry_price = float(buy_order['fills'][0]['price'])

        # 3. Calcul du SL à -5%
        sl_price = round(entry_price * (1 - SL_PCT / 100), 2)

        # 4. Quantité BTC achetée
        qty_btc = float(buy_order['executedQty'])

        # 5. Pose du Stop Loss Market sur Binance
        sl_order = client.create_order(
            symbol=symbol,
            side=SIDE_SELL,
            type=ORDER_TYPE_STOP_LOSS,
            quantity=qty_btc,
            stopPrice=sl_price,
            timeInForce=TIME_IN_FORCE_GTC
        )
        logging.info(f"SL posé à {sl_price} USDC : {sl_order}")

        return jsonify({
            "status": "ok",
            "side": "BUY",
            "entry_price": entry_price,
            "qty_btc": qty_btc,
            "sl_price": sl_price,
            "sl_order_id": sl_order['orderId']
        })

    # ── SELL ──────────────────────────────────────────────
    elif side == 'SELL':
        # 1. Récupère la quantité BTC disponible
        balance = client.get_asset_balance(asset='BTC')
        qty_btc = float(balance['free'])

        if qty_btc < 0.0001:
            logging.warning("SELL ignoré : pas de BTC en position")
            return jsonify({"status": "no_position"}), 200

        # 2. Annule tous les ordres SL ouverts
        open_orders = client.get_open_orders(symbol=symbol)
        for order in open_orders:
            client.cancel_order(symbol=symbol, orderId=order['orderId'])
            logging.info(f"SL annulé : orderId {order['orderId']}")

        # 3. Vente Market de tout le BTC
        sell_order = client.order_market_sell(
            symbol=symbol,
            quantity=round(qty_btc, 5)
        )
        logging.info(f"SELL exécuté : {sell_order}")

        return jsonify({
            "status": "ok",
            "side": "SELL",
            "qty_btc": qty_btc
        })

    return jsonify({"error": "side invalide"}), 400


@app.route('/test-binance', methods=['GET'])
def test_binance():
    balance = client.get_asset_balance(asset='USDC')
    return jsonify({
        "status": "ok",
        "usdc_balance": balance['free']
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
