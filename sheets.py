import json
import os

import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# =========================
# 🔐 Google Auth
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

CREDS_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

if GOOGLE_CREDENTIALS_JSON:
    credentials = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS_JSON),
        scopes=SCOPES
    )
else:
    credentials = Credentials.from_service_account_file(
        CREDS_FILE,
        scopes=SCOPES
    )

client = gspread.authorize(credentials)

# =========================
# 📌 Spreadsheet ID
# =========================
SPREADSHEET_ID = os.environ.get(
    "SPREADSHEET_ID",
    "1m6ZQsxYavA37CW8inxYiZ-4ZJT1uV9VFm_UybQ6lRTY"
)

# 👉 預先開啟
sheet_cache = client.open_by_key(SPREADSHEET_ID)

EXPECTED_SHEETS = {
    "商品主表": [["id"], ["name"], ["type"], ["price"], ["active"]],
    "尺寸庫存表": [["product_id"], ["sku"], ["size"], ["price"], ["stock"]],
    "訂單明細": [["order_id"], ["date"], ["group"], ["sku"], ["product_name", "name"], ["size"], ["price"], ["qty"]],
    "訂單主表": [["order_id"], ["date"], ["group"], ["items", "name"], ["total"]]
}


# =========================
# 🧠 工具函式（🔥新加）
# =========================
def clean_key(r, *targets):
    for k in r.keys():
        for t in targets:
            if str(k).strip().lower() == str(t).strip().lower():
                return r[k]
    return ""


def safe_int(value):
    try:
        if value in [None, ""]:
            return 0
        return int(value)
    except:
        return 0


def find_header_index(headers, *targets):
    for index, header in enumerate(headers):
        for target in targets:
            if str(header).strip().lower() == str(target).strip().lower():
                return index
    return None


def normalize_header(value):
    return str(value).strip().lower()


def validate_sheet_schema():
    result = []

    for sheet_name, expected_groups in EXPECTED_SHEETS.items():
        try:
            sheet = sheet_cache.worksheet(sheet_name)
            headers = [normalize_header(header) for header in sheet.row_values(1)]
            missing = [
                " / ".join(group) for group in expected_groups
                if not any(normalize_header(header) in headers for header in group)
            ]

            result.append({
                "sheet": sheet_name,
                "ok": len(missing) == 0,
                "missing": missing,
                "headers": headers
            })
        except Exception as e:
            result.append({
                "sheet": sheet_name,
                "ok": False,
                "missing": [" / ".join(group) for group in expected_groups],
                "headers": [],
                "error": str(e)
            })

    return {
        "ok": all(item["ok"] for item in result),
        "sheets": result
    }


def build_order_row(order_id, order_date, group, item):
    return [
        order_id,
        order_date,
        group,
        str(item.get("sku", "")).strip(),
        str(item.get("name", item.get("product_name", ""))).strip(),
        str(item.get("size", "")).strip(),
        safe_int(item.get("price")),
        safe_int(item.get("qty"))
    ]


def order_summary(items):
    valid_items = [
        item for item in items
        if (
            str(item.get("sku", "")).strip()
            or str(item.get("name", item.get("product_name", ""))).strip()
        ) and safe_int(item.get("qty")) > 0
    ]
    total = sum(safe_int(item.get("price")) * safe_int(item.get("qty")) for item in valid_items)
    names = " / ".join([
        str(item.get("name", item.get("product_name", ""))).strip()
        for item in valid_items
        if str(item.get("name", item.get("product_name", ""))).strip()
    ])
    return valid_items, names, total


# =========================
# 📦 商品主表
# =========================
def get_products():
    sheet = sheet_cache.worksheet("商品主表")
    return sheet.get_all_records()


# =========================
# 📦 尺寸庫存表
# =========================
def get_variants():
    sheet = sheet_cache.worksheet("尺寸庫存表")
    return sheet.get_all_records()


# =========================
# 📦 商品 + 尺寸整合
# =========================
def get_product_grouped():

    products_sheet = sheet_cache.worksheet("商品主表")
    variants_sheet = sheet_cache.worksheet("尺寸庫存表")

    products = products_sheet.get_all_records()
    variants = variants_sheet.get_all_records()

    result = []

    for p in products:
        pid = str(p.get("id", "")).strip()
        ptype = str(p.get("type", "variant")).strip()

        # ===== simple 商品 =====
        if ptype == "simple":

            result.append({
                "id": pid,
                "name": p.get("name", ""),
                "active": p.get("active", "TRUE"),
                "type": "simple",
                "sizes": [
                    {
                        "sku": pid,
                        "size": "單一",
                        "price": safe_int(p.get("price")),
                        "stock": 9999
                    }
                ]
            })
            continue

        # ===== variant 商品 =====
        sizes = []

        for v in variants:
            if str(v.get("product_id","")).strip() == pid.strip():

                sizes.append({
                    "sku": str(v.get("sku", "")).strip(),
                    "size": str(v.get("size", "")).strip(),
                    "price": safe_int(v.get("price")),
                    "stock": safe_int(v.get("stock"))
                })

        result.append({
            "id": pid,
            "name": p.get("name", ""),
            "active": p.get("active", "TRUE"),
            "type": "variant",
            "sizes": sizes
        })

    return result


