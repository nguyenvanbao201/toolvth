#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Toolxw File Lock Server + Telegram admin bot.
Quản lý khóa/thu hồi bản FILE Toolxw theo FILE_ID, không quản lý key VIP.
"""
from __future__ import annotations
import html, os, sqlite3, threading, time
from datetime import datetime, timezone
from hmac import compare_digest
from flask import Flask, jsonify, request
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
BOT_TOKEN=os.getenv("BOT_TOKEN","").strip()
ADMIN_IDS={x.strip() for x in os.getenv("ADMIN_IDS","8801844480").split(",") if x.strip()}
SERVER_SECRET=os.getenv("SERVER_SECRET","ToolxwFileLock_2026_4Yp8N7vQ2mK6").strip()
DB_PATH=os.getenv("DB_PATH","/var/data/toolxw_files.db")
PORT=int(os.getenv("PORT","10000"))
app=Flask(__name__)
bot=telebot.TeleBot(BOT_TOKEN,parse_mode="HTML",threaded=True) if BOT_TOKEN else None
LOCK=threading.Lock()
def conn():
    parent=os.path.dirname(os.path.abspath(DB_PATH))
    if parent: os.makedirs(parent,exist_ok=True)
    c=sqlite3.connect(DB_PATH,timeout=20); c.row_factory=sqlite3.Row; return c
def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def admin(uid): return str(uid) in ADMIN_IDS
def auth(req): return bool(SERVER_SECRET) and compare_digest(req.headers.get("X-Server-Secret",""),SERVER_SECRET)
def fid(x): return str(x or "").strip().upper()
def init_db():
    with LOCK,conn() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS files(file_id TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'ACTIVE',device_id TEXT,created_at TEXT NOT NULL,first_seen_at TEXT,last_seen_at TEXT,locked_at TEXT,note TEXT DEFAULT '')"""); db.commit()
def ensure_file(x):
    x=fid(x)
    if not x:return False
    with LOCK,conn() as db:
        db.execute("INSERT OR IGNORE INTO files(file_id,status,created_at) VALUES(?,?,?)",(x,"ACTIVE",now()));db.commit()
    return True
def get_file(x):
    with LOCK,conn() as db:return db.execute("SELECT * FROM files WHERE file_id=?",(fid(x),)).fetchone()
def seen(x,device):
    x=fid(x); ensure_file(x); device=str(device or "").strip()
    with LOCK,conn() as db:
        row=db.execute("SELECT first_seen_at FROM files WHERE file_id=?",(x,)).fetchone()
        db.execute("UPDATE files SET device_id=COALESCE(device_id,?),first_seen_at=COALESCE(first_seen_at,?),last_seen_at=? WHERE file_id=?",(device,now(),now(),x));db.commit()
def lock_file(x):
    with LOCK,conn() as db:
        cur=db.execute("UPDATE files SET status='LOCKED',locked_at=? WHERE file_id=?",(now(),fid(x)));db.commit();return cur.rowcount==1
def unlock_file(x):
    with LOCK,conn() as db:
        cur=db.execute("UPDATE files SET status='ACTIVE',locked_at=NULL WHERE file_id=? AND status='LOCKED'",(fid(x),));db.commit();return cur.rowcount==1
def file_text(r):
    if not r:return "❌ <b>FILE KHÔNG TỒN TẠI</b>"
    return f"📦 <b>FILE TOOLXW</b>\n• FILE ID: <code>{html.escape(r['file_id'])}</code>\n• Trạng thái: <b>{html.escape(r['status'])}</b>\n• Device ID: <code>{html.escape(r['device_id'] or '-')}</code>\n• Hoạt động cuối: <code>{html.escape(r['last_seen_at'] or '-')}</code>\n• Khóa lúc: <code>{html.escape(r['locked_at'] or '-')}</code>"
def menu():
    kb=InlineKeyboardMarkup(row_width=1);kb.add(InlineKeyboardButton("🔴 Thu hồi / Khóa file",callback_data="lock"),InlineKeyboardButton("🟢 Mở khóa file",callback_data="unlock"),InlineKeyboardButton("🔎 Kiểm tra file",callback_data="info"),InlineKeyboardButton("📋 Danh sách file",callback_data="list"));return kb
@app.get("/")
def home():return jsonify(ok=True,service="Toolxw File Lock")
@app.get("/health")
def health():return jsonify(ok=True,time=now())
@app.post("/api/file_status")
def status():
    if not auth(request):return jsonify(success=False,status="UNAUTHORIZED"),401
    p=request.get_json(silent=True) or {}; x=fid(p.get("file_id")); d=str(p.get("device_id") or "")
    if not x:return jsonify(success=False,status="INVALID",message="Thiếu FILE_ID"),400
    ensure_file(x); r=get_file(x)
    if r['status']=="LOCKED":return jsonify(success=False,status="LOCKED",file_id=x,message="FILE TOOLXW ĐÃ BỊ KHÓA/THU HỒI")
    seen(x,d);return jsonify(success=True,status="ACTIVE",file_id=x,device_id=d)
@app.post("/api/lock_file")
def api_lock():
    if not auth(request):return jsonify(success=False),401
    x=(request.get_json(silent=True) or {}).get("file_id")
    if not lock_file(x):return jsonify(success=False,message="FILE ID không tồn tại"),404
    return jsonify(success=True,status="LOCKED",message="Đã khóa/thu hồi file")
