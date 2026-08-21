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

ID_ADMIN = os.getenv("ID_ADMIN", "8801844480").strip()
BOT = os.getenv("BOT", "8862931913:AAFLGMCWYYeSE1hbiUCWZoNAs0Ul7oEA8rc").strip()
BOT_TOKEN = BOT
PRIMARY_ADMIN_ID = os.getenv("PRIMARY_ADMIN_ID", "8801844480").strip()
ADMIN_IDS = {ID_ADMIN} if ID_ADMIN else set()
if PRIMARY_ADMIN_ID:
    ADMIN_IDS.add(PRIMARY_ADMIN_ID)
SERVER_SECRET = os.getenv("SERVER_SECRET", "ToolxwFileLock_2026_4Yp8N7vQ2mK6").strip()
ADMIN_BOOTSTRAP_SECRET = os.getenv("ADMIN_BOOTSTRAP_SECRET", SERVER_SECRET).strip()
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
    """Kiểm tra admin theo ID cấu hình hoặc danh sách admin đã lưu."""
    uid = str(int(user_id))
    if uid in ADMIN_IDS:
        return True
    try:
        with DB_LOCK, db_connect() as db:
            row = db.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone()
            return row is not None
    except Exception:
        return False


def admin_count() -> int:
    try:
        with DB_LOCK, db_connect() as db:
            row = db.execute("SELECT COUNT(*) FROM admins").fetchone()
            return int(row[0] if row else 0)
    except Exception:
        return 0


def auto_claim_first_admin(user_id: int) -> bool:
    """Cho tài khoản đầu tiên trở thành admin khi chưa có admin nào cấu hình/lưu."""
    if ADMIN_IDS or admin_count() > 0:
        return False
    add_admin(user_id)
    return True


