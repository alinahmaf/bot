
import json, logging, os, sqlite3, time, urllib.request, uuid
from pathlib import Path
from threading import Thread
from flask import Flask
TOKEN=os.environ.get("BOT_TOKEN")
API_URL=f"https://api.telegram.org/bot{TOKEN}/"
DB_PATH=Path(".data/telegram-bot.sqlite3")
HEALTH_PORT=int(os.environ.get("PORT","8000"))
app=Flask("bot")
@app.get("/")
def home(): return "Bot is alive!"
def run_health_server(): app.run(host="0.0.0.0", port=HEALTH_PORT, use_reloader=False)
def keep_alive(): Thread(target=run_health_server, daemon=True).start()
def api_call(method, payload=None, timeout=30):
 import json as js; body=js.dumps(payload or {}).encode("utf-8"); req=urllib.request.Request(API_URL+method, data=body, headers={"Content-Type":"application/json"}, method="POST")
 with urllib.request.urlopen(req, timeout=timeout) as r: result=js.loads(r.read().decode("utf-8"))
 return result.get("result")
def database():
 DB_PATH.parent.mkdir(parents=True, exist_ok=True); c=sqlite3.connect(DB_PATH, timeout=30); c.row_factory=sqlite3.Row; return c
def initialize_database():
 with database() as conn:
  conn.executescript("CREATE TABLE IF NOT EXISTS inventory (number TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'available', payload TEXT UNIQUE, reserved_at REAL);CREATE TABLE IF NOT EXISTS orders (payload TEXT PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);CREATE TABLE IF NOT EXISTS payments (charge_id TEXT PRIMARY KEY, payload TEXT NOT NULL, number TEXT NOT NULL, user_id INTEGER NOT NULL);CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
  conn.executemany("INSERT OR IGNORE INTO inventory(number) VALUES (?)", [(n,) for n in ["+963944111111","+963944222222"]])
  conn.execute("INSERT OR IGNORE INTO bot_state(key, value) VALUES ('last_update_id', '0')")
def send_message(chat_id, text, reply_markup=None):
 p={"chat_id":chat_id, "text":text}; 
 if reply_markup: p["reply_markup"]=reply_markup
 api_call("sendMessage", p)
def run():
 initialize_database(); keep_alive()
 with database() as conn: row=conn.execute("SELECT value FROM bot_state WHERE key='last_update_id'").fetchone(); offset=int(row["value"]) if row else 0
 while True:
  try:
   updates=api_call("getUpdates", {"offset":offset, "timeout":20, "allowed_updates":["message","pre_checkout_query"]}, timeout=30)
   for u in updates:
    if "pre_checkout_query" in u:
     q=u["pre_checkout_query"]; qid=q["id"]; payload=q.get("invoice_payload",""); uid=q.get("from",{}).get("id"); accepted=False
     with database() as conn:
      conn.execute("BEGIN IMMEDIATE"); order=conn.execute("SELECT user_id, status FROM orders WHERE payload=?", (payload,)).fetchone()
      if order and order["user_id"]==uid:
       if order["status"]=="reserved": accepted=conn.execute("SELECT 1 FROM inventory WHERE payload=? AND status='reserved'", (payload,)).fetchone() is not None
       elif order["status"]=="created":
        stock=conn.execute("SELECT number FROM inventory WHERE status='available' LIMIT 1").fetchone()
        if stock: conn.execute("UPDATE inventory SET status='reserved', payload=?, reserved_at=? WHERE number=? AND status='available'", (payload, time.time(), stock["number"])); conn.execute("UPDATE orders SET status='reserved' WHERE payload=?", (payload,)); accepted=True
     a={"pre_checkout_query_id":qid, "ok":accepted}
     if not accepted: a["error_message"]="غير متوفر"
     api_call("answerPreCheckoutQuery", a)
    else:
     m=u.get("message",{}); chat_id=m.get("chat",{}).get("id"); uid=m.get("from",{}).get("id")
     if not chat_id: offset=u["update_id"]+1; continue
     if "successful_payment" in m:
      pay=m["successful_payment"]; payload=pay.get("invoice_payload",""); charge=pay.get("telegram_payment_charge_id",""); number=None
      with database() as conn:
       conn.execute("BEGIN IMMEDIATE"); prev=conn.execute("SELECT number FROM payments WHERE charge_id=?", (charge,)).fetchone() if charge else None
       if prev: number=prev["number"]
       else:
        order=conn.execute("SELECT user_id, status FROM orders WHERE payload=?", (payload,)).fetchone()
        if order and order["user_id"]==uid:
         stock=conn.execute("SELECT number FROM inventory WHERE payload=? AND status='reserved'", (payload,)).fetchone()
         if not stock: stock=conn.execute("SELECT number FROM inventory WHERE status='available' LIMIT 1").fetchone()
         if stock: number=stock["number"]; conn.execute("UPDATE inventory SET status='sold', payload=?, reserved_at=NULL WHERE number=?", (payload, number)); conn.execute("UPDATE orders SET status='paid' WHERE payload=?", (payload,)); conn.execute("INSERT INTO payments(charge_id, payload, number, user_id) VALUES (?,?,?,?)", (charge, payload, number, uid))
      if number: send_message(chat_id, f"✅ تم الدفع!\nرقمك:\n{number}")
      else:
       if uid and charge: api_call("refundStarPayment", {"user_id":uid, "telegram_payment_charge_id":charge})
     else:
      text=m.get("text","")
      if text in ("/start","/help"): send_message(chat_id, "📞 بوت بيع الأرقام\nالسعر: 5 نجوم\nاضغط الزر للشراء", {"keyboard":[[{"text":"💰 اشتري رقم - 5 ⭐"}]], "resize_keyboard":True})
      elif text=="💰 اشتري رقم - 5 ⭐" and uid:
       payload=uuid.uuid4().hex
       with database() as conn: conn.execute("INSERT INTO orders(payload, user_id, status, created_at) VALUES (?,?, 'created', ?)", (payload, uid, time.time()))
       api_call("sendInvoice", {"chat_id":chat_id, "title":"شراء رقم", "description":"تسليم فوري", "payload":payload, "provider_token":"", "currency":"XTR", "prices":[{"label":"رقم","amount":5}]})
    offset=u["update_id"]+1
    with database() as conn: conn.execute("UPDATE bot_state SET value=? WHERE key='last_update_id'", (str(offset),))
  except Exception as e: time.sleep(3)
if __name__=="__main__": logging.basicConfig(level=logging.INFO); run()
  