# =========================
# 📦 訂單寫入
# =========================
def save_order(order_id, data):

    detail_sheet = sheet_cache.worksheet("訂單明細")
    master_sheet = sheet_cache.worksheet("訂單主表")

    group = str(data.get("group", "")).strip()
    items = data.get("items", [])

    order_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    valid_items, names, total = order_summary(items)

    for item in valid_items:

        sku = str(item.get("sku", "")).strip()
        name = str(item.get("name", item.get("product_name", ""))).strip()

        detail_sheet.append_row([
            order_id,
            order_date,
            group,
            sku,
            name,
            str(item.get("size", "")).strip(),
            safe_int(item.get("price")),
            safe_int(item.get("qty"))
        ])

    # =========================
    # 🔥 寫入主表（新增）
    # =========================
    master_sheet.append_row([
        order_id,
        order_date,
        group,
        names,
        total
    ])


# =========================
# 📦 訂單總覽（🔥已修 size 問題）
# =========================
def get_orders():

    sheet = sheet_cache.worksheet("訂單明細")
    rows = sheet.get_all_records()

    orders = {}

    for r in rows:

        oid = str(clean_key(r, "order_id")).strip()
        if not oid:
            continue

        if oid not in orders:
            orders[oid] = {
                "order_id": oid,
                "date": clean_key(r, "date"),
                "group": clean_key(r, "group"),
                "items": [],
                "total": 0
            }

        # ===== 🔥 用 clean_key（完全修復）=====
        name = str(clean_key(r, "product_name", "name")).strip()
        size = str(clean_key(r, "size", "尺寸")).strip()
        sku = str(clean_key(r, "sku")).strip()

        price = safe_int(clean_key(r, "price", "價格"))
        qty = safe_int(clean_key(r, "qty", "數量"))

        if not name:
            continue

        orders[oid]["items"].append({
            "sku": sku,
            "name": name,
            "product_name": name,
            "size": size,
            "price": price,
            "qty": qty
        })

        orders[oid]["total"] += price * qty

    return list(orders.values())


# =========================
# ✏️ 修改訂單
# =========================
def update_order(order_id, data):

    detail_sheet = sheet_cache.worksheet("訂單明細")
    master_sheet = sheet_cache.worksheet("訂單主表")

    group = str(data.get("group", "")).strip()
    items = data.get("items", [])
    valid_items, names, total = order_summary(items)

    if not group:
        raise ValueError("缺少組別")

    if not valid_items:
        raise ValueError("訂單至少要有一個品項")

    detail_values = detail_sheet.get_all_values()
    if not detail_values:
        raise ValueError("找不到訂單明細表資料")

    detail_headers = detail_values[0]
    detail_order_index = find_header_index(detail_headers, "order_id")
    detail_date_index = find_header_index(detail_headers, "date")

    if detail_order_index is None:
        raise ValueError("訂單明細表缺少 order_id 欄位")

    rows_to_delete = []
    original_date = ""

    for row_number, row in enumerate(detail_values[1:], start=2):
        oid = row[detail_order_index].strip() if detail_order_index < len(row) else ""

        if oid != order_id:
            continue

        rows_to_delete.append(row_number)

        if detail_date_index is not None and detail_date_index < len(row) and not original_date:
            original_date = row[detail_date_index].strip()

    if not rows_to_delete:
        raise ValueError("找不到此訂單")

    order_date = original_date or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for row_number in sorted(rows_to_delete, reverse=True):
        detail_sheet.delete_rows(row_number)

    detail_sheet.append_rows([
        build_order_row(order_id, order_date, group, item)
        for item in valid_items
    ])

    master_values = master_sheet.get_all_values()

    if master_values:
        master_headers = master_values[0]
        master_order_index = find_header_index(master_headers, "order_id")
    else:
        master_order_index = None

    master_row = [order_id, order_date, group, names, total]
    updated_master = False

    if master_order_index is not None:
        for row_number, row in enumerate(master_values[1:], start=2):
            oid = row[master_order_index].strip() if master_order_index < len(row) else ""

            if oid == order_id:
                master_sheet.update(f"A{row_number}:E{row_number}", [master_row])
                updated_master = True
                break

    if not updated_master:
        master_sheet.append_row(master_row)

    return {
        "order_id": order_id,
        "date": order_date,
        "group": group,
        "items": [
            {
                "sku": str(item.get("sku", "")).strip(),
                "name": str(item.get("name", item.get("product_name", ""))).strip(),
                "product_name": str(item.get("name", item.get("product_name", ""))).strip(),
                "size": str(item.get("size", "")).strip(),
                "price": safe_int(item.get("price")),
                "qty": safe_int(item.get("qty"))
            }
            for item in valid_items
        ],
        "total": total
    }
