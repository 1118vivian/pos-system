from flask import Flask, jsonify, redirect, request, send_from_directory, session, url_for
from datetime import datetime
from functools import wraps
from threading import Lock
from config import APP_PASSWORD, SECRET_KEY

# 👉 sheets
from sheets import (
    get_products,
    get_variants,
    get_product_grouped,
    save_order,
    get_orders,
    update_order,
    validate_sheet_schema
)

app = Flask(__name__)
app.secret_key = SECRET_KEY
order_lock = Lock()


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if session.get("logged_in"):
            return func(*args, **kwargs)

        if request.path.startswith("/api/"):
            return jsonify({"status": "error", "message": "請先登入"}), 401

        return redirect(url_for("login"))

    return wrapper

# =========================
# 🏠 首頁
# =========================
@app.route("/")
def home():
    if session.get("logged_in"):
        return redirect(url_for("pos"))

    return redirect(url_for("login"))


# =========================
# 🔐 簡易登入
# =========================
@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""

    if request.method == "POST":
        password = request.form.get("password", "")

        if password == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("pos"))

        error = "密碼錯誤"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>POS 登入</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
        body {{
            min-height: 100vh;
            display: grid;
            place-items: center;
            background: #f8f9fa;
            font-family: Arial;
        }}
        .login-box {{
            width: min(360px, calc(100vw - 32px));
            padding: 20px;
            background: white;
            border: 1px solid #ddd;
            border-radius: 8px;
        }}
        </style>
    </head>
    <body>
        <form class="login-box" method="post">
            <h4 class="mb-3">POS 系統登入</h4>
            <input class="form-control mb-2" type="password" name="password" placeholder="密碼" autofocus>
            <button class="btn btn-primary w-100" type="submit">登入</button>
            <div class="text-danger mt-2">{error}</div>
        </form>
    </body>
    </html>
    """


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =========================
# 🖥 POS 畫面
# =========================
@app.route("/pos")
@login_required
def pos():
    return send_from_directory(".", "index.html")


# =========================
# 📦 商品
# =========================
@app.route("/products")
@login_required
def products():
    return jsonify(get_products())


# =========================
# 📏 尺寸
# =========================
@app.route("/variants")
@login_required
def variants():
    return jsonify(get_variants())


# =========================
# 📦 商品整合（POS用）
# =========================
@app.route("/products_grouped")
@login_required
def products_grouped():
    return jsonify(get_product_grouped())


# =========================
# 🚀 ORDER ID（穩定短號版本）
# =========================
def generate_order_id():

    today = datetime.now().strftime("%Y%m%d")

    orders = get_orders()

    max_seq = 0

    for o in orders:

        oid = str(o.get("order_id", "")).strip()

        # ❗只接受標準格式 YYYYMMDD-XXX
        if not oid or "-" not in oid:
            continue

        parts = oid.split("-")

        if len(parts) != 2:
            continue

        date_part, seq_part = parts

        if date_part != today:
            continue

        if not seq_part.isdigit():
            continue

        seq = int(seq_part)

        if seq > max_seq:
            max_seq = seq

    return f"{today}-{max_seq + 1:03d}"


# =========================
# 🚀 送出訂單
# =========================
@app.route("/submit_order", methods=["POST"])
@login_required
def submit_order():

    data = request.json

    with order_lock:
        # 🔥 使用短 order id，並用鎖避免同一台伺服器瞬間重複編號
        order_id = generate_order_id()
        save_order(order_id, data)

    return jsonify({
        "status": "success",
        "order_id": order_id,
        "message": "訂單已成功送出"
    })


# =========================
# 📊 訂單總覽
# =========================
@app.route("/orders")
@login_required
def orders():
    return jsonify(get_orders())


# =========================
# ✏️ 修改訂單
# =========================
@app.route("/orders/<order_id>", methods=["PUT"])
@login_required
def edit_order(order_id):

    data = request.json or {}

    try:
        updated = update_order(order_id, data)
    except ValueError as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 404

    return jsonify({
        "status": "success",
        "order": updated,
        "message": "訂單已更新"
    })


# =========================
# 📄 訂單頁面
# =========================
@app.route("/orders_page")
@login_required
def orders_page():
    return send_from_directory(".", "orders.html")


# =========================
# 🧪 Google Sheet 欄位檢查
# =========================
@app.route("/sheet_check")
@login_required
def sheet_check():
    return jsonify(validate_sheet_schema())


# =========================
# 🚀 啟動
# =========================
if __name__ == "__main__":
    print(validate_sheet_schema())
    app.run(debug=False)
