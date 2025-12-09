# -*- coding: utf-8 -*-
"""
BOT TELEGRAM FULL:
- MySQL (XAMPP)
- VietQR Payment + Webhook
- QR Payment (ảnh QR thay vì link)
- ngrok để public webhook
- Admin:
    + /myid lấy ID
    + /addadmin <id> cấp quyền
    + /listadmins xem danh sách
    + /admin_stock xem tồn kho
    + gửi file TXT + caption /uploadcodes <product_key> để nạp mã
- User:
    + /start → chọn gói
    + nhập số lượng
    + nhận QR thanh toán VietQR
    + thanh toán xong → bot tự gửi mã
"""
import re
import time
import json
import traceback
import threading
import io
import os
import qrcode

import requests
from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import mysql.connector


# =========================
# 1. CONFIG
# =========================

# TODO: 1 — TELEGRAM BOT TOKEN (lấy ở BotFather)
TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN",
    "8095563406:AAGc5o98VbvchcFN4ce6U_6qcmyczdpQaD0",
)

# TODO: 2 — ID của bạn (SUPER ADMIN). Lấy bằng /myid
SUPER_ADMINS = [7839568848]   # sửa lại khi biết ID của bạn

# MySQL XAMPP
DB_CONFIG = {
    "host": "localhost",
    "user": "h50d75d929_shop_bot",
    "password": "ErBQbFkYeyfle6xNpFje",
    "database": "h50d75d929_shop_bot",
    "charset": "utf8mb4",
}


# TODO: 3 — VietQR info (ưu tiên set bằng biến môi trường khi deploy thật)
VIETQR_ACCOUNT_NAME = "TRUONG MINH LOC"
VIETQR_ACCOUNT_NO = "0336797171"
VIETQR_BANK_BIN = "970422"      # MB Bank
VIETQR_TEMPLATE = "compact2"

# Webhook + domain (ngrok)
WEBHOOK_DOMAIN = os.getenv(
    "WEBHOOK_DOMAIN",
    "https://verona-violative-searingly.ngrok-free.dev",
)
WEBHOOK_RETURN = f"{WEBHOOK_DOMAIN}/vietqr-return"
WEBHOOK_IPN = f"{WEBHOOK_DOMAIN}/autobank-webhook"  # AUTOBANK IPN

# AUTOBANK token (gửi về email)
AUTOBANK_TOKEN = os.getenv(
    "AUTOBANK_TOKEN",
    "4241e9a8-4230-4c6c-b503-de6312b63a2b",
)

# URL API lịch sử giao dịch của AutoBank – em cần thay bằng URL ĐÚNG trong tài liệu
AUTOBANK_HISTORY_URL = "https://autobank.dev/apiv2/autobank/history"
ORDER_EXPIRY_SECONDS = 300  # 5 minutes timeout


# Danh sách sản phẩm
PRODUCTS = {
    "capcut_21d": {"name": "CAPCUT PRO TEAM 21D", "price": 15000},
    "capcut_28d": {"name": "CAPCUT PRO TEAM 28D", "price": 20000},
    "capcut_35d": {"name": "CAPCUT PRO TEAM 35D", "price": 25000},
    "capcut_42d": {"name": "CAPCUT PRO TEAM 42D", "price": 30000},
    "code_gpt":   {"name": "CODE GPT",            "price": 8000},
}

user_states: dict[int, dict] = {}
processed_tx_ids: set[str] = set()


# =========================
# 2. MYSQL FUNCTIONS
# =========================

def get_conn():
    return mysql.connector.connect(**DB_CONFIG)


def get_stock(product_key: str) -> int:
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT COUNT(*) AS c FROM codes WHERE product_key=%s AND used=0",
        (product_key,),
    )
    row = cur.fetchone()
    conn.close()
    return row["c"] if row else 0


def add_codes(product_key: str, codes: list[str]) -> int:
    codes = [c.strip() for c in codes if c.strip()]
    if not codes:
        return 0
    conn = get_conn()
    cur = conn.cursor()
    rows = [(product_key, c) for c in codes]
    cur.executemany(
        "INSERT INTO codes (product_key, code, used) VALUES (%s,%s,0)",
        rows,
    )
    conn.commit()
    added = cur.rowcount
    conn.close()
    return added


