import json
import os
import re
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
credentials_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
if credentials_json:
    credentials = Credentials.from_service_account_info(json.loads(credentials_json), scopes=SCOPES)
else:
    credentials = Credentials.from_service_account_file(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json"), scopes=SCOPES)
client = gspread.authorize(credentials)
sheet_cache = client.open_by_key(os.environ.get("SPREADSHEET_ID", "1m6ZQsxYavA37CW8inxYiZ-4ZJT1uV9VFm_UybQ6lRTY"))

PRODUCT_SHEET = "商品主表"
VARIANT_SHEET = "尺寸庫存表"
DETAIL_SHEET = "訂單明細"
MASTER_SHEET = "訂單主表"
SETTINGS_SHEET = "系統設定"
EXPECTED_SHEETS = {
    PRODUCT_SHEET: [["id"], ["name"], ["type"], ["price"], ["active"]],
    VARIANT_SHEET: [["product_id"], ["sku"], ["size"], ["price"], ["stock"]],
    DETAIL_SHEET: [["order_id"], ["date"], ["sku"], ["product_name", "name"], ["size"], ["price"], ["qty"], ["customer_name"], ["term"]],
    MASTER_SHEET: [["order_id"], ["date"], ["items", "name"], ["total"], ["customer_name"], ["term"]],
}


def safe_int(value):
    try:
        return int(float(value)) if value not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def normalize_header(value):
    return str(value).strip().lower()


def find_header_index(headers, *targets):
    normalized = [normalize_header(x) for x in headers]
    for target in targets:
        if normalize_header(target) in normalized:
            return normalized.index(normalize_header(target))
    return None


def clean_key(record, *targets):
    wanted = {normalize_header(x) for x in targets}
    for key, value in record.items():
        if normalize_header(key) in wanted:
            return value
    return ""


def ensure_header(sheet, header):
    headers = sheet.row_values(1)
    if find_header_index(headers, header) is None:
        sheet.update_cell(1, len(headers) + 1, header)
        headers.append(header)
    return headers


def settings_worksheet():
    try:
        return sheet_cache.worksheet(SETTINGS_SHEET)
    except gspread.WorksheetNotFound:
        sheet = sheet_cache.add_worksheet(title=SETTINGS_SHEET, rows=20, cols=2)
        sheet.update("A1:B1", [["key", "value"]])
        return sheet


def get_active_term(default_term):
    settings = get_system_settings()
    if settings.get("active_term"):
        return str(settings["active_term"])
    set_active_term(default_term)
    return default_term


def set_active_term(term):
    set_system_settings({"active_term": term})


def get_system_settings():
    sheet = settings_worksheet()
    result = {}
    for row in sheet.get_all_records():
        key = str(row.get("key", "")).strip()
        raw_value = row.get("value", "")
        if not key:
            continue
        try:
            result[key] = json.loads(str(raw_value))
        except (TypeError, ValueError, json.JSONDecodeError):
            result[key] = raw_value
    return result


def set_system_settings(settings):
    sheet = settings_worksheet()
    values = sheet.get_all_values()
    existing = {str(row[0]).strip(): row_number for row_number, row in enumerate(values[1:], start=2) if row}
    cells = []
    rows = []
    for key, value in settings.items():
        encoded = json.dumps(value, ensure_ascii=False)
        if key in existing:
            cells.append(gspread.Cell(existing[key], 2, encoded))
        else:
            rows.append([key, encoded])
    if cells:
        sheet.update_cells(cells, value_input_option="RAW")
    if rows:
        sheet.append_rows(rows, value_input_option="RAW")


def row_for_headers(headers, values):
    return [values.get(normalize_header(header), "") for header in headers]


def validate_sheet_schema():
    result = []
    for sheet_name, groups in EXPECTED_SHEETS.items():
        try:
            headers = [normalize_header(x) for x in sheet_cache.worksheet(sheet_name).row_values(1)]
            missing = [" / ".join(group) for group in groups if not any(normalize_header(x) in headers for x in group)]
            result.append({"sheet": sheet_name, "ok": not missing, "missing": missing, "headers": headers})
        except Exception as exc:
            result.append({"sheet": sheet_name, "ok": False, "missing": [" / ".join(x) for x in groups], "headers": [], "error": str(exc)})
    return {"ok": all(x["ok"] for x in result), "sheets": result}


def get_products():
    return sheet_cache.worksheet(PRODUCT_SHEET).get_all_records()


def get_variants():
    return sheet_cache.worksheet(VARIANT_SHEET).get_all_records()


def get_product_grouped():
    products, variants = get_products(), get_variants()
    result = []
    male_white_shirt_sizes = []
    for product in products:
        pid = str(product.get("id", "")).strip()
        product_name = str(product.get("name", "")).strip()
        if not pid or not product_name:
            continue
        ptype = str(product.get("type", "variant")).strip().lower()
        category = str(product.get("category", product.get("分類", ""))).strip()
        if category in ("服裝", "衣服"):
            category = "衣物"
        elif category not in ("衣物", "物品"):
            category = "物品" if ptype == "simple" else "衣物"

        # The sheet stores each men's white-shirt neck size as a separate
        # simple product. Present them as one clothing product with four sizes.
        shirt_match = re.fullmatch(r"白襯衫\s*(15(?:\.5)?|16(?:\.5)?)['’]?\s*[（(]男[）)]", product_name)
        if shirt_match:
            male_white_shirt_sizes.append({
                "sku": pid,
                "size": f"{shirt_match.group(1)}'",
                "price": safe_int(product.get("price")),
                "stock": 9999,
            })
            continue
        if ptype == "simple":
            sizes = [{"sku": pid, "size": "單一規格", "price": safe_int(product.get("price")), "stock": 9999}]
        else:
            sizes = [{"sku": str(v.get("sku", "")).strip(), "size": str(v.get("size", "")).strip(), "price": safe_int(v.get("price")), "stock": safe_int(v.get("stock"))} for v in variants if str(v.get("product_id", "")).strip() == pid]
            if sizes and not any(x["size"].upper() == "XS" for x in sizes):
                template = next((x for x in sizes if x["size"].upper() == "S"), sizes[0])
                sizes.insert(0, {"sku": f"{pid}-XS", "size": "XS", "price": template["price"], "stock": template["stock"]})
        result.append({"id": pid, "name": product_name, "active": product.get("active", "TRUE"), "type": ptype, "category": category, "sizes": sizes})

    if male_white_shirt_sizes:
        male_white_shirt_sizes.sort(key=lambda item: float(item["size"].rstrip("'")))
        result.append({
            "id": "MEN-WHITE-SHIRT",
            "name": "白襯衫（男）",
            "active": "TRUE",
            "type": "variant",
            "category": "衣物",
            "sizes": male_white_shirt_sizes,
        })

    def product_sort_key(product):
        name = product["name"]
        family = re.sub(r"[（(](?:男|女)[）)]", "", name).strip()
        gender_order = 0 if re.search(r"[（(]女[）)]", name) else 1 if re.search(r"[（(]男[）)]", name) else 2
        clothing_order = {
            "白襯衫": 0,
            "運動短袖": 1,
            "運動長袖": 2,
            "運動褲": 3,
            "夾克": 4,
            "背心": 5,
            "長裙": 6,
        }
        family_order = clothing_order.get(family, len(clothing_order))
        return (0 if product["category"] == "衣物" else 1, family_order, family, gender_order, name)

    result.sort(key=product_sort_key)
    return result


def order_summary(items):
    valid = []
    for item in items if isinstance(items, list) else []:
        name = str(item.get("name", item.get("product_name", ""))).strip()
        qty = safe_int(item.get("qty"))
        if name and qty > 0:
            valid.append({"sku": str(item.get("sku", "")).strip(), "name": name, "product_name": name, "size": str(item.get("size", "")).strip(), "price": safe_int(item.get("price")), "qty": qty})
    total = sum(x["price"] * x["qty"] for x in valid)
    return valid, " / ".join(x["name"] for x in valid), total


def save_order(order_id, data):
    detail, master = sheet_cache.worksheet(DETAIL_SHEET), sheet_cache.worksheet(MASTER_SHEET)
    customer_name = str(data.get("customer_name", "")).strip()
    group = str(data.get("group", "")).strip()
    term = str(data.get("term", "")).strip()
    valid, names, total = order_summary(data.get("items", []))
    if not customer_name:
        raise ValueError("請填寫訂購人姓名")
    if not valid:
        raise ValueError("訂單內沒有有效商品")
    order_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    detail_headers, master_headers = ensure_header(detail, "customer_name"), ensure_header(master, "customer_name")
    detail_headers, master_headers = ensure_header(detail, "term"), ensure_header(master, "term")
    detail.append_rows([row_for_headers(detail_headers, {"order_id": order_id, "date": order_date, "group": group, **item, "customer_name": customer_name, "term": term}) for item in valid])
    master.append_row(row_for_headers(master_headers, {"order_id": order_id, "date": order_date, "group": group, "items": names, "name": names, "total": total, "customer_name": customer_name, "term": term}))
    return {"order_id": order_id, "order_time": order_date, "customer_name": customer_name, "term": term, "items": valid, "total": total}


def get_orders(term=None):
    rows = sheet_cache.worksheet(DETAIL_SHEET).get_all_records()
    orders = {}
    for row in rows:
        oid = str(clean_key(row, "order_id")).strip()
        if not oid:
            continue
        order_term = str(clean_key(row, "term")).strip() or "legacy"
        if term and order_term != term:
            continue
        order = orders.setdefault(oid, {"order_id": oid, "date": clean_key(row, "date"), "group": clean_key(row, "group"), "customer_name": clean_key(row, "customer_name"), "term": order_term, "items": [], "total": 0})
        name = str(clean_key(row, "product_name", "name")).strip()
        if name:
            item = {"sku": str(clean_key(row, "sku")).strip(), "name": name, "product_name": name, "size": str(clean_key(row, "size", "尺寸")).strip(), "price": safe_int(clean_key(row, "price", "單價")), "qty": safe_int(clean_key(row, "qty", "數量"))}
            order["items"].append(item)
            order["total"] += item["price"] * item["qty"]
    return list(orders.values())


def get_order_terms():
    ensure_header(sheet_cache.worksheet(DETAIL_SHEET), "term")
    ensure_header(sheet_cache.worksheet(MASTER_SHEET), "term")
    terms = {order.get("term", "legacy") for order in get_orders()}
    return sorted(terms, key=lambda value: (value == "legacy", value), reverse=False)


def initialize_order_terms(term):
    """One-time migration: treat all pre-semester orders as the active term."""
    sheets = [sheet_cache.worksheet(DETAIL_SHEET), sheet_cache.worksheet(MASTER_SHEET)]
    all_term_values = []
    sheet_data = []
    for sheet in sheets:
        headers = ensure_header(sheet, "term")
        values = sheet.get_all_values()
        term_index = find_header_index(headers, "term")
        order_index = find_header_index(headers, "order_id")
        populated = []
        for row in values[1:]:
            value = row[term_index].strip() if term_index is not None and term_index < len(row) else ""
            if value:
                populated.append(value)
        all_term_values.extend(populated)
        sheet_data.append((sheet, values, term_index, order_index))
    if all_term_values:
        return False
    for sheet, values, term_index, order_index in sheet_data:
        cells = []
        for row_number, row in enumerate(values[1:], start=2):
            order_id = row[order_index].strip() if order_index is not None and order_index < len(row) else ""
            if order_id:
                cells.append(gspread.Cell(row_number, term_index + 1, term))
        if cells:
            sheet.update_cells(cells, value_input_option="RAW")
    return True


def update_order(order_id, data):
    detail = sheet_cache.worksheet(DETAIL_SHEET)
    values = detail.get_all_values()
    if not values:
        raise ValueError("找不到訂單資料")
    headers = values[0]
    oid_index = find_header_index(headers, "order_id")
    matching = [(number, row) for number, row in enumerate(values[1:], 2) if oid_index is not None and oid_index < len(row) and row[oid_index].strip() == order_id]
    if not matching:
        raise ValueError("找不到這筆訂單")
    date_index, customer_index, term_index = find_header_index(headers, "date"), find_header_index(headers, "customer_name"), find_header_index(headers, "term")
    original_date = matching[0][1][date_index] if date_index is not None and date_index < len(matching[0][1]) else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    customer_name = matching[0][1][customer_index] if customer_index is not None and customer_index < len(matching[0][1]) else ""
    term = matching[0][1][term_index] if term_index is not None and term_index < len(matching[0][1]) else ""
    valid, names, total = order_summary(data.get("items", []))
    if not valid:
        raise ValueError("訂單至少要保留一項商品")
    for number, _ in reversed(matching):
        detail.delete_rows(number)
    group = str(data.get("group", "")).strip()
    detail.append_rows([row_for_headers(headers, {"order_id": order_id, "date": original_date, "group": group, **item, "customer_name": customer_name, "term": term}) for item in valid])
    master = sheet_cache.worksheet(MASTER_SHEET)
    master_headers = ensure_header(master, "customer_name")
    master_headers = ensure_header(master, "term")
    master_values, master_oid = master.get_all_values(), find_header_index(master_headers, "order_id")
    row = row_for_headers(master_headers, {"order_id": order_id, "date": original_date, "group": group, "items": names, "name": names, "total": total, "customer_name": customer_name, "term": term})
    target = next((n for n, old in enumerate(master_values[1:], 2) if master_oid is not None and master_oid < len(old) and old[master_oid].strip() == order_id), None)
    if target:
        master.update(f"A{target}:{gspread.utils.rowcol_to_a1(target, len(master_headers)).split(str(target))[0]}{target}", [row])
    else:
        master.append_row(row)
    return {"order_id": order_id, "date": original_date, "group": group, "customer_name": customer_name, "term": term or "legacy", "items": valid, "total": total}
