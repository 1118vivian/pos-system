import json
import os
from datetime import datetime
from functools import wraps
from pathlib import Path
from threading import Lock

from flask import Flask, jsonify, redirect, request, send_from_directory, session, url_for

from config import APP_PASSWORD, SECRET_KEY
from sheets import get_orders, get_product_grouped, save_order, update_order, validate_sheet_schema

app = Flask(__name__)
app.secret_key = SECRET_KEY
order_lock = Lock()
settings_lock = Lock()
SETTINGS_FILE = Path(__file__).with_name("order_settings.json")
DEFAULT_SETTINGS = {
    "ordering_enabled": True,
    "start_at": "",
    "end_at": "",
    "bank_name": os.environ.get("BANK_NAME", "請設定銀行名稱"),
    "bank_code": os.environ.get("BANK_CODE", ""),
    "bank_account": os.environ.get("BANK_ACCOUNT", "請設定匯款帳號"),
    "account_name": os.environ.get("BANK_ACCOUNT_NAME", ""),
}


@app.after_request
def disable_browser_cache(response):
    """Ordering screens change frequently; always show the current version."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if session.get("logged_in"):
            return func(*args, **kwargs)
        if request.path.startswith("/api/") or request.path in {"/orders", "/sheet_check"}:
            return jsonify({"status": "error", "message": "請先登入"}), 401
        return redirect(url_for("login"))
    return wrapper


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return settings


def parse_local_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def ordering_status():
    settings = load_settings()
    now = datetime.now()
    start = parse_local_datetime(settings.get("start_at"))
    end = parse_local_datetime(settings.get("end_at"))
    is_open = bool(settings.get("ordering_enabled", True))
    reason = "目前開放訂購"
    if not is_open:
        reason = "目前暫停訂購"
    elif start and now < start:
        is_open, reason = False, f"訂購將於 {start:%Y-%m-%d %H:%M} 開放"
    elif end and now > end:
        is_open, reason = False, "本次訂購時間已結束"
    return {"is_open": is_open, "message": reason, "start_at": settings.get("start_at", ""), "end_at": settings.get("end_at", "")}


@app.route("/")
def home():
    return redirect(url_for("pos"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if request.form.get("password", "") == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("orders_page"))
        error = "密碼錯誤"
    return f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>管理員登入</title><link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css' rel='stylesheet'></head><body class='bg-light d-flex align-items-center justify-content-center min-vh-100'><form method='post' class='bg-white border rounded-3 p-4 shadow-sm' style='width:min(360px,calc(100vw - 32px))'><h4>訂單管理登入</h4><input class='form-control my-3' type='password' name='password' placeholder='管理密碼' autofocus required><button class='btn btn-primary w-100'>登入</button><div class='text-danger mt-2'>{error}</div></form></body></html>"""


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("pos"))


@app.route("/pos")
def pos():
    return send_from_directory(".", "index.html")


@app.route("/products_grouped")
def products_grouped():
    return jsonify(get_product_grouped())


@app.route("/ordering_status")
def public_ordering_status():
    return jsonify(ordering_status())


def generate_order_id():
    today = datetime.now().strftime("%Y%m%d")
    max_seq = 0
    for order in get_orders():
        order_id = str(order.get("order_id", ""))
        if order_id.startswith(today + "-") and order_id.split("-")[-1].isdigit():
            max_seq = max(max_seq, int(order_id.split("-")[-1]))
    return f"{today}-{max_seq + 1:03d}"


@app.route("/submit_order", methods=["POST"])
def submit_order():
    status = ordering_status()
    if not status["is_open"]:
        return jsonify({"status": "error", "message": status["message"]}), 403
    data = request.get_json(silent=True) or {}
    customer_name = str(data.get("customer_name", "")).strip()
    group = str(data.get("group", "")).strip()
    valid_groups = {"P", *(f"G{number:02d}" for number in range(1, 16))}
    if group not in valid_groups:
        return jsonify({"status": "error", "message": "請先選擇組別"}), 400
    if not customer_name:
        return jsonify({"status": "error", "message": "請填寫訂購人姓名"}), 400
    if not isinstance(data.get("items"), list) or not data["items"]:
        return jsonify({"status": "error", "message": "訂單內沒有商品"}), 400
    # The order page is public, so never trust prices or product names sent by
    # the browser. Resolve every item from the current product catalogue.
    catalogue = {
        variant["sku"]: (product, variant)
        for product in get_product_grouped()
        for variant in product.get("sizes", [])
    }
    verified_items = []
    for requested in data["items"]:
        match = catalogue.get(str(requested.get("sku", "")))
        qty = requested.get("qty", 0)
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            qty = 0
        if not match or qty < 1 or qty > 999:
            return jsonify({"status": "error", "message": "商品資料已變更，請重新整理後再訂購"}), 400
        product, variant = match
        verified_items.append({"sku": variant["sku"], "name": product["name"], "size": variant["size"], "price": variant["price"], "qty": qty})
    data["items"] = verified_items
    try:
        with order_lock:
            order_id = generate_order_id()
            saved = save_order(order_id, data)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("save order failed")
        return jsonify({"status": "error", "message": f"訂單送出失敗：{exc}"}), 500
    settings = load_settings()
    return jsonify({
        "status": "success", "message": "訂購完成，謝謝訂購", **saved,
        "payment": {"bank_name": settings["bank_name"], "bank_code": settings.get("bank_code", ""), "bank_account": settings["bank_account"], "account_name": settings.get("account_name", ""), "amount": saved["total"]},
    })


@app.route("/orders")
@login_required
def orders():
    return jsonify(get_orders())


@app.route("/orders/<order_id>", methods=["PUT"])
@login_required
def edit_order(order_id):
    try:
        updated = update_order(order_id, request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "success", "order": updated, "message": "訂單已更新"})


@app.route("/orders_page")
@login_required
def orders_page():
    return send_from_directory(".", "orders.html")


@app.route("/api/settings", methods=["GET", "PUT"])
@login_required
def settings_api():
    if request.method == "GET":
        return jsonify(load_settings())
    incoming = request.get_json(silent=True) or {}
    allowed = {"ordering_enabled", "start_at", "end_at", "bank_name", "bank_code", "bank_account", "account_name"}
    settings = load_settings()
    settings.update({key: incoming[key] for key in allowed if key in incoming})
    with settings_lock:
        SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"status": "success", "settings": settings, "ordering": ordering_status()})


@app.route("/sheet_check")
@login_required
def sheet_check():
    return jsonify(validate_sheet_schema())


if __name__ == "__main__":
    app.run(debug=False)