def add_admin(user_id: int) -> None:
    with DB_LOCK, db_connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO admins(user_id, added_at) VALUES (?, ?)",
            (str(user_id), now()),
        )
        db.commit()


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
            CREATE TABLE IF NOT EXISTS admins (
                user_id TEXT PRIMARY KEY,
                added_at TEXT NOT NULL
            )
            """
        )
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
                revoked INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cols = {row[1] for row in db.execute("PRAGMA table_info(files)").fetchall()}
        if "revoked" not in cols:
            db.execute("ALTER TABLE files ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0")
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


def set_file_status(file_id: str, status: str, hard_revoke: bool = False) -> bool:
    file_id = normalize_file_id(file_id)
    if not file_id:
        return False
    ensure_file(file_id)
    status = status.upper()
    if status == "LOCKED":
        with DB_LOCK, db_connect() as db:
            db.execute(
                "UPDATE files SET status='LOCKED', locked_at=?, revoked=? WHERE file_id=?",
                (now(), 1 if hard_revoke else 0, file_id),
            )
            db.commit()
        return True
    if status == "ACTIVE":
        with DB_LOCK, db_connect() as db:
            db.execute(
                "UPDATE files SET status='ACTIVE', locked_at=NULL, revoked=0 WHERE file_id=?",
                (file_id,),
            )
            db.commit()
        return True
    return False


def revoke_file(file_id: str) -> bool:
    return set_file_status(file_id, "LOCKED", hard_revoke=True)


def restore_file(file_id: str) -> bool:
    return set_file_status(file_id, "ACTIVE", hard_revoke=False)


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
        f"• Khóa lúc: <code>{html.escape(row['locked_at'] or '-')}</code>\n"
        f"• Thu hồi cứng: <b>{'CÓ' if int(row['revoked'] or 0) else 'KHÔNG'}</b>"
    )


def admin_menu() -> InlineKeyboardMarkup:
    """Menu chính: chỉ hiện 2 chức năng quản lý file Toolxw."""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🔴 THU HỒI FILE", callback_data="file_lock"),
        InlineKeyboardButton("🟢 MỞ FILE", callback_data="file_unlock"),
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
    if row and (str(row["status"]).upper() == "LOCKED" or int(row["revoked"] or 0) == 1):
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
    set_file_status(file_id, "LOCKED", hard_revoke=False)
    return jsonify(success=True, status="LOCKED", file_id=file_id, message="Đã khóa file")


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
            "SELECT file_id,status,device_id,last_seen_at,revoked FROM files ORDER BY created_at DESC"
        ).fetchall()
    if not rows:
        return "📋 <b>DANH SÁCH FILE TOOLXW</b>\n\nChưa có file nào kết nối."
    lines = ["📋 <b>DANH SÁCH FILE TOOLXW</b>"]
    for i, row in enumerate(rows, 1):
        lines.append(
            f"\n<b>{i}. {html.escape(row['file_id'])}</b> — <b>{html.escape(row['status'])}</b>"
            f"{' 🔒 THU HỒI CỨNG' if int(row['revoked'] or 0) else ''}"
            f"\n• Device: <code>{html.escape(row['device_id'] or '-')}</code>"
            f"\n• Cuối: <code>{html.escape(row['last_seen_at'] or '-')}</code>"
        )
    return "\n".join(lines)


if bot:

    @bot.message_handler(commands=["myid"])
    def cmd_myid(message):
        bot.reply_to(message, f"🆔 Telegram ID của bạn: <code>{message.from_user.id}</code>")

    @bot.message_handler(commands=["admin"])
    def cmd_admin_claim(message):
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            bot.reply_to(message, "❌ Cú pháp: <code>/admin SERVER_SECRET</code>")
            return
        supplied = parts[1].strip()
        if not ADMIN_BOOTSTRAP_SECRET or not compare_digest(supplied, ADMIN_BOOTSTRAP_SECRET):
            bot.reply_to(message, "❌ Mã cấp quyền admin không đúng.")
            return
        add_admin(message.from_user.id)
        bot.reply_to(message, "✅ <b>Đã cấp quyền ADMIN thành công.</b>\nBây giờ bạn có thể dùng /revoke, /unlock, /lock và menu quản trị.", reply_markup=admin_menu())

    @bot.message_handler(commands=["checkadmin"])
    def cmd_checkadmin(message):
        uid = str(message.from_user.id)
        bot.reply_to(
            message,
            f"🆔 ID: <code>{html.escape(uid)}</code>\n"
            f"👑 ADMIN: <b>{"CÓ" if is_admin(message.from_user.id) else "KHÔNG"}</b>\n"
            f"🔐 PRIMARY_ADMIN_ID: <code>{html.escape(PRIMARY_ADMIN_ID or "-")}</code>"
        )

    @bot.message_handler(commands=["start", "menu"])
    def cmd_start(message):
        # Lần chạy đầu: nếu chưa có admin nào, tài khoản đầu tiên mở bot sẽ được cấp ADMIN.
        if not is_admin(message.from_user.id):
            if auto_claim_first_admin(message.from_user.id):
                bot.send_message(
                    message.chat.id,
                    f"✅ <b>Đã tự cấp quyền ADMIN cho ID {message.from_user.id}</b>\n\n"
                    "Tài khoản này là admin đầu tiên của hệ thống.",
                )
            else:
                return require_admin(message)
        bot.send_message(
            message.chat.id,
            "🛡️ <b>TOOLXW FILE CONTROL</b>\n\n"
            "Chọn chức năng bên dưới:",
            reply_markup=admin_menu(),
        )

    @bot.message_handler(commands=["help"])
    def cmd_help(message):
        if not require_admin(message):
            return
        bot.reply_to(
            message,
            "🛡️ <b>TOOLXW FILE CONTROL</b>\n\n"
            "Dùng menu bên dưới để <b>THU HỒI FILE</b> hoặc <b>MỞ FILE</b>.",
            reply_markup=admin_menu(),
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
        if hard:
            revoke_file(file_id)
        else:
            set_file_status(file_id, "LOCKED", hard_revoke=False)
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
        restore_file(file_id)
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
            "lock": "🔴 <b>THU HỒI / KHÓA CỨNG FILE</b>\n\nGửi FILE_ID cần thu hồi.",
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
        if action == "lock":
            ok = revoke_file(file_id)
            if ok:
                bot.reply_to(message, f"✅ <b>ĐÃ THU HỒI / KHÓA CỨNG FILE</b>\n• FILE ID: <code>{html.escape(file_id)}</code>\n• Trạng thái: <b>LOCKED</b>")
            else:
                bot.reply_to(message, "❌ Không thể khóa file.")
            return
        if action == "unlock":
            ok = restore_file(file_id)
            if ok:
                bot.reply_to(message, f"✅ <b>ĐÃ MỞ KHÓA FILE</b>\n• FILE ID: <code>{html.escape(file_id)}</code>\n• Trạng thái: <b>ACTIVE</b>")
            else:
                bot.reply_to(message, "❌ Không thể mở khóa file.")
            return

    @bot.callback_query_handler(func=lambda call: call.data in {"file_lock", "file_unlock"})
    def callbacks(call):
        if not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "Bạn không có quyền", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        action_map = {"file_lock": "lock", "file_unlock": "unlock"}
        ask_for_file(call.message, action_map[call.data])


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
