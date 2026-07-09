import os, requests
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

INSTACART_API_KEY = os.environ["INSTACART_API_KEY"]
INSTACART_BASE    = "https://connect.dev.instacart.tools/idp/v1"
HEADERS           = {
    "Authorization": f"Bearer {INSTACART_API_KEY}",
    "Content-Type":  "application/json",
}

PE_UPCS = [
    "855140002168","855140002175","855140002151","855140002144",
    "855140002984","855140002991","855140002656","855140002663",
    "855140002687","855140002700","855140002724",
    "810589031971","810589031964","810589031988","810589032039",
    "810589032015","810589032220","810589032244","810589032183",
    "810589032541","810589032596","810589032602","810589032619",
    "810589032411","810589032435","810589032459",
    "810589032794","810589032800",
    "810589030035","810589031698","810589031940","810589031957","810589032688",
]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/retailers")
def retailers():
    postal_code  = request.args.get("postal_code", "")
    country_code = request.args.get("country_code", "US")
    if not postal_code:
        return jsonify({"error": "postal_code required"}), 400

    r = requests.get(
        f"{INSTACART_BASE}/retailers",
        headers=HEADERS,
        params={"postal_code": postal_code, "country_code": country_code},
        timeout=10,
    )
    return jsonify(r.json()), r.status_code


@app.route("/api/shop-link", methods=["POST"])
def shop_link():
    data         = request.json or {}
    postal_code  = data.get("postal_code", "")
    retailer_key = data.get("retailer_key")   # optional — scopes to one retailer
    line_items   = [
        {"name": "Purely Elizabeth Granola & Oatmeal", "upcs": PE_UPCS, "quantity": 1}
    ]
    payload = {
        "title":       "Purely Elizabeth Products",
        "line_items":  line_items,
        "expires_in":  30,
    }
    if retailer_key:
        payload["partner_linkback_url"] = f"https://instacart.com/store/{retailer_key}"

    r = requests.post(
        f"{INSTACART_BASE}/products/products_link",
        headers=HEADERS,
        json=payload,
        timeout=10,
    )
    return jsonify(r.json()), r.status_code


if __name__ == "__main__":
    app.run(debug=True, port=5001)
