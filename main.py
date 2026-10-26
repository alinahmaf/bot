import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from threading import Thread
from flask import Flask

TOKEN = os.environ.get("BOT_TOKEN")
API_URL = f"https://api.telegram.org/bot{TOKEN}/"
PRICE_WA = 5
PRICE_TG = 10
RESERVATION_TTL = 60 * 60
DB_PATH = Path(".data/telegram-bot.sqlite3")
HEALTH_PORT = int(os.environ.get("PORT", "10000"))

BTN_BUY = "💰 اشتري رقم"
BTN_WA = f"📱 واتساب - {PRICE_WA} ⭐"
BTN_TG = f"✈️ تيليجرام - {PRICE_TG} ⭐"
BTN_BACK = "⬅️ رجوع"

app = Flask("telegram_bot")
@app.get("/")
def home(): return "Bot is alive!"

def run_health_server():
    app.run(host="0.0.0.0", port=HEALTH_PORT, use_reloader=False, threaded=True)
def keep_alive():
    Thread(target=run_health_server, daemon=True).start()

class TelegramAPIError(RuntimeError): pass
def api_call(method, payload=None, timeout=30):
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(API_URL + method, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise TelegramAPIError(str(e))
    if not res.get("ok"): raise TelegramAPIError(res.get("description"))
    return res.get("result")

def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def initialize_database():
    with database() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS inventory (number TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'available', payload TEXT UNIQUE, reserved_at REAL, type TEXT DEFAULT 'whatsapp');
            CREATE TABLE IF NOT EXISTS orders (payload TEXT PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, type TEXT, amount INTEGER);
            CREATE TABLE IF NOT EXISTS payments (charge_id TEXT PRIMARY KEY, payload TEXT NOT NULL, number TEXT NOT NULL, user_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        conn.execute("INSERT OR IGNORE INTO inventory(number, type) VALUES (?, 'whatsapp')", ("+963944111111",))
        conn.execute("INSERT OR IGNORE INTO inventory(number, type) VALUES (?, 'telegram')", ("+963944222222",))
        conn.execute("INSERT OR IGNORE INTO bot_state(key, value) VALUES ('last_update_id', '0')")

def available_count(conn, typ):
    return conn.execute("SELECT COUNT(*) FROM inventory WHERE status='available' AND type=?", (typ,)).fetchone()[0]

def release_expired(conn):
    cutoff = time.time() - RESERVATION_TTL
    for row in conn.execute("SELECT payload FROM inventory WHERE status='reserved' AND reserved_at < ?", (cutoff,)).fetchall():
        conn.execute("UPDATE inventory SET status='available', payload=NULL, reserved_at=NULL WHERE payload=?", (row["payload"],))
        conn.execute("UPDATE orders SET status='expired' WHERE payload=?", (row["payload"],))

def send_message(chat_id, text, markup=None):
    p = {"chat_id": chat_id, "text": text}
    if markup: p["reply_markup"] = markup
    api_call("sendMessage", p)

def send_invoice(chat_id, user_id, num_type):
    payload = uuid.uuid4().hex
    amount = PRICE_WA if num_type == "whatsapp" else PRICE_TG
    title = "رقم واتساب" if num_type == "whatsapp" else "رقم تيليجرام"
    with database() as conn:
        conn.execute("INSERT INTO orders(payload, user_id, status, created_at, type, amount) VALUES (?, ?, 'created', ?, ?, ?)", (payload, user_id, time.time(), num_type, amount))
    api_call("sendInvoice", {"chat_id": chat_id, "title": title, "description": f"تسليم فوري - {title}", "payload": payload, "provider_token": "", "currency": "XTR", "prices": [{"label": title, "amount": amount}]})

def answer_pre_checkout(q):
    qid = q["id"]; payload = q.get("invoice_payload",""); uid = q.get("from",{}).get("id")
    ok = False
    if q.get("currency")=="XTR" and uid:
        with database() as conn:
            conn.execute("BEGIN IMMEDIATE"); release_expired(conn)
            order = conn.execute("SELECT user_id, status, type, amount FROM orders WHERE payload=?", (payload,)).fetchone()
            if order and order["user_id"]==uid and q.get("total_amount")==order["amount"]:
                if order["status"]=="reserved":
                    ok = conn.execute("SELECT 1 FROM inventory WHERE payload=?", (payload,)).fetchone() is not None
                elif order["status"]=="created":
                    stock = conn.execute("SELECT number FROM inventory WHERE status='available' AND type=? LIMIT 1", (order["type"],)).fetchone()
                    if stock:
                        conn.execute("UPDATE inventory SET status='reserved', payload=?, reserved_at=? WHERE number=?", (payload, time.time(), stock["number"]))
                        conn.execute("UPDATE orders SET status='reserved' WHERE payload=?", (payload,))
                        ok=True
    ans = {"pre_checkout_query_id": qid, "ok": ok}
    if not ok: ans["error_message"]="الرقم غير متوفر او السعر خطأ"
    api_call("answerPreCheckoutQuery", ans)

def process_payment(msg):
    pay = msg["successful_payment"]; uid = msg.get("from",{}).get("id"); chat_id = msg.get("chat",{}).get("id")
    payload = pay.get("invoice_payload",""); charge_id = pay.get("telegram_payment_charge_id",""); number=None
    with database() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prev = conn.execute("SELECT number FROM payments WHERE charge_id=?", (charge_id,)).fetchone() if charge_id else None
        if prev: number=prev["number"]
        elif uid and charge_id and pay.get("currency")=="XTR":
            order = conn.execute("SELECT user_id, status, amount FROM orders WHERE payload=?", (payload,)).fetchone()
            if order and order["user_id"]==uid and pay.get("total_amount")==order["amount"]:
                stock = conn.execute("SELECT number FROM inventory WHERE payload=?", (payload,)).fetchone()
                if not stock: stock = conn.execute("SELECT number FROM inventory WHERE status='available' LIMIT 1").fetchone()
                if stock:
                    number=stock["number"]
                    conn.execute("UPDATE inventory SET status='sold', payload=?, reserved_at=NULL WHERE number=?", (payload, number))
                    conn.execute("UPDATE orders SET status='paid' WHERE payload=?", (payload,))
                    conn.execute("INSERT INTO payments(charge_id, payload, number, user_id) VALUES (?,?,?,?)", (charge_id, payload, number, uid))
    if number: send_message(chat_id, f"✅ تم الدفع!\n\nرقمك:\n{number}", {"keyboard": [[{"text": BTN_BUY}]], "resize_keyboard": True})
    else:
        if uid and charge_id:
            api_call("refundStarPayment", {"user_id": uid, "telegram_payment_charge_id": charge_id})
            if chat_id: send_message(chat_id, "تعذر التسليم وتم ارجاع النجوم.")

def handle_update(upd):
    if "pre_checkout_query" in upd: answer_pre_checkout(upd["pre_checkout_query"]); return
    msg = upd.get("message",{}); chat_id = msg.get("chat",{}).get("id"); uid = msg.get("from",{}).get("id")
    if not chat_id: return
    if "successful_payment" in msg: process_payment(msg); return
    text = msg.get("text","")
    if text in ("/start","/help"):
        with database() as c: wa = available_count(c,'whatsapp'); tg = available_count(c,'telegram')
        send_message(chat_id, f"📞 بوت بيع الأرقام\n\n{BTN_WA} - متوفر: {wa}\n{BTN_TG} - متوفر: {tg}\n\nاضغط اشتري رقم للبدء", {"keyboard": [[{"text": BTN_BUY}]], "resize_keyboard": True})
    elif text == BTN_BUY:
        send_message(chat_id, "اختار نوع الرقم:", {"keyboard": [[{"text": BTN_WA}, {"text": BTN_TG}], [{"text": BTN_BACK}]], "resize_keyboard": True})
    elif text == BTN_WA and uid:
        send_invoice(chat_id, uid, "whatsapp")
    elif text == BTN_TG and uid:
        send_invoice(chat_id, uid, "telegram")
    elif text == BTN_BACK:
        send_message(chat_id, "القائمة الرئيسية", {"keyboard": [[{"text": BTN_BUY}]], "resize_keyboard": True})

def run():
    initialize_database(); keep_alive()
    with database() as c: row=c.execute("SELECT value FROM bot_state WHERE key='last_update_id'").fetchone(); offset=int(row["value"]) if row else 0
    while True:
        try:
            ups = api_call("getUpdates", {"offset": offset, "timeout": 20, "allowed_updates": ["message","pre_checkout_query"]}, timeout=30)
            for u in ups:
                handle_update(u); offset=u["update_id"]+1
                with database() as c: c.execute("UPDATE bot_state SET value=? WHERE key='last_update_id'", (str(offset),))
        except Exception as e:
            logging.error(e); time.sleep(3)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO); run()