@app.post("/api/unlock_file")
def api_unlock():
    if not auth(request):return jsonify(success=False),401
    x=(request.get_json(silent=True) or {}).get("file_id")
    if not unlock_file(x):return jsonify(success=False,message="File không ở trạng thái LOCKED"),404
    return jsonify(success=True,status="ACTIVE",message="Đã mở khóa file")
@app.post("/api/file_info")
def api_info():
    if not auth(request):return jsonify(success=False),401
    r=get_file((request.get_json(silent=True) or {}).get("file_id"))
    if not r:return jsonify(success=False,message="FILE ID không tồn tại"),404
    return jsonify(success=True,file=dict(r))
@app.get("/api/files")
def api_files():
    if not auth(request):return jsonify(success=False),401
    with LOCK,conn() as db:rows=db.execute("SELECT * FROM files ORDER BY created_at DESC").fetchall()
    return jsonify(success=True,files=[dict(r) for r in rows])
if bot:
    @bot.message_handler(commands=["start","help"])
    def start(m):
        if admin(m.from_user.id):bot.send_message(m.chat.id,"🛡️ <b>TOOLXW FILE ADMIN</b>\n\n<code>/lockfile FILE_ID</code> — khóa/thu hồi file\n<code>/unlockfile FILE_ID</code> — mở khóa file\n<code>/filestatus FILE_ID</code> — xem trạng thái\n<code>/files</code> — danh sách file",reply_markup=menu())
        else:bot.reply_to(m,"❌ Bạn không có quyền.")
    @bot.message_handler(commands=["lockfile"])
    def lockcmd(m):
        if not admin(m.from_user.id):return bot.reply_to(m,"❌ Bạn không có quyền.")
        p=m.text.split(maxsplit=1)
        if len(p)<2:return bot.reply_to(m,"❌ Cú pháp: <code>/lockfile FILE_ID</code>")
        x=p[1].strip()
        if lock_file(x):bot.reply_to(m,f"✅ <b>ĐÃ KHÓA / THU HỒI FILE</b>\n• FILE ID: <code>{html.escape(x.upper())}</code>\n• Trạng thái: <b>LOCKED</b>")
        else:bot.reply_to(m,"❌ FILE ID không tồn tại.")
    @bot.message_handler(commands=["unlockfile"])
    def unlockcmd(m):
        if not admin(m.from_user.id):return bot.reply_to(m,"❌ Bạn không có quyền.")
        p=m.text.split(maxsplit=1)
        if len(p)<2:return bot.reply_to(m,"❌ Cú pháp: <code>/unlockfile FILE_ID</code>")
        x=p[1].strip()
        if unlock_file(x):bot.reply_to(m,f"✅ <b>ĐÃ MỞ KHÓA FILE</b>\n• FILE ID: <code>{html.escape(x.upper())}</code>\n• Trạng thái: <b>ACTIVE</b>")
        else:bot.reply_to(m,"❌ File không ở trạng thái LOCKED hoặc không tồn tại.")
    @bot.message_handler(commands=["filestatus"])
    def infocmd(m):
        if not admin(m.from_user.id):return bot.reply_to(m,"❌ Bạn không có quyền.")
        p=m.text.split(maxsplit=1)
        if len(p)<2:return bot.reply_to(m,"❌ Cú pháp: <code>/filestatus FILE_ID</code>")
        bot.reply_to(m,file_text(get_file(p[1])))
    @bot.message_handler(commands=["files"])
    def filescmd(m):
        if not admin(m.from_user.id):return bot.reply_to(m,"❌ Bạn không có quyền.")
        with LOCK,conn() as db:rows=db.execute("SELECT file_id,status,device_id,last_seen_at FROM files ORDER BY created_at DESC").fetchall()
        if not rows:return bot.reply_to(m,"📋 Chưa có file nào kết nối.")
        s="📋 <b>DANH SÁCH FILE TOOLXW</b>\n"+"\n".join(f"\n{ i }. <code>{html.escape(r['file_id'])}</code> — <b>{html.escape(r['status'])}</b>\nDevice: <code>{html.escape(r['device_id'] or '-')}</code>" for i,r in enumerate(rows,1));bot.reply_to(m,s)
    @bot.callback_query_handler(func=lambda c:c.data in {"lock","unlock","info","list"})
    def cb(c):
        if not admin(c.from_user.id):return bot.answer_callback_query(c.id,"Bạn không có quyền",show_alert=True)
        bot.answer_callback_query(c.id)
        msg={"lock":"🔴 Gửi: <code>/lockfile FILE_ID</code>","unlock":"🟢 Gửi: <code>/unlockfile FILE_ID</code>","info":"🔎 Gửi: <code>/filestatus FILE_ID</code>","list":"📋 Gửi <code>/files</code>"}[c.data];bot.send_message(c.message.chat.id,msg)
    def bot_loop():
        while True:
            try:bot.infinity_polling(skip_pending=True,timeout=30,long_polling_timeout=30)
            except Exception:time.sleep(5)
init_db()
if bot:threading.Thread(target=bot_loop,daemon=True).start()
if __name__=="__main__":app.run(host="0.0.0.0",port=PORT)
