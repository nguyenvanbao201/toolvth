#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Toolxw File Lock server + Telegram admin bot.
Admin targets are FILE_ID values belonging to Toolxw files, not VIP keys.
"""
from __future__ import annotations

import html
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from hmac import compare_digest

from flask import Flask, jsonify, request
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

BOT_TOKEN = os.getenv("BOT_TOKEN", "8809444250:AAE1C2V5HyfoMrIfFRvVN9px4KCRJ3fP_ZM").strip()
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", "8801844480").split(",") if x.strip()}
SERVER_SECRET = os.getenv("SERVER_SECRET", "ToolxwFileLock_2026_4Yp8N7vQ2mK6").strip()
DB_PATH = os.getenv("DB_PATH", "data/toolxw_files.db").strip() or "data/toolxw_files.db"
PORT = int(os.getenv("PORT", "10000"))

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=8) if BOT_TOKEN else None
DB_LOCK = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_file_id(value: str | None) -> str:
    return str(value or "").strip().upper()


def is_admin(user_id: int) -> bool:
    return str(user_id) in ADMIN_IDS


def db_connect() -> sqlite3.Connection:
    path = os.path.abspath(DB_PATH)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with DB_LOCK, db_connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                file_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                device_id TEXT,
                created_at TEXT NOT NULL,
                first_seen_at TEXT,
                last_seen_at TEXT,
                locked_at TEXT,
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        db.commit()


def ensure_file(file_id: str) -> bool:
    file_id = normalize_file_id(file_id)
    if not file_id:
        return False
    with DB_LOCK, db_connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO files(file_id,status,created_at) VALUES (?, 'ACTIVE', ?)",
            (file_id, now()),
        )
        db.commit()
    return True


def get_file(file_id: str):
    file_id = normalize_file_id(file_id)
    if not file_id:
        return None
    with DB_LOCK, db_connect() as db:
        return db.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()


def register_seen(file_id: str, device_id: str = "") -> None:
    file_id = normalize_file_id(file_id)
    if not file_id:
        return
    ensure_file(file_id)
    device_id = str(device_id or "").strip()
    ts = now()
    with DB_LOCK, db_connect() as db:
        db.execute(
            """
            UPDATE files
               SET device_id = COALESCE(NULLIF(device_id,''), ?),
                   first_seen_at = COALESCE(first_seen_at, ?),
                   last_seen_at = ?
             WHERE file_id = ?
            """,
            (device_id, ts, ts, file_id),
        )
        db.commit()


def set_file_status(file_id: str, status: str) -> bool:
    file_id = normalize_file_id(file_id)
    if not file_id:
        return False
    ensure_file(file_id)
    status = status.upper()
    if status == "LOCKED":
        with DB_LOCK, db_connect() as db:
            db.execute(
                "UPDATE files SET status='LOCKED', locked_at=? WHERE file_id=?",
                (now(), file_id),
            )
            db.commit()
        return True
    if status == "ACTIVE":
        with DB_LOCK, db_connect() as db:
            db.execute(
                "UPDATE files SET status='ACTIVE', locked_at=NULL WHERE file_id=?",
                (file_id,),
            )
            db.commit()
        return True
    return False


def auth(req) -> bool:
    supplied = req.headers.get("X-Server-Secret", "")
    return bool(SERVER_SECRET) and compare_digest(supplied, SERVER_SECRET)


def file_info_text(row) -> str:
    if not row:
        return "❌ <b>FILE KHÔNG TỒN TẠI</b>"
    return (
        "📦 <b>THÔNG TIN FILE TOOLXW</b>\n"
        f"• FILE ID: <code>{html.escape(row['file_id'])}</code>\n"
        f"• Trạng thái: <b>{html.escape(row['status'])}</b>\n"
        f"• Device ID: <code>{html.escape(row['device_id'] or '-')}</code>\n"
        f"• Kết nối đầu: <code>{html.escape(row['first_seen_at'] or '-')}</code>\n"
        f"• Kết nối cuối: <code>{html.escape(row['last_seen_at'] or '-')}</code>\n"
        f"• Khóa lúc: <code>{html.escape(row['locked_at'] or '-')}</code>"
    )


def admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🔴 Thu hồi / Khóa file", callback_data="file_lock"),
        InlineKeyboardButton("🟢 Mở khóa file", callback_data="file_unlock"),
        InlineKeyboardButton("🔎 Kiểm tra file", callback_data="file_info"),
        InlineKeyboardButton("📋 Danh sách file", callback_data="file_list"),
    )
    return kb


# ---------------- HTTP API for Toolxw ----------------
@app.get("/")
def home():
    return jsonify(ok=True, service="Toolxw File Lock", version="2.0")


@app.get("/health")
def health():
    return jsonify(ok=True, time=now())


@app.post("/api/file_status")
def api_file_status():
    if not auth(request):
        return jsonify(success=False, status="UNAUTHORIZED"), 401
    payload = request.get_json(silent=True) or {}
    file_id = normalize_file_id(payload.get("file_id"))
    device_id = str(payload.get("device_id") or "").strip()
    if not file_id:
        return jsonify(success=False, status="INVALID", message="Thiếu FILE_ID"), 400

    ensure_file(file_id)
    row = get_file(file_id)
    if row and str(row["status"]).upper() == "LOCKED":
        return jsonify(
            success=False,
            status="LOCKED",
            file_id=file_id,
            message="FILE TOOLXW ĐÃ BỊ KHÓA/THU HỒI",
        )

    register_seen(file_id, device_id)
    return jsonify(success=True, status="ACTIVE", file_id=file_id, device_id=device_id)


@app.post("/api/lock_file")
def api_lock_file():
    if not auth(request):
        return jsonify(success=False, message="UNAUTHORIZED"), 401
    payload = request.get_json(silent=True) or {}
    file_id = normalize_file_id(payload.get("file_id"))
    if not file_id:
        return jsonify(success=False, message="Thiếu FILE_ID"), 400
    set_file_status(file_id, "LOCKED")
    return jsonify(success=True, status="LOCKED", file_id=file_id, message="Đã khóa/thu hồi file")


@app.post("/api/unlock_file")
def api_unlock_file():
    if not auth(request):
        return jsonify(success=False, message="UNAUTHORIZED"), 401
    payload = request.get_json(silent=True) or {}
    file_id = normalize_file_id(payload.get("file_id"))
    if not file_id:
        return jsonify(success=False, message="Thiếu FILE_ID"), 400
    set_file_status(file_id, "ACTIVE")
    return jsonify(success=True, status="ACTIVE", file_id=file_id, message="Đã mở khóa file")


@app.post("/api/file_info")
def api_file_info():
    if not auth(request):
        return jsonify(success=False, message="UNAUTHORIZED"), 401
    payload = request.get_json(silent=True) or {}
    row = get_file(payload.get("file_id"))
    if not row:
        return jsonify(success=False, message="FILE_ID chưa tồn tại"), 404
    return jsonify(success=True, file=dict(row))


@app.get("/api/files")
def api_files():
    if not auth(request):
        return jsonify(success=False, message="UNAUTHORIZED"), 401
    with DB_LOCK, db_connect() as db:
        rows = db.execute("SELECT * FROM files ORDER BY created_at DESC").fetchall()
    return jsonify(success=True, files=[dict(r) for r in rows])


# ---------------- Telegram helpers ----------------
def require_admin(message) -> bool:
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Bạn không có quyền sử dụng chức năng này.")
        return False
    return True


def parse_arg(message) -> str:
    parts = (message.text or "").split(maxsplit=1)
    return normalize_file_id(parts[1] if len(parts) == 2 else "")


def list_text() -> str:
    with DB_LOCK, db_connect() as db:
        rows = db.execute(
            "SELECT file_id,status,device_id,last_seen_at FROM files ORDER BY created_at DESC"
        ).fetchall()
    if not rows:
        return "📋 <b>DANH SÁCH FILE TOOLXW</b>\n\nChưa có file nào kết nối."
    lines = ["📋 <b>DANH SÁCH FILE TOOLXW</b>"]
    for i, row in enumerate(rows, 1):
        lines.append(
            f"\n<b>{i}. {html.escape(row['file_id'])}</b> — <b>{html.escape(row['status'])}</b>"
            f"\n• Device: <code>{html.escape(row['device_id'] or '-')}</code>"
            f"\n• Cuối: <code>{html.escape(row['last_seen_at'] or '-')}</code>"
        )
    return "\n".join(lines)


if bot:

    @bot.message_handler(commands=["start"])
    def cmd_start(message):
        if not require_admin(message):
            return
        bot.send_message(
            message.chat.id,
            "🛡️ <b>TOOLXW FILE CONTROL</b>\n\n"
            "/lock FILE_ID — khóa file\n"
            "/unlock FILE_ID — mở khóa\n"
            "/revoke FILE_ID — THU HỒI + KHÓA CỨNG\n"
            "/restore FILE_ID — mở khóa thường\n"
            "/unrevoke FILE_ID — gỡ thu hồi khóa cứng\n"
            "/status FILE_ID — xem trạng thái\n"
            "/list — danh sách file\n"
            "/adminid — xem ID admin\n"
            "/help — trợ giúp",
            reply_markup=admin_menu(),
        )

    @bot.message_handler(commands=["help"])
    def cmd_help(message):
        if not require_admin(message):
            return
        bot.reply_to(
            message,
            "🛡️ <b>TOOLXW FILE CONTROL</b>\n\n"
            "/lock FILE_ID\n"
            "/unlock FILE_ID\n"
            "/revoke FILE_ID — thu hồi + khóa cứng\n"
            "/restore FILE_ID\n"
            "/unrevoke FILE_ID — gỡ thu hồi khóa cứng\n"
            "/status FILE_ID\n"
            "/list\n"
            "/adminid",
        )

    @bot.message_handler(commands=["adminid"])
    def cmd_adminid(message):
        if not require_admin(message):
            return
        bot.reply_to(message, f"Admin Telegram ID: <code>{html.escape(str(message.from_user.id))}</code>")

    def simple_status_command(message):
        if not require_admin(message):
            return
        file_id = parse_arg(message)
        if not file_id:
            bot.reply_to(message, "❌ Cú pháp: <code>/status FILE_ID</code>")
            return
        bot.reply_to(message, file_info_text(get_file(file_id)))

    @bot.message_handler(commands=["status", "filestatus", "fileinfo", "keyinfo"])
    def cmd_status(message):
        simple_status_command(message)

    def simple_lock_command(message, hard: bool = False):
        if not require_admin(message):
            return
        file_id = parse_arg(message)
        if not file_id:
            bot.reply_to(message, "❌ Cú pháp: <code>/revoke FILE_ID</code>" if hard else "❌ Cú pháp: <code>/lock FILE_ID</code>")
            return
        set_file_status(file_id, "LOCKED")
        label = "THU HỒI + KHÓA CỨNG" if hard else "KHÓA FILE"
        bot.reply_to(
            message,
            f"✅ <b>{label}</b>\n• FILE ID: <code>{html.escape(file_id)}</code>\n• Trạng thái: <b>LOCKED</b>",
        )

    @bot.message_handler(commands=["lock"])
    def cmd_lock(message):
        simple_lock_command(message, hard=False)

    @bot.message_handler(commands=["revoke"])
    def cmd_revoke(message):
        simple_lock_command(message, hard=True)

    def simple_unlock_command(message):
        if not require_admin(message):
            return
        file_id = parse_arg(message)
        if not file_id:
            bot.reply_to(message, "❌ Cú pháp: <code>/unlock FILE_ID</code>")
            return
        set_file_status(file_id, "ACTIVE")
        bot.reply_to(
            message,
            f"✅ <b>ĐÃ MỞ KHÓA FILE</b>\n• FILE ID: <code>{html.escape(file_id)}</code>\n• Trạng thái: <b>ACTIVE</b>",
        )

    @bot.message_handler(commands=["unlock", "restore", "unrevoke"])
    def cmd_unlock(message):
        simple_unlock_command(message)

    @bot.message_handler(commands=["list", "files"])
    def cmd_list(message):
        if not require_admin(message):
            return
        bot.reply_to(message, list_text())

    def ask_for_file(message, action: str):
        if not require_admin(message):
            return
        prompts = {
            "lock": "🔴 <b>THU HỒI / KHÓA FILE</b>\n\nGửi FILE_ID cần thu hồi.",
            "unlock": "🟢 <b>MỞ KHÓA FILE</b>\n\nGửi FILE_ID cần mở khóa.",
            "status": "🔎 <b>KIỂM TRA FILE</b>\n\nGửi FILE_ID cần kiểm tra.",
        }
        sent = bot.send_message(message.chat.id, prompts[action])
        bot.register_next_step_handler(sent, lambda nxt: handle_file_input(nxt, action))

    def handle_file_input(message, action: str):
        if not require_admin(message):
            return
        file_id = normalize_file_id(message.text or "")
        if not file_id or file_id.startswith("/"):
            bot.reply_to(message, "❌ FILE_ID không hợp lệ.")
            return
        if action == "status":
            bot.reply_to(message, file_info_text(get_file(file_id)))
            return
        set_file_status(file_id, "LOCKED" if action == "lock" else "ACTIVE")
        if action == "lock":
            bot.reply_to(message, f"✅ <b>ĐÃ THU HỒI / KHÓA FILE</b>\n• FILE ID: <code>{html.escape(file_id)}</code>\n• Trạng thái: <b>LOCKED</b>")
        else:
            bot.reply_to(message, f"✅ <b>ĐÃ MỞ KHÓA FILE</b>\n• FILE ID: <code>{html.escape(file_id)}</code>\n• Trạng thái: <b>ACTIVE</b>")

    @bot.callback_query_handler(func=lambda call: call.data in {"file_lock", "file_unlock", "file_info", "file_list"})
    def callbacks(call):
        if not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "Bạn không có quyền", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        action_map = {"file_lock": "lock", "file_unlock": "unlock", "file_info": "status"}
        if call.data in action_map:
            ask_for_file(call.message, action_map[call.data])
            return
        bot.send_message(call.message.chat.id, list_text())


def bot_loop():
    if not bot:
        return
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as exc:
            print(f"Telegram polling error: {exc}", flush=True)
            time.sleep(5)


init_db()
if bot:
    threading.Thread(target=bot_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
