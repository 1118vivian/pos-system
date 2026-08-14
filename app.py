import json
import os
from io import BytesIO
from datetime import datetime
from functools import wraps
from pathlib import Path
from threading import Lock

from flask import Flask, jsonify, redirect, request, send_file, send_from_directory, session, url_for
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from config import APP_PASSWORD, SECRET_KEY
from sheets import get_active_term, get_order_terms, get_orders, get_product_grouped, get_system_settings, initialize_order_terms, save_order, set_active_term, set_system_settings, update_order, validate_sheet_schema

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
    "academic_year": datetime.now().year,
    "semester": 1,
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
    try:
        saved = get_system_settings()
        saved.pop("active_term", None)
        settings.update(saved)
    except Exception:
        app.logger.exception("Unable to read system settings from Google Sheet")
    return settings


def parse_local_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def current_term(settings=None):
    supplied_settings = settings is not None
    settings = settings or load_settings()
    try:
        year = int(settings.get("academic_year", datetime.now().year))
        semester = int(settings.get("semester", 1))
    except (TypeError, ValueError):
        year, semester = datetime.now().year, 1
    semester = 2 if semester == 2 else 1
    fallback = f"{year}-{semester}"
    return fallback if supplied_settings else get_active_term(fallback)


def term_label(term):
    if term == "legacy":
        return "歷史資料（未標示學期）"
    try:
        year, semester = str(term).split("-", 1)
        return f"{int(year)} 年第 {int(semester)} 學期"
    except (TypeError, ValueError):
        return str(term or "")


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
    term = current_term()
    return {"is_open": is_open, "message": reason, "start_at": settings.get("start_at", ""), "end_at": settings.get("end_at", ""), "term": term, "term_label": term_label(term)}


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
    valid_groups = {"P", "EXT", *(f"G{number:02d}" for number in range(1, 16))}
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
            data["term"] = current_term()
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


def group_label(group):
    if group == "P":
        return "個人"
    if group == "EXT":
        return "延伸"
    if str(group).startswith("G") and str(group)[1:].isdigit():
        return f"{int(str(group)[1:])}組"
    return str(group or "")


