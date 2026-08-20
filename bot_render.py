import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from hmac import compare_digest

import requests
from flask import Flask, jsonify, request

# ============================================================
# TOOLXW TELEGRAM CONTROL SERVER - RENDER
# ============================================================
# Required Render environment variables:
# BOT_TOKEN=123456:ABC...
# ADMIN_IDS=123456789,987654321
# SERVER_SECRET=make-a-long-random-secret
# DB_PATH=/var/data/control.db      (recommended with a Render persistent disk)
# PORT is provided by Render automatically.

BOT_TOKEN = os.getenv("BOT_TOKEN", "8809444250:AAE1C2V5HyfoMrIfFRvVN9px4KCRJ3fP_ZM").strip()
SERVER_SECRET = os.getenv("SERVER_SECRET", "ToolxwRemote_2026_8f4LzP9mQ2vX7kR6").strip()
ADMIN_IDS = {
    x.strip() for x in os.getenv("ADMIN_IDS", "8801844480").split(",") if x.strip()
}
DB_PATH = os.getenv("DB_PATH", "control.db")
APP_NAME = "Toolxw"
PORT = int(os.getenv("PORT", "10000"))

app = Flask(__name__)
DB_LOCK = threading.Lock()


def db_conn():
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with DB_LOCK, db_conn() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app TEXT NOT NULL,
                user_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen TEXT,
                UNIQUE(app, user_id, device_id)
            )
            """
        )
        db.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(value, max_len=128):
    value = str(value or "").strip()
    return value[:max_len]


def admin_only(uid):
    return str(uid) in ADMIN_IDS


def authorized_api(req):
    incoming = req.headers.get("X-Server-Secret", "")
    return bool(SERVER_SECRET) and compare_digest(incoming, SERVER_SECRET)


def get_client(user_id, device_id):
    with DB_LOCK, db_conn() as db:
        return db.execute(
            "SELECT * FROM clients WHERE app=? AND user_id=? AND device_id=?",
            (APP_NAME, user_id, device_id),
        ).fetchone()


def check_client(user_id, device_id):
    user_id = normalize(user_id)
    device_id = normalize(device_id, 128)
    if not user_id or not device_id:
        return False, "Thiếu user_id hoặc device_id"

    t = now_iso()
    with DB_LOCK, db_conn() as db:
        # Device-specific lock/revoke has highest priority.
        row = db.execute(
            "SELECT * FROM clients WHERE app=? AND user_id=? AND device_id=?",
            (APP_NAME, user_id, device_id),
        ).fetchone()

        # User-level status has priority over device-specific status.
        user_row = db.execute(
            "SELECT * FROM clients WHERE app=? AND user_id=? AND device_id='*'",
            (APP_NAME, user_id),
        ).fetchone()

        active = user_row if user_row and user_row["status"] != "active" else (row or user_row)
        if active:
            db.execute(
                "UPDATE clients SET last_seen=?, updated_at=? WHERE id=?",
                (t, t, active["id"]),
            )
            db.commit()
            status = active["status"]
            if status == "active":
                return True, "Được phép chạy"
            if status == "locked":
                return False, "Thiết bị/tài khoản đang bị KHÓA"
            return False, "Thiết bị/tài khoản đã bị THU HỒI"

        db.execute(
            "INSERT INTO clients(app,user_id,device_id,status,note,created_at,updated_at,last_seen) VALUES(?,?,?,?,?,?,?,?)",
            (APP_NAME, user_id, device_id, "active", "auto-register", t, t, t),
        )
        db.commit()
        return True, "Đăng ký thiết bị thành công"


def set_status(target, status, device_id="*", note=""):
    target = normalize(target)
    device_id = normalize(device_id, 128) or "*"
    if not target:
        return False, "Thiếu user_id"
    t = now_iso()
    with DB_LOCK, db_conn() as db:
        row = db.execute(
            "SELECT id FROM clients WHERE app=? AND user_id=? AND device_id=?",
            (APP_NAME, target, device_id),
        ).fetchone()
        if row:
            db.execute(
                "UPDATE clients SET status=?, note=?, updated_at=? WHERE id=?",
                (status, note, t, row["id"]),
            )
        else:
            db.execute(
                "INSERT INTO clients(app,user_id,device_id,status,note,created_at,updated_at,last_seen) VALUES(?,?,?,?,?,?,?,?)",
                (APP_NAME, target, device_id, status, note, t, t, None),
            )
        db.commit()
    return True, f"{target} -> {status}"


def restore(target, device_id="*"):
    target = normalize(target)
    device_id = normalize(device_id, 128) or "*"
    with DB_LOCK, db_conn() as db:
        if device_id == "*":
            cur = db.execute(
                "UPDATE clients SET status='active', note='restored', updated_at=? WHERE app=? AND user_id=?",
                (now_iso(), APP_NAME, target),
            )
        else:
            cur = db.execute(
                "UPDATE clients SET status='active', note='restored', updated_at=? WHERE app=? AND user_id=? AND device_id=?",
                (now_iso(), APP_NAME, target, device_id),
            )
        db.commit()
    return cur.rowcount


def list_clients(limit=30):
    with DB_LOCK, db_conn() as db:
        return db.execute(
            "SELECT user_id,device_id,status,last_seen,note FROM clients WHERE app=? ORDER BY updated_at DESC LIMIT ?",
            (APP_NAME, int(limit)),
        ).fetchall()


def telegram_call(method, payload=None, timeout=35):
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN chưa được cấu hình"}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload or {}, timeout=timeout)
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


def tg_send(chat_id, text):
    telegram_call("sendMessage", {"chat_id": chat_id, "text": text})


def command_help():
    return (
        "🔐 TOOLXW CONTROL\n\n"
        "/lock USER_ID - khóa tài khoản\n"
        "/unlock USER_ID - mở khóa\n"
        "/revoke USER_ID - thu hồi\n"
        "/restore USER_ID - khôi phục\n"
        "/lockdev USER_ID DEVICE_ID - khóa 1 thiết bị\n"
        "/revokedev USER_ID DEVICE_ID - thu hồi 1 thiết bị\n"
        "/restoredev USER_ID DEVICE_ID - mở 1 thiết bị\n"
        "/status USER_ID - xem trạng thái\n"
        "/list - xem các client gần đây\n"
        "/help - trợ giúp"
    )


def handle_text(chat_id, from_id, text):
    if not admin_only(from_id):
        tg_send(chat_id, "⛔ Bạn không có quyền quản trị bot.")
        return

    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].split("@", 1)[0].lower()

    if cmd in ("/start", "/help"):
        tg_send(chat_id, command_help())
        return

    if cmd in ("/lock", "/unlock", "/revoke", "/restore"):
        if len(parts) < 2:
            tg_send(chat_id, f"Cú pháp: {cmd} USER_ID")
            return
        uid = parts[1]
        mapping = {"/lock": "locked", "/unlock": "active", "/revoke": "revoked", "/restore": "active"}
        if cmd == "/restore":
            n = restore(uid)
            tg_send(chat_id, f"✅ Đã khôi phục {uid}. Bản ghi cập nhật: {n}")
            return
        ok, msg = set_status(uid, mapping[cmd], note=f"Telegram {cmd}")
        tg_send(chat_id, ("✅ " if ok else "❌ ") + msg)
        return

    if cmd in ("/lockdev", "/revokedev", "/restoredev"):
        if len(parts) < 3:
            tg_send(chat_id, f"Cú pháp: {cmd} USER_ID DEVICE_ID")
            return
        uid, device = parts[1], parts[2]
        if cmd == "/restoredev":
            n = restore(uid, device)
            tg_send(chat_id, f"✅ Khôi phục device {device}: {n} bản ghi")
            return
        status = "locked" if cmd == "/lockdev" else "revoked"
        ok, msg = set_status(uid, status, device_id=device, note=f"Telegram {cmd}")
        tg_send(chat_id, ("✅ " if ok else "❌ ") + msg)
        return

    if cmd == "/status":
        if len(parts) < 2:
            tg_send(chat_id, "Cú pháp: /status USER_ID")
            return
        uid = normalize(parts[1])
        with DB_LOCK, db_conn() as db:
            rows = db.execute(
                "SELECT user_id,device_id,status,last_seen,note FROM clients WHERE app=? AND user_id=? ORDER BY updated_at DESC",
                (APP_NAME, uid),
            ).fetchall()
        if not rows:
            tg_send(chat_id, f"ℹ️ Chưa có dữ liệu cho {uid}")
        else:
            lines = [f"📋 {uid}"]
            for row in rows[:20]:
                lines.append(f"• {row['device_id']} | {row['status']} | last={row['last_seen'] or '-'}")
            tg_send(chat_id, "\n".join(lines))
        return

    if cmd == "/list":
        rows = list_clients()
        if not rows:
            tg_send(chat_id, "ℹ️ Chưa có client nào.")
            return
        lines = ["📋 CLIENT GẦN ĐÂY:"]
        for row in rows:
            lines.append(f"{row['user_id']} | {row['device_id']} | {row['status']}")
        tg_send(chat_id, "\n".join(lines))
        return

    tg_send(chat_id, "❓ Lệnh không hợp lệ. Dùng /help")


def telegram_loop():
    offset = None
    if BOT_TOKEN:
        # Nếu trước đó từng dùng webhook, xóa webhook để long polling nhận update.
        telegram_call("deleteWebhook", {"drop_pending_updates": False})

    while True:
        try:
            payload = {"timeout": 25, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            result = telegram_call("getUpdates", payload, timeout=35)
            if not result.get("ok"):
                time.sleep(3)
                continue
            for update in result.get("result", []):
                offset = int(update["update_id"]) + 1
                msg = update.get("message") or {}
                chat = msg.get("chat") or {}
                sender = msg.get("from") or {}
                text = msg.get("text") or ""
                if chat.get("id") is not None and text:
                    handle_text(chat["id"], sender.get("id"), text)
        except Exception:
            time.sleep(3)


@app.get("/")
def index():
    return jsonify({"ok": True, "service": APP_NAME, "status": "running"})


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/api/check")
def api_check():
    if not authorized_api(request):
        return jsonify({"allowed": False, "message": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    if data.get("app") != APP_NAME:
        return jsonify({"allowed": False, "message": "Sai app"}), 400
    allowed, message = check_client(data.get("user_id"), data.get("device_id"))
    return jsonify({"allowed": allowed, "message": message, "server_time": now_iso()})


@app.post("/api/admin/set")
def api_admin_set():
    if not authorized_api(request):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    ok, msg = set_status(data.get("user_id"), data.get("status", "locked"), data.get("device_id", "*"), data.get("note", "api"))
    return jsonify({"ok": ok, "message": msg})


def start_threads():
    init_db()
    threading.Thread(target=telegram_loop, daemon=True, name="telegram-bot").start()


init_db()
start_threads()

if __name__ == "__main__":
    # Render requires a public HTTP server on PORT.
    app.run(host="0.0.0.0", port=PORT, threaded=True)