def get_unused_codes(product_key: str, qty: int) -> list[str]:
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT id, code FROM codes WHERE product_key=%s AND used=0 LIMIT %s",
        (product_key, qty),
    )
    rows = cur.fetchall()
    if not rows:
        conn.close()
        return []

    ids = [r["id"] for r in rows]
    codes = [r["code"] for r in rows]

    placeholder = ",".join(["%s"] * len(ids))
    cur.execute(
        f"UPDATE codes SET used=1 WHERE id IN ({placeholder})",
        tuple(ids),
    )
    conn.commit()
    conn.close()
    return codes


def save_order(order_id: str, telegram_user_id: int,
               product_key: str, qty: int, amount: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO orders(order_id, telegram_user_id, product_key, qty, amount, paid, created_at)
        VALUES (%s,%s,%s,%s,%s,0,%s)
        """,
        (order_id, telegram_user_id, product_key, qty, amount, int(time.time())),
    )
    conn.commit()
    conn.close()


def mark_order_paid(order_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE orders SET paid=1 WHERE order_id=%s", (order_id,))
    conn.commit()
    conn.close()


def get_order(order_id: str):
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM orders WHERE order_id=%s", (order_id,))
    row = cur.fetchone()
    conn.close()
    return row


def add_admin_db(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT IGNORE INTO admins (user_id) VALUES (%s)", (user_id,))
    conn.commit()
    conn.close()


def delete_admin_db(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM admins WHERE user_id=%s", (user_id,))
    conn.commit()
    conn.close()


def list_admins_db() -> list[int]:
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT user_id FROM admins")
    rows = cur.fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def is_super_admin(user_id: int) -> bool:
    return user_id in SUPER_ADMINS


def is_admin(user_id: int) -> bool:
    if user_id in SUPER_ADMINS:
        return True
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT 1 FROM admins WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None


# =========================
# 3. VIETQR PAYMENT + WEBHOOK
# =========================

def create_vietqr_payment(order_id: str, amount: int) -> str:
    """
    Tạo URL QR VietQR (img.vietqr.io) với nội dung order_id để đối soát.
    """
    params = {
        "amount": amount,
        "addInfo": order_id,
        "accountName": VIETQR_ACCOUNT_NAME,
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    url = (
        f"https://img.vietqr.io/image/"
        f"{VIETQR_BANK_BIN}-{VIETQR_ACCOUNT_NO}-{VIETQR_TEMPLATE}.png?{query}"
    )
    return url


def find_order_id_in_text(description: str) -> str | None:
    """
    Tìm order_id trong nội dung chuyển khoản.
    - Ưu tiên nhóm số dài ≥ 6 (order_id do bot tạo)
    - Thử từ cuối về đầu; nếu trùng đơn trong DB thì chọn luôn
    """
    if not description:
        return None

    groups = re.findall(r"\d{6,}", description)
    if not groups:
        groups = re.findall(r"\d{4,}", description)
    if not groups:
        return None

    # Thử từ cuối về đầu để bám sát format ngân hàng (mã sau cùng)
    for g in reversed(groups):
        if get_order(g):
            return g
    return groups[-1]



def verify_autobank_webhook(req) -> tuple[bool, str, int, int]:
    """
    AUTOBANK webhook dự kiến gửi JSON ví dụ:
    {
      "token": "<token>",
      "amount": 15000,
      "description": "ORD123...",
      "trans_id": "abc",
      "paid_at": 1710000000
    }
    - Xác thực token trùng AUTOBANK_TOKEN
    - Lấy order_id từ description (lọc số)
    """
    data = req.get_json(force=True, silent=True) or {}
    token = data.get("token") or req.headers.get("token")
    if token != AUTOBANK_TOKEN:
        return False, "", 0, 0

    desc = str(data.get("description", "") or "")
    order_id = find_order_id_in_text(desc)
    try:
        amount = int(data.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    try:
        paid_at = int(data.get("paid_at", time.time()))
    except (TypeError, ValueError):
        paid_at = int(time.time())

    if not order_id or amount <= 0:
        return False, "", 0, 0
    return True, order_id, amount, paid_at


def notify_order_timeout(order_id: str, telegram_uid: int):
    """After expiry window, if order still unpaid, notify user that it expired."""
    time.sleep(ORDER_EXPIRY_SECONDS)
    order = get_order(order_id)
    if order and order.get("paid") == 0:
        msg = (
            "⏰ Đơn hàng đã hết hạn thanh toán (quá 5 phút).\n"
            f"Mã đơn: {order_id}\n"
            "Nếu đã thanh toán sau thời gian này, liên hệ admin để hỗ trợ."
        )
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": telegram_uid, "text": msg},
        )


def poll_autobank_loop():
    """
    Vòng lặp gọi API AutoBank liên tục để lấy giao dịch mới.
    Không cần webhook.
    """
    while True:
        try:
            # DÙNG POST ĐÚNG THEO TÀI LIỆU AUTOBANK
            resp = requests.post(
                AUTOBANK_HISTORY_URL,
                json={"token": AUTOBANK_TOKEN},
                timeout=10,
            )
            data = resp.json()
            print("AUTOBANK history raw:", data)   # <== IN RA ĐỂ KIỂM TRA

            tx_list = data.get("data") or []


            for tx in tx_list:
                # --- TUỲ TÀI LIỆU AUTOBANK, sửa key cho đúng ---
                # Ví dụ: tx_id có thể là "transactionID", "tranId" hoặc "id"
                tx_id = str(
                    tx.get("id")
                    or tx.get("tranId")
                    or tx.get("trans_id")
                    or tx.get("transactionID")
                )

                # Nếu đã xử lý giao dịch này rồi -> bỏ qua
                if tx_id in processed_tx_ids:
                    continue

                processed_tx_ids.add(tx_id)

                # Số tiền
                try:
                    amount = int(tx.get("amount", 0))
                except (TypeError, ValueError):
                    amount = 0

                # Nội dung chuyển khoản
                desc = str(tx.get("description", "") or tx.get("content", "") or "")

                # Lấy order_id từ nội dung (lọc số) ưu tiên match DB để tránh nhầm
                order_id = find_order_id_in_text(desc)
                if not order_id:
                    continue

                order = get_order(order_id)
                if not order:
                    # Không phải đơn của bot mình
                    continue

                # Nếu đơn đã thanh toán rồi -> bỏ qua
                if order["paid"] == 1:
                    continue

                # (tuỳ bạn) có thể kiểm tra số tiền khớp
                if amount < int(order["amount"]):
                    # Thiếu tiền, có thể nhắn admin kiểm tra
                    continue

                # Đánh dấu đã thanh toán
                mark_order_paid(order_id)

                product_key = order["product_key"]
                qty = order["qty"]
                telegram_uid = order["telegram_user_id"]

                codes = get_unused_codes(product_key, qty)

                if len(codes) < qty:
                    msg = (
                        f"⚠️ Thanh toán OK nhưng thiếu mã.\n"
                        f"Đơn: {order_id}"
                    )
                else:
                    msg = (
                        f"🎉 Thanh toán thành công!\n"
                        f"Đơn: {order_id}\n"
                        f"Số tiền: {amount:,}đ\n\n"
                        f"✨ Mã của bạn:\n" +
                        "\n".join(f"- {c}" for c in codes)
                    )

                # Gửi code cho khách
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": telegram_uid, "text": msg},
                )

        except Exception as e:
            print("Lỗi khi gọi AutoBank:", e)
            traceback.print_exc()

        # Nghỉ 5 giây rồi gọi lại
        time.sleep(5)


# =========================
# 4. TELEGRAM BOT
# =========================

def build_product_keyboard():
    keyboard = []
    for key, p in PRODUCTS.items():
        stock = get_stock(key)
        text = f"{p['name']} — {p['price']:,}đ (còn {stock})"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"buy:{key}")])
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Xin chào! 👋\n"
        "Đây là bot bán hàng tự động của Lộc. Liên hệ admin @loktruong nếu có vấn đề khi giao dịch!\n"
        "Để bắt đầu giao dịch, vui lòng gõ /menu để xem danh sách sản phẩm.",
    )
    await update.message.reply_text(
        "👉 CHỌN SẢN PHẨM:",
        reply_markup=build_product_keyboard(),
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    await update.message.reply_text(f"ID của bạn: {uid}")


async def addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not is_super_admin(uid):
        await update.message.reply_text("⛔ Chỉ SUPER ADMIN mới được cấp quyền.")
        return

    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Cách dùng: /addadmin <user_id>")
        return

    new_admin = int(parts[1])
    add_admin_db(new_admin)
    await update.message.reply_text(f"✅ Đã thêm admin: {new_admin}")


async def listadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Bạn không có quyền.")
        return

    admins = list_admins_db()
    msg = "👑 SUPER ADMINS:\n"
    for x in SUPER_ADMINS:
        msg += f"- {x}\n"
    msg += "\n🧑‍💻 ADMINS:\n"
    if admins:
        for x in admins:
            msg += f"- {x}\n"
    else:
        msg += "- (chưa có ai)"
    await update.message.reply_text(msg)


async def deladmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not is_super_admin(uid):
        await update.message.reply_text("⛔ Chỉ SUPER ADMIN mới được gỡ quyền.")
        return

    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Cách dùng: /deladmin <user_id>")
        return

    target = int(parts[1])
    delete_admin_db(target)
    await update.message.reply_text(f"✅ Đã gỡ quyền admin: {target}")


async def uploadcodes_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Gửi file TXT + caption:\n`/uploadcodes <product_key>`",
        parse_mode="Markdown",
    )


async def test_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đã chuyển sang VietQR. Dùng /start hoặc /menu để mua.")


async def admin_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Không có quyền admin.")
        return
    msg = "📦 Tồn kho:\n"
    for key, p in PRODUCTS.items():
        msg += f"- {key}: {get_stock(key)} mã\n"
    await update.message.reply_text(msg)


async def handle_buy_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, product_key = query.data.split(":")
    product = PRODUCTS.get(product_key)

    if not product:
        await query.message.reply_text("❌ Sản phẩm không tồn tại.")
        return

    stock = get_stock(product_key)
    if stock <= 0:
        await query.message.reply_text("⚠️ Sản phẩm này đã hết mã.")
        return

    uid = query.from_user.id
    user_states[uid] = {"step": "waiting_qty", "product_key": product_key}

    await query.message.reply_text(
        f"Bạn chọn *{product['name']}*\n"
        f"Giá: *{product['price']:,}đ*\n"
        f"Tồn kho: *{stock} mã*\n\n"
        f"Nhập số lượng muốn mua:",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    # Admin xem kho (fallback nếu bot chưa bắt CommandHandler)
    if text.startswith("/admin_stock"):
        if not is_admin(uid):
            await update.message.reply_text("⛔ Không có quyền admin.")
            return
        msg = "📦 Tồn kho:\n"
        for key, p in PRODUCTS.items():
            msg += f"- {key}: {get_stock(key)} mã\n"
        await update.message.reply_text(msg)
        return

    if uid not in user_states:
        await update.message.reply_text("Gõ /start để mua hàng.")
        return

    state = user_states[uid]

    if state["step"] != "waiting_qty":
        await update.message.reply_text("Gõ /start để mua hàng.")
        return

    if not text.isdigit():
        await update.message.reply_text("⚠️ Nhập số lượng hợp lệ.")
        return

    qty = int(text)
    product_key = state["product_key"]
    product = PRODUCTS[product_key]

    stock = get_stock(product_key)
    if qty < 1 or qty > stock:
        await update.message.reply_text(f"Số lượng phải từ 1 → {stock}.")
        return

    amount = product["price"] * qty
    # Tạo order_id ~10 chữ số từ timestamp + một phần user_id để hạn chế trùng
    base = int(time.time())
    order_id = str(base + (uid % 1000))
    order_info = f"Mua {product['name']} x{qty}"

    try:
        save_order(order_id, uid, product_key, qty, amount)
        pay_url = create_vietqr_payment(order_id, amount)
        threading.Thread(
            target=notify_order_timeout,
            args=(order_id, uid),
            daemon=True,
        ).start()
    except Exception as e:  # catch DB errors to avoid silent failures
        print("Order creation error:", e)
        traceback.print_exc()
        await update.message.reply_text(
            "❌ Lỗi hệ thống khi tạo đơn. Thử lại sau.\n"
            "Nếu vẫn lỗi, kiểm tra console/log để xem chi tiết."
        )
        return

    if not pay_url:
        await update.message.reply_text("❌ Tạo QR thanh toán thất bại.")
        return

    user_states.pop(uid, None)

    # pay_url là LINK HÌNH PNG VietQR từ img.vietqr.io
    # Không cần tạo QR mới nữa

    await update.message.reply_photo(
        photo=pay_url,   # Gửi trực tiếp link ảnh
        caption=(
            f"■ Quét QR để thanh toán {amount:,}đ\n"
            f"Nội dung chuyển khoản: {order_id}\n"
            f"(Thanh toán VietQR - giữ nguyên nội dung để đối soát)"
        ),
        parse_mode=None,
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = msg.from_user.id

    if not is_admin(uid):
        await msg.reply_text("⛔ Bạn không có quyền upload mã.")
        return

    caption = msg.caption or ""
    parts = caption.split()

    if len(parts) != 2 or parts[0] != "/uploadcodes":
        await msg.reply_text(
            "Sai cú pháp.\nGửi file TXT + caption:\n`/uploadcodes <product_key>`",
            parse_mode="Markdown",
        )
        return

    product_key = parts[1]
    if product_key not in PRODUCTS:
        await msg.reply_text("Product key không hợp lệ.")
        return

    file = await msg.document.get_file()
    tmp = "upload_codes.txt"
    await file.download_to_drive(tmp)

    with open(tmp, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    added = add_codes(product_key, lines)
    stock = get_stock(product_key)

    await msg.reply_text(
        f"✅ Đã nạp {added} mã cho {product_key}.\n"
        f"Tồn kho mới: {stock} mã.",
    )


# =========================
# 5. FLASK – VIETQR WEBHOOK
# =========================

app = Flask(__name__)


@app.route("/autobank-webhook", methods=["POST"])
def autobank_webhook():
    ok, order_id, amount, paid_at = verify_autobank_webhook(request)
    print("AUTOBANK webhook:", {"ok": ok, "order_id": order_id, "amount": amount})

    if not ok:
        print("❌ Sai token hoặc payload webhook")
        return jsonify({"message": "invalid webhook"}), 400

    order = get_order(order_id)
    if not order:
        return jsonify({"message": "order not found"}), 404

    # Reject late payments beyond expiry window
    if int(time.time()) - int(order.get("created_at", 0)) > ORDER_EXPIRY_SECONDS:
        msg = (
            "⚠️ Thanh toán nhận được nhưng đơn đã hết hạn (quá 5 phút).\n"
            f"Mã đơn: {order_id}\n"
            "Liên hệ admin để kiểm tra giao dịch."
        )
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": order["telegram_user_id"], "text": msg},
        )
        return jsonify({"message": "order expired"}), 200

    if order and order["paid"] == 0:
        mark_order_paid(order_id)

        product_key = order["product_key"]
        qty = order["qty"]
        telegram_uid = order["telegram_user_id"]

        codes = get_unused_codes(product_key, qty)

        if len(codes) < qty:
            msg = (
                f"⚠️ Thanh toán OK nhưng thiếu mã.\n"
                f"Đơn: {order_id}"
            )
        else:
            msg = (
                f"🎉 Thanh toán thành công!\n"
                f"Đơn: {order_id}\n"
                f"Số tiền: {amount:,}đ\n\n"
                f"✨ Mã của bạn:\n" +
                "\n".join(f"- {c}" for c in codes)
            )

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": telegram_uid, "text": msg},
        )

    return jsonify({"message": "received"}), 200


@app.route("/vietqr-return", methods=["GET"])
def vietqr_return():
    return "Thanh toán VietQR đã xử lý. Bạn có thể quay lại Telegram.", 200


# =========================
# 6. RUN BOTH: TELEGRAM + FLASK
# =========================
# ===============================
# TELEGRAM BOT RUN FUNCTION
# ===============================
def run_bot():
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Command handlers
    app_tg.add_handler(CommandHandler(["start", "menu", "buy"], start))
    app_tg.add_handler(CommandHandler("myid", myid))
    app_tg.add_handler(CommandHandler("addadmin", addadmin))
    app_tg.add_handler(CommandHandler("deladmin", deladmin))
    app_tg.add_handler(CommandHandler("listadmins", listadmins))
    app_tg.add_handler(CommandHandler("admin_stock", admin_stock))
    app_tg.add_handler(CommandHandler("uploadcodes", uploadcodes_help))
    app_tg.add_handler(CommandHandler("testmomo", test_payment))

    # Callback handler
    app_tg.add_handler(CallbackQueryHandler(handle_buy_button, pattern="^buy:"))

    # Message handlers
    app_tg.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # CHẠY BOT (SYNC)
    app_tg.run_polling()



# ===============================
# FLASK SERVER
# ===============================
def run_flask():
    app.run(host="0.0.0.0", port=8000)


if __name__ == "__main__":
    import threading

    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=poll_autobank_loop, daemon=True).start()

    run_bot()