def receipt_font():
    font_name = "OrderReceiptFont"
    if font_name in pdfmetrics.getRegisteredFontNames():
        return font_name
    candidates = [
        os.environ.get("PDF_FONT_PATH", ""),
        r"C:\Windows\Fonts\msjh.ttc",
        r"C:\Windows\Fonts\mingliu.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansTC-Regular.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            pdfmetrics.registerFont(TTFont(font_name, path, subfontIndex=0))
            return font_name
    pdfmetrics.registerFont(UnicodeCIDFont("MSung-Light"))
    return "MSung-Light"


@app.route("/download_order_pdf", methods=["POST"])
def download_order_pdf():
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    if not isinstance(items, list) or not items or len(items) > 100:
        return jsonify({"status": "error", "message": "訂單明細格式錯誤"}), 400

    order_id = str(data.get("order_id", ""))[:50]
    customer_name = str(data.get("customer_name", ""))[:50]
    order_time = str(data.get("order_time", ""))[:50]
    payment = data.get("payment", {}) if isinstance(data.get("payment"), dict) else {}
    total = sum(safe_pdf_number(item.get("price")) * safe_pdf_number(item.get("qty")) for item in items)

    font_name = receipt_font()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ReceiptTitle", parent=styles["Title"], fontName=font_name, fontSize=20, leading=26, alignment=TA_CENTER)
    body_style = ParagraphStyle("ReceiptBody", parent=styles["BodyText"], fontName=font_name, fontSize=11, leading=17)
    right_style = ParagraphStyle("ReceiptRight", parent=body_style, alignment=TA_RIGHT)
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm, title=f"訂單 {order_id}")
    story = [Paragraph("訂購完成 - 訂單明細", title_style), Spacer(1, 6 * mm)]
    meta = [["訂單編號", order_id], ["學期", term_label(data.get("term"))], ["組別", group_label(data.get("group"))], ["訂購人", customer_name], ["訂單時間", order_time]]
    meta_table = Table(meta, colWidths=[28 * mm, 128 * mm])
    meta_table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), font_name), ("FONTSIZE", (0, 0), (-1, -1), 11), ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555"))]))
    story.extend([meta_table, Spacer(1, 5 * mm)])
    rows = [["商品", "尺寸／規格", "單價", "數量", "小計"]]
    for item in items:
        price, qty = safe_pdf_number(item.get("price")), safe_pdf_number(item.get("qty"))
        rows.append([str(item.get("name", ""))[:100], str(item.get("size", ""))[:30], f"{price:,}", str(qty), f"{price * qty:,}"])
    rows.append(["", "", "", "總計", f"NT$ {total:,}"])
    detail_table = Table(rows, colWidths=[61 * mm, 30 * mm, 22 * mm, 18 * mm, 27 * mm], repeatRows=1)
    detail_table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), font_name), ("FONTSIZE", (0, 0), (-1, -1), 10), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9ECEF")), ("GRID", (0, 0), (-1, -2), 0.5, colors.HexColor("#777777")), ("ALIGN", (2, 1), (-1, -1), "RIGHT"), ("FONTNAME", (3, -1), (-1, -1), font_name), ("FONTSIZE", (3, -1), (-1, -1), 12), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    story.extend([detail_table, Spacer(1, 8 * mm), Paragraph("匯款資訊", ParagraphStyle("PaymentTitle", parent=body_style, fontSize=14, leading=20)), Spacer(1, 2 * mm)])
    bank_code = str(payment.get("bank_code", ""))[:20]
    bank_line = f"{str(payment.get('bank_name', ''))[:50]}" + (f"（銀行代號：{bank_code}）" if bank_code else "")
    story.extend([Paragraph(bank_line, body_style), Paragraph(f"匯款帳號：{str(payment.get('bank_account', ''))[:60]}", body_style), Paragraph(f"戶名：{str(payment.get('account_name', ''))[:50] or '-'}", body_style), Spacer(1, 3 * mm), Paragraph(f"匯款金額：NT$ {total:,}", right_style)])
    document.build(story)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name=f"order-{order_id or 'receipt'}.pdf")


def safe_pdf_number(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


@app.route("/orders")
@login_required
def orders():
    requested_term = request.args.get("term", "").strip() or current_term()
    return jsonify(get_orders(term=requested_term))


@app.route("/api/terms")
@login_required
def terms_api():
    active = current_term()
    initialize_order_terms(active)
    terms = get_order_terms()
    if active not in terms:
        terms.insert(0, active)
    return jsonify({"current_term": active, "current_label": term_label(active), "terms": [{"value": term, "label": term_label(term)} for term in terms]})


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
        settings = load_settings()
        term = current_term()
        year, semester = term.split("-", 1)
        settings["academic_year"], settings["semester"] = int(year), int(semester)
        return jsonify(settings)
    incoming = request.get_json(silent=True) or {}
    allowed = {"ordering_enabled", "start_at", "end_at", "bank_name", "bank_code", "bank_account", "account_name", "academic_year", "semester"}
    settings = load_settings()
    settings.update({key: incoming[key] for key in allowed if key in incoming})
    with settings_lock:
        SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    set_system_settings(settings)
    set_active_term(current_term(settings))
    return jsonify({"status": "success", "settings": settings, "ordering": ordering_status()})


@app.route("/api/settings/next_term", methods=["POST"])
@login_required
def activate_next_term():
    settings = load_settings()
    try:
        active = current_term()
        year, semester = (int(value) for value in active.split("-", 1))
    except (TypeError, ValueError):
        year, semester = datetime.now().year, 1
    if semester == 1:
        semester = 2
    else:
        year, semester = year + 1, 1
    settings["academic_year"], settings["semester"] = year, semester
    set_active_term(f"{year}-{semester}")
    with settings_lock:
        SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    set_system_settings(settings)
    term = current_term(settings)
    return jsonify({"status": "success", "settings": settings, "term": term, "term_label": term_label(term)})


@app.route("/sheet_check")
@login_required
def sheet_check():
    return jsonify(validate_sheet_schema())


if __name__ == "__main__":
    app.run(debug=False)
