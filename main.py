import sqlite3
import os
import uvicorn
import requests
import datetime
import json
import time
import random
import threading
import io
import re
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict

# ================= 配置与持久化 =================
CONFIG_DIR = "/app/config"
if not os.path.exists(CONFIG_DIR):
    os.makedirs(CONFIG_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")

# 🔥 固定日报封面
REPORT_COVER_URL = "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=1200&auto=format&fit=crop"
FALLBACK_IMAGE_URL = "https://img.hotimg.com/a444d32a033994d5b.png"

DEFAULT_CONFIG = {
    "emby_host": os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/'),
    "emby_api_key": os.getenv("EMBY_API_KEY", "").strip(),
    "tmdb_api_key": os.getenv("TMDB_API_KEY", "").strip(),
    "proxy_url": "",
    "hidden_users": [],
    # 🤖 机器人配置
    "tg_bot_token": "",
    "tg_chat_id": "",     
    "enable_bot": False,  
    "enable_notify": False 
}

class ConfigManager:
    def __init__(self):
        self.config = DEFAULT_CONFIG.copy()
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                    self.config.update(saved)
            except Exception as e: print(f"⚠️ Config Load Error: {e}")
    
    def save(self):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
        except Exception as e: print(f"⚠️ Config Save Error: {e}")

    def get(self, key): return self.config.get(key, DEFAULT_CONFIG.get(key))
    def set(self, key, value): self.config[key] = value; self.save()
    def get_all(self): return self.config

cfg = ConfigManager()

# ================= 基础设置 =================
PORT = 10307
SECRET_KEY = os.getenv("SECRET_KEY", "embypulse_secret_key_2026")
TMDB_FALLBACK_POOL = [
    "https://image.tmdb.org/t/p/original/zfbjgQE1uSd9wiPTX4VzsLi0rGG.jpg",
    "https://image.tmdb.org/t/p/original/rLb2cs785pePbIKYQz1CADtovh7.jpg",
    "https://image.tmdb.org/t/p/original/tmU7GeKVybMWFButWEGl2M4GeiP.jpg",
    "https://image.tmdb.org/t/p/original/kXfqcdQKsToO0OUXHcrrNCHDBzO.jpg",
    "https://image.tmdb.org/t/p/original/zb6fM1CX41D9rF9hdgclu0peUmy.jpg", 
    "https://image.tmdb.org/t/p/original/vI3aUGTuRRdM7J78KIdW98Lnidq.jpg",
    "https://image.tmdb.org/t/p/original/jXJxMcVoEuXzym3vFnjqDW4ifo6.jpg",
    "https://image.tmdb.org/t/p/original/sRLC052ieEroxViUFWa3KD77SII.jpg",
    "https://image.tmdb.org/t/p/original/mSDsSDwaP3E7dEfUPWy4J0djt4O.jpg",
    "https://image.tmdb.org/t/p/original/lzWHmYdfeFiMIY4JaMmtR7GEli3.jpg",
]

print(f"--- EmbyPulse V60 (User Management Fixed) ---")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400*7)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

if not os.path.exists("static"): os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ================= 数据模型 =================
class LoginModel(BaseModel):
    username: str
    password: str

class SettingsModel(BaseModel):
    emby_host: str
    emby_api_key: str
    tmdb_api_key: Optional[str] = ""
    proxy_url: Optional[str] = ""
    hidden_users: List[str] = []

class BotSettingsModel(BaseModel):
    tg_bot_token: str
    tg_chat_id: str
    enable_bot: bool
    enable_notify: bool

# 🔥 用户管理模型
class UserUpdateModel(BaseModel):
    user_id: str
    password: Optional[str] = None
    is_disabled: Optional[bool] = None
    expire_date: Optional[str] = None # YYYY-MM-DD

class NewUserModel(BaseModel):
    name: str
    password: str
    expire_date: Optional[str] = None

# ================= 辅助函数 =================

# 🔥 数据库初始化
def init_db():
    if not os.path.exists(DB_PATH): return
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users_meta (
                        user_id TEXT PRIMARY KEY,
                        expire_date TEXT,
                        note TEXT,
                        created_at TEXT
                    )''')
        conn.commit()
        conn.close()
    except Exception as e: print(f"❌ DB Init Error: {e}")

init_db()

# 🔥 修改：支持读写操作 (去掉 mode=ro)
def query_db(query, args=(), one=False):
    if not os.path.exists(DB_PATH): return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=rw", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, args)
        
        if query.strip().upper().startswith("SELECT"):
            rv = cur.fetchall()
            conn.close()
            return (rv[0] if rv else None) if one else rv
        else:
            conn.commit()
            conn.close()
            return True
    except Exception as e: 
        # print(f"❌ SQL Error: {e}") 
        return None

def get_base_filter(user_id_filter: Optional[str]):
    where = "WHERE 1=1"
    params = []
    if user_id_filter and user_id_filter != 'all':
        where += " AND UserId = ?"
        params.append(user_id_filter)
    if (not user_id_filter or user_id_filter == 'all') and len(cfg.get("hidden_users")) > 0:
        hidden = cfg.get("hidden_users")
        placeholders = ','.join(['?'] * len(hidden))
        where += f" AND UserId NOT IN ({placeholders})"
        params.extend(hidden)
    return where, params

def get_user_map():
    user_map = {}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if key and host:
        try:
            res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=2)
            if res.status_code == 200:
                for u in res.json(): user_map[u['Id']] = u['Name']
        except: pass
    return user_map

# ================= 🤖 Telegram Bot 核心逻辑 =================
class TelegramBot:
    def __init__(self):
        self.running = False
        self.poll_thread = None
        self.monitor_thread = None
        self.schedule_thread = None 
        self.offset = 0
        self.active_sessions = {}
        self.last_check_min = -1
        
    def start(self):
        if self.running: return
        if not cfg.get("enable_bot") or not cfg.get("tg_bot_token"):
            print("🤖 Bot config missing or disabled.")
            return
        
        self.running = True
        self._set_commands()
        
        self.poll_thread = threading.Thread(target=self._polling_loop, daemon=True)
        self.poll_thread.start()
        
        if cfg.get("enable_notify"):
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
        
        self.schedule_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.schedule_thread.start()
            
        print("🤖 Telegram Bot & Monitor Started!")

    def stop(self):
        self.running = False
        print("🤖 Stopping Bot...")

    def _get_proxies(self):
        proxy = cfg.get("proxy_url")
        return {"http": proxy, "https": proxy} if proxy else None

    def _get_location(self, ip):
        if not ip: return "未知"
        if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("127.") or ip == "::1":
            return "局域网 / 内网"
        try:
            url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
            res = requests.get(url, timeout=3)
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "success":
                    return f"{data.get('country','')} {data.get('city','')}".strip()
        except: pass
        return "未知位置"

    def _set_commands(self):
        token = cfg.get("tg_bot_token")
        if not token: return
        commands = [
            {"command": "start", "description": "🤖 唤醒"},
            {"command": "stats", "description": "📊 日报"},
            {"command": "now", "description": "🟢 状态"},
            {"command": "recent", "description": "🕰 记录"},
            {"command": "top", "description": "🏆 榜单"},
            {"command": "check", "description": "✅ 检查"}
        ]
        try:
            url = f"https://api.telegram.org/bot{token}/setMyCommands"
            requests.post(url, json={"commands": commands}, proxies=self._get_proxies(), timeout=10)
        except: pass

    def _download_emby_image(self, item_id, img_type='Primary'):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return None
        try:
            url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=1200&quality=90&api_key={key}"
            res = requests.get(url, timeout=5)
            if res.status_code == 200: return io.BytesIO(res.content)
        except: pass
        return None

    def send_photo(self, chat_id, photo_io, caption, parse_mode="HTML"):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode}
            if isinstance(photo_io, str):
                data["photo"] = photo_io
                requests.post(url, data=data, proxies=self._get_proxies(), timeout=20)
            else:
                photo_io.seek(0) 
                files = {"photo": ("image.jpg", photo_io, "image/jpeg")}
                requests.post(url, data=data, files=files, proxies=self._get_proxies(), timeout=20)
        except Exception as e: 
            self.send_message(chat_id, caption)

    def send_message(self, chat_id, text, parse_mode="HTML"):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}, proxies=self._get_proxies(), timeout=10)
        except Exception as e: print(f"Bot Send Error: {e}")

    def _polling_loop(self):
        token = cfg.get("tg_bot_token")
        admin_id = str(cfg.get("tg_chat_id"))
        while self.running:
            try:
                url = f"https://api.telegram.org/bot{token}/getUpdates"
                params = {"offset": self.offset, "timeout": 30}
                res = requests.get(url, params=params, proxies=self._get_proxies(), timeout=35)
                if res.status_code == 200:
                    updates = res.json().get("result", [])
                    for update in updates:
                        self.offset = update["update_id"] + 1
                        if "message" in update:
                            self._handle_message(update["message"], admin_id)
                else: time.sleep(5)
            except: time.sleep(5)

    def _handle_message(self, msg, admin_id):
        chat_id = str(msg.get("chat", {}).get("id"))
        text = msg.get("text", "").strip()
        if admin_id and chat_id != admin_id:
            self.send_message(chat_id, "🚫 <b>Access Denied</b>")
            return
        if text.startswith("/start"):
            self.send_message(chat_id, "👋 <b>EmbyPulse</b>\n\n指令列表：\n/stats - 图文日报\n/now - 实时状态\n/recent - 最近记录\n/top - 排行榜\n/search [名] - 搜记录")
        elif text.startswith("/stats"): self._cmd_stats(chat_id)
        elif text.startswith("/recent"): self._cmd_recent(chat_id)
        elif text.startswith("/now"): self._cmd_now(chat_id)
        elif text.startswith("/check"): self._cmd_check(chat_id)
        elif text.startswith("/top"): self._cmd_top(chat_id)
        elif text.startswith("/history"): self._cmd_history(chat_id, text[9:].strip())
        elif text.startswith("/search"): self._cmd_search(chat_id, text[7:].strip())

    def _monitor_loop(self):
        admin_id = str(cfg.get("tg_chat_id"))
        while self.running and cfg.get("enable_notify"):
            try:
                key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
                if not key or not host: 
                    time.sleep(30); continue

                res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=5)
                if res.status_code == 200:
                    current_active_ids = []
                    for s in res.json():
                        if s.get("NowPlayingItem"):
                            sid = s.get("Id"); current_active_ids.append(sid)
                            item = s["NowPlayingItem"]; item_id = item.get("Id")
                            name = item.get("Name", "未知"); series = item.get("SeriesName")
                            season_num = item.get("ParentIndexNumber"); ep_num = item.get("IndexNumber")
                            media_type = "🎬 电影"; title_fmt = name
                            if series:
                                media_type = "📚 剧集"
                                if season_num is not None and ep_num is not None: title_fmt = f"{series} S{season_num:02d}E{ep_num:02d} 第 {ep_num} 集"
                                else: title_fmt = f"{series} - {name}"
                            ticks = s.get("PlayState", {}).get("PositionTicks", 0)
                            total = item.get("RunTimeTicks", 1)
                            pct = f"{(ticks/total)*100:.1f}%" if total > 0 else "0%"
                            user = s.get("UserName", "User")
                            client = s.get("Client", "Unknown"); device = s.get("DeviceName", "Unknown"); device_str = f"{client} {device}"
                            remote = s.get("RemoteEndPoint", ""); ip = remote.split(":")[0] if remote else "Unknown"
                            if "ffff" in ip: ip = re.search(r'\d+\.\d+\.\d+\.\d+', ip).group(0) if re.search(r'\d+\.\d+\.\d+\.\d+', ip) else ip
                            
                            if sid not in self.active_sessions:
                                location = self._get_location(ip)
                                now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                session_data = {"title": title_fmt, "type": media_type, "user": user, "ip": ip, "location": location, "device": device_str, "start_time": now_time, "item_id": item_id, "pct": pct}
                                self.active_sessions[sid] = session_data
                                msg = (f"▶️ <b>【{user}】开始播放</b>\n📺 <b>{title_fmt}</b>\n──────────────\n📚 类型：{media_type}\n🔄 进度：{pct}\n🌐 地址：{ip} ({location})\n📱 设备：{device_str}\n🕒 时间：{now_time}")
                                img = self._download_emby_image(item_id, 'Backdrop') or self._download_emby_image(item_id, 'Primary')
                                if img: self.send_photo(admin_id, img, msg)
                                else: self.send_message(admin_id, msg)
                            else:
                                self.active_sessions[sid]["pct"] = pct

                    stopped = [sid for sid in self.active_sessions if sid not in current_active_ids]
                    for sid in stopped:
                        info = self.active_sessions[sid]
                        stop_time = datetime.datetime.now().strftime("%H:%M:%S")
                        msg = (f"⏹️ <b>【{info['user']}】停止播放</b>\n📺 <b>{info['title']}</b>\n──────────────\n📚 类型：{info['type']}\n🔄 进度：{info['pct']}\n🌐 地址：{info['ip']} ({info['location']})\n📱 设备：{info['device']}\n🕒 时间：{stop_time}")
                        img = self._download_emby_image(info['item_id'], 'Backdrop') or self._download_emby_image(info['item_id'], 'Primary')
                        if img: self.send_photo(admin_id, img, msg)
                        else: self.send_message(admin_id, msg)
                        del self.active_sessions[sid]
                time.sleep(10)
            except Exception as e: time.sleep(10)

    # 🔥 调度器：用户有效期检查
    def _scheduler_loop(self):
        while self.running:
            try:
                now = datetime.datetime.now()
                if now.minute != self.last_check_min:
                    self.last_check_min = now.minute
                    # 每天 09:00 检查用户过期
                    if now.hour == 9 and now.minute == 0:
                        self._check_user_expiration()
                time.sleep(5)
            except: time.sleep(60)

    # 🔥 检查过期用户
    def _check_user_expiration(self):
        print("🔍 Checking user expirations...")
        users = query_db("SELECT user_id, expire_date FROM users_meta WHERE expire_date IS NOT NULL AND expire_date != ''")
        if not users: return
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        admin_id = str(cfg.get("tg_chat_id"))
        for u in users:
            if u['expire_date'] < today:
                user_id = u['user_id']
                try:
                    res = requests.get(f"{host}/emby/Users/{user_id}?api_key={key}")
                    if res.status_code == 200:
                        user_data = res.json()
                        policy = user_data.get('Policy', {})
                        if not policy.get('IsDisabled'):
                            policy['IsDisabled'] = True
                            update_res = requests.post(f"{host}/emby/Users/{user_id}/Policy?api_key={key}", json=policy)
                            if update_res.status_code == 204:
                                name = user_data.get('Name')
                                print(f"🚫 User {name} expired.")
                                if admin_id: self.send_message(admin_id, f"🚫 <b>账号过期通知</b>\n用户：{name}\n到期日：{u['expire_date']}\n状态：已自动禁用")
                except: pass

    # --- 指令实现 ---
    def _cmd_stats(self, chat_id):
        where, params = get_base_filter('all')
        total_plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)[0]['c']
        today_where = where + " AND DateCreated > date('now', 'start of day', 'localtime')"
        today_plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {today_where}", params)[0]['c']
        today_dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {today_where}", params)[0]['c'] or 0
        today_hours = round(today_dur / 3600, 1)
        active_users = query_db(f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {today_where}", params)[0]['c']
        top_user_sql = f"SELECT UserId, SUM(PlayDuration) as D FROM PlaybackActivity {today_where} GROUP BY UserId ORDER BY D DESC LIMIT 1"
        top_user_res = query_db(top_user_sql, params)
        user_map = get_user_map()
        top_user_str = "暂无"
        if top_user_res:
            u_name = user_map.get(top_user_res[0]['UserId'], "User")
            u_time = round(top_user_res[0]['D'] / 3600, 1)
            top_user_str = f"{u_name} ({u_time}h)"
        recent_sql = f"SELECT DateCreated, UserId, ItemName, PlayDuration FROM PlaybackActivity {today_where} ORDER BY DateCreated DESC LIMIT 10"
        recent_rows = query_db(recent_sql, params)
        detail_str = ""
        if recent_rows:
            for r in recent_rows:
                t = r['DateCreated'].split(' ')[1][:5] if ' ' in r['DateCreated'] else r['DateCreated'][-8:-3]
                u = user_map.get(r['UserId'], "User")
                dur = round((r['PlayDuration'] or 0) / 60)
                detail_str += f"<code>{t}</code> | <b>{u}</b> | ⏳{dur}m\n└ {r['ItemName']}\n"
        else: detail_str = "<i>(今日暂无播放记录)</i>"
        lib_str = "..."
        try:
            key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
            res = requests.get(f"{host}/emby/Items/Counts?api_key={key}", timeout=2)
            d = res.json()
            lib_str = f"🎬 {d.get('MovieCount')} | 📺 {d.get('SeriesCount')} | 💿 {d.get('EpisodeCount')}"
        except: pass
        today_date = datetime.datetime.now().strftime("%Y-%m-%d")
        caption = (f"📊 <b>EmbyPulse 每日速报</b>\n📅 <code>{today_date}</code>\n───────────────\n<b>📈 今日大盘</b>\n▶️ 播放次数: <b>{today_plays}</b>\n⏱️ 播放时长: <b>{today_hours}</b> 小时\n👥 活跃人数: <b>{active_users}</b>\n🏆 今日榜首: <b>{top_user_str}</b>\n📦 媒体库存: {lib_str}\n───────────────\n<b>📝 今日流水 (Top 10)</b>\n{detail_str}")
        self.send_photo(chat_id, REPORT_COVER_URL, caption)

    def _cmd_recent(self, chat_id):
        where, params = get_base_filter('all')
        rows = query_db(f"SELECT DateCreated, UserId, ItemName, PlayDuration FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 8", params)
        user_map = get_user_map()
        if not rows: return self.send_message(chat_id, "📭 暂无记录")
        msg = "🕰 <b>最近 8 条播放记录</b>\n───────────────\n"
        for r in rows:
            u = user_map.get(r['UserId'], "User")
            t = r['DateCreated'].split(' ')[1][:5] if ' ' in r['DateCreated'] else r['DateCreated']
            dur = round((r['PlayDuration'] or 0) / 60)
            msg += f"<code>{t}</code> <b>{u}</b> ({dur}m)\n└ {r['ItemName']}\n"
        self.send_message(chat_id, msg)

    def _cmd_top(self, chat_id):
        where, params = get_base_filter('all')
        week_sql = f"SELECT UserId, SUM(PlayDuration) as D FROM PlaybackActivity {where} AND DateCreated > date('now', '-7 days') GROUP BY UserId ORDER BY D DESC LIMIT 3"
        week_rows = query_db(week_sql, params)
        month_sql = f"SELECT UserId, SUM(PlayDuration) as D FROM PlaybackActivity {where} AND DateCreated > date('now', '-30 days') GROUP BY UserId ORDER BY D DESC LIMIT 3"
        month_rows = query_db(month_sql, params)
        user_map = get_user_map()
        def format_rank(rows):
            if not rows: return "<i>无数据</i>"
            res = ""
            medals = ["🥇", "🥈", "🥉"]
            for i, r in enumerate(rows):
                u = user_map.get(r['UserId'], "User")
                h = round(r['D']/3600, 1)
                res += f"{medals[i]} <b>{u}</b> — <code>{h}h</code>\n"
            return res
        msg = f"🏆 <b>观影排行榜</b>\n\n<b>📅 本周 Top 3</b>\n{format_rank(week_rows)}\n<b>🗓 本月 Top 3</b>\n{format_rank(month_rows)}"
        self.send_message(chat_id, msg)

    def _cmd_now(self, chat_id):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=5)
            sessions = [s for s in res.json() if s.get("NowPlayingItem")]
            if not sessions: return self.send_message(chat_id, "💤 当前服务器空闲")
            for s in sessions:
                item = s["NowPlayingItem"]; item_id = item.get("Id")
                title = item.get("Name")
                if item.get("SeriesName"): title = f"{item.get('SeriesName')} - {title}"
                ticks = s.get("PlayState", {}).get("PositionTicks", 0)
                total = item.get("RunTimeTicks", 1)
                pct = int((ticks / total) * 100) if total > 0 else 0
                bar = "▓" * (pct // 10) + "░" * (10 - (pct // 10))
                transcode = "🔥转码" if s.get("PlayState", {}).get("IsTranscoding") else "⚡直通"
                device = s.get("DeviceName"); user = s.get("UserName")
                caption = f"🟢 <b>正在播放</b>\n\n📺 <b>{title}</b>\n👤 <b>{user}</b> @ {device}\n[{bar}] {pct}%\n⚙️ {transcode}"
                img_data = self._download_emby_image(item_id, 'Backdrop')
                if img_data: self.send_photo(chat_id, img_data, caption)
                else: self.send_message(chat_id, caption)
        except: self.send_message(chat_id, "❌ 连接 Emby 失败")

    def _cmd_check(self, chat_id):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        start = time.time()
        try:
            requests.get(f"{host}/emby/System/Info?api_key={key}", timeout=5)
            ping = int((time.time() - start) * 1000)
            self.send_message(chat_id, f"✅ <b>Emby 服务器在线</b>\n📶 延迟: {ping}ms\n🔗 地址: {host}")
        except: self.send_message(chat_id, "❌ <b>Emby 服务器离线</b>")

    def _cmd_history(self, chat_id, username):
        if not username: return self.send_message(chat_id, "用法: /history 用户名")
        user_map = get_user_map()
        target_id = None
        for uid, name in user_map.items():
            if name.lower() == username.lower(): target_id = uid; break
        if not target_id: return self.send_message(chat_id, f"🚫 找不到用户: {username}")
        where, params = get_base_filter('all') 
        sql = f"SELECT DateCreated, ItemName, PlayDuration FROM PlaybackActivity {where} AND UserId = ? ORDER BY DateCreated DESC LIMIT 10"
        rows = query_db(sql, params + [target_id])
        if not rows: return self.send_message(chat_id, f"📭 {username} 暂无记录")
        msg = f"👤 <b>{username} 的最近记录</b>\n───────────────\n"
        for r in rows:
            t = r['DateCreated'].split(' ')[0][5:]; dur = round((r['PlayDuration'] or 0) / 60)
            msg += f"• {t} | {dur}m | {r['ItemName']}\n"
        self.send_message(chat_id, msg)

    def _cmd_search(self, chat_id, keyword):
        if not keyword: return self.send_message(chat_id, "请提供关键词，例如：/search 阿凡达")
        where, params = get_base_filter('all')
        sql = f"SELECT DateCreated, UserId, ItemName FROM PlaybackActivity {where} AND ItemName LIKE ? ORDER BY DateCreated DESC LIMIT 8"
        rows = query_db(sql, params + [f"%{keyword}%"])
        user_map = get_user_map()
        if not rows: return self.send_message(chat_id, f"🔍 未找到关于 '{keyword}' 的记录")
        msg = f"🔍 <b>搜索: {keyword}</b>\n\n"
        for r in rows:
            u = user_map.get(r['UserId'], "User"); d = r['DateCreated'].split(' ')[0]
            msg += f"• {d} <b>{u}</b>\n  {r['ItemName']}\n"
        self.send_message(chat_id, msg)

bot = TelegramBot()

@app.on_event("startup")
async def startup_event():
    bot.start()

@app.on_event("shutdown")
async def shutdown_event():
    bot.stop()

# ================= 🚀 用户管理 API (修复版) =================

@app.get("/api/manage/users")
def api_manage_users(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code != 200: return {"status": "error", "message": "Emby API Error"}
        emby_users = res.json()
        
        meta_rows = query_db("SELECT * FROM users_meta")
        meta_map = {r['user_id']: dict(r) for r in meta_rows} if meta_rows else {}
        
        final_list = []
        for u in emby_users:
            uid = u['Id']
            meta = meta_map.get(uid, {})
            policy = u.get('Policy', {})
            final_list.append({
                "Id": uid, 
                "Name": u['Name'], 
                "LastLoginDate": u.get('LastLoginDate'),
                "IsDisabled": policy.get('IsDisabled', False), 
                "IsAdmin": policy.get('IsAdministrator', False),
                "ExpireDate": meta.get('expire_date'), 
                "Note": meta.get('note'),
                "PrimaryImageTag": u.get('PrimaryImageTag') # 🔥 修复：添加头像Tag
            })
        
        return {"status": "success", "data": final_list}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.post("/api/manage/user/update")
def api_manage_user_update(data: UserUpdateModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        # 1. 修复：确保 ID 正确存入 DB
        if data.expire_date is not None:
            exist = query_db("SELECT 1 FROM users_meta WHERE user_id = ?", (data.user_id,), one=True)
            if exist: query_db("UPDATE users_meta SET expire_date = ? WHERE user_id = ?", (data.expire_date, data.user_id))
            else: query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (data.user_id, data.expire_date, datetime.datetime.now().isoformat()))
        
        # 2. 修复：密码重置逻辑
        if data.password:
            r = requests.post(f"{host}/emby/Users/{data.user_id}/Password?api_key={key}", json={"NewPassword": data.password, "ResetPassword": True})
            if r.status_code not in [200, 204]: return {"status": "error", "message": f"密码重置失败: {r.text}"}

        # 3. 禁用状态
        if data.is_disabled is not None:
            p_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if p_res.status_code == 200:
                policy = p_res.json().get('Policy', {})
                policy['IsDisabled'] = data.is_disabled
                requests.post(f"{host}/emby/Users/{data.user_id}/Policy?api_key={key}", json=policy)

        return {"status": "success", "message": "更新成功"}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.post("/api/manage/user/new")
def api_manage_user_new(data: NewUserModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        # 1. 创建用户
        res = requests.post(f"{host}/emby/Users/New?api_key={key}", json={"Name": data.name})
        if res.status_code != 200: return {"status": "error", "message": f"创建失败: {res.text}"}
        new_id = res.json()['Id']
        
        # 2. 🔥 修复：显式设置 Policy 启用用户
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json={"IsDisabled": False})

        # 3. 🔥 修复：设置密码
        if data.password:
            r = requests.post(f"{host}/emby/Users/{new_id}/Password?api_key={key}", json={"NewPassword": data.password, "ResetPassword": True})
            if r.status_code not in [200, 204]: return {"status": "error", "message": "用户创建成功但密码设置失败"}

        # 4. 有效期
        if data.expire_date:
            query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (new_id, data.expire_date, datetime.datetime.now().isoformat()))
            
        return {"status": "success", "message": "用户创建成功"}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.delete("/api/manage/user/{user_id}")
def api_manage_user_delete(user_id: str, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        res = requests.delete(f"{host}/emby/Users/{user_id}?api_key={key}")
        if res.status_code in [200, 204]:
            query_db("DELETE FROM users_meta WHERE user_id = ?", (user_id,))
            return {"status": "success", "message": "用户已删除"}
        return {"status": "error", "message": "删除失败"}
    except Exception as e: return {"status": "error", "message": str(e)}

# 🔥 修复：下拉框数据源改为 Emby API (解决新用户不显示)
@app.get("/api/users")
def api_get_users():
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if not key: return {"status": "error"}
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code == 200:
            users = res.json()
            hidden_users = cfg.get("hidden_users") or []
            data = []
            for u in users:
                data.append({"UserId": u['Id'], "UserName": u['Name'], "IsHidden": u['Id'] in hidden_users})
            data.sort(key=lambda x: x['UserName'])
            return {"status": "success", "data": data}
        return {"status": "success", "data": []}
    except Exception as e: return {"status": "error", "message": str(e)}

# ================= 🚀 页面路由 (新增) =================
@app.get("/users_manage")
async def page_users_manage(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("users.html", {"request": request, "active_page": "users_manage", "user": request.session.get("user")})

# ================= 🤖 机器人配置路由 =================
@app.get("/bot")
async def page_bot(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("bot.html", {"request": request, "active_page": "bot", "user": request.session.get("user")})

@app.get("/api/bot/settings")
def api_get_bot_settings(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    return {"status": "success", "data": {
        "tg_bot_token": cfg.get("tg_bot_token"),
        "tg_chat_id": cfg.get("tg_chat_id"),
        "enable_bot": cfg.get("enable_bot"),
        "enable_notify": cfg.get("enable_notify")
    }}

@app.post("/api/bot/settings")
def api_save_bot_settings(data: BotSettingsModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    cfg.set("tg_bot_token", data.tg_bot_token); cfg.set("tg_chat_id", data.tg_chat_id)
    cfg.set("enable_bot", data.enable_bot); cfg.set("enable_notify", data.enable_notify)
    bot.stop()
    if data.enable_bot: threading.Timer(1.0, bot.start).start()
    return {"status": "success", "message": "配置已保存，机器人状态已更新"}

@app.post("/api/bot/test")
def api_test_bot(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    token = cfg.get("tg_bot_token"); chat_id = cfg.get("tg_chat_id"); proxy = cfg.get("proxy_url")
    if not token or not chat_id: return {"status": "error", "message": "请先保存完整的 Bot 配置"}
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        res = requests.post(url, json={"chat_id": chat_id, "text": "🎉 <b>连接成功！</b>\nEmbyPulse 机器人已就绪。"}, proxies=proxies, timeout=10)
        if res.status_code == 200: return {"status": "success", "message": "测试消息已发送"}
        else: return {"status": "error", "message": f"API Error: {res.text}"}
    except Exception as e: return {"status": "error", "message": f"Connect Error: {str(e)}"}

# ================= 原有 API 保持不变 =================
@app.get("/login")
async def page_login(request: Request):
    if request.session.get("user"): return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/api/login")
def api_login(data: LoginModel, request: Request):
    try:
        host = cfg.get("emby_host")
        if not host: return {"status": "error", "message": "请先在环境变量或配置文件中设置 EMBY_HOST"}
        auth_url = f"{host}/emby/Users/AuthenticateByName"
        headers = {"X-Emby-Authorization": 'MediaBrowser Client="EmbyPulse", Device="Web", DeviceId="EmbyPulse", Version="1.0.0"'}
        payload = {"Username": data.username, "Pw": data.password}
        res = requests.post(auth_url, json=payload, headers=headers, timeout=5)
        if res.status_code == 200:
            user_data = res.json()
            user_info = user_data.get("User", {})
            if not user_info.get("Policy", {}).get("IsAdministrator", False): return {"status": "error", "message": "仅限 Emby 管理员登录"}
            request.session["user"] = {"id": user_info.get("Id"), "name": user_info.get("Name"), "is_admin": True}
            return {"status": "success", "message": "登录成功"}
        else: return {"status": "error", "message": "用户名或密码错误"}
    except Exception as e: return {"status": "error", "message": f"连接失败: {str(e)}"}

@app.get("/logout")
async def api_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")

@app.get("/api/wallpaper")
def api_get_wallpaper():
    tmdb_key = cfg.get("tmdb_api_key"); proxy = cfg.get("proxy_url")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if tmdb_key:
        try:
            url = f"https://api.themoviedb.org/3/trending/all/week?api_key={tmdb_key}&language=zh-CN"
            res = requests.get(url, timeout=8, proxies=proxies)
            if res.status_code == 200:
                data = res.json()
                results = [i for i in data.get("results", []) if i.get("backdrop_path")]
                if results:
                    target = random.choice(results)
                    return {"status": "success", "url": f"https://image.tmdb.org/t/p/original{target['backdrop_path']}", "title": target.get("title") or target.get("name")}
        except: pass
    return {"status": "success", "url": random.choice(TMDB_FALLBACK_POOL), "title": "Cinematic Collection"}

@app.get("/")
async def page_dashboard(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("index.html", {"request": request, "active_page": "dashboard", "user": request.session.get("user")})

@app.get("/content")
async def page_content(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("content.html", {"request": request, "active_page": "content", "user": request.session.get("user")})

@app.get("/report")
async def page_report(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("report.html", {"request": request, "active_page": "report", "user": request.session.get("user")})

@app.get("/details")
async def page_details(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("details.html", {"request": request, "active_page": "details", "user": request.session.get("user")})

@app.get("/settings")
async def page_settings(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("settings.html", {"request": request, "active_page": "settings", "user": request.session.get("user")})

@app.get("/api/settings")
def api_get_settings(request: Request):
    if not request.session.get("user"): return {"status": "error", "message": "Unauthorized"}
    conf = cfg.get_all().copy()
    return {"status": "success", "data": conf}

@app.post("/api/settings")
def api_save_settings(data: SettingsModel, request: Request):
    if not request.session.get("user"): return {"status": "error", "message": "Unauthorized"}
    try:
        test_url = f"{data.emby_host.rstrip('/')}/emby/System/Info?api_key={data.emby_api_key}"
        res = requests.get(test_url, timeout=5)
        if res.status_code != 200: return {"status": "error", "message": "Emby 连接失败"}
    except: return {"status": "error", "message": "无法连接到 Emby 服务器"}
    cfg.set("emby_host", data.emby_host.rstrip('/')); cfg.set("emby_api_key", data.emby_api_key)
    cfg.set("tmdb_api_key", data.tmdb_api_key); cfg.set("proxy_url", data.proxy_url); cfg.set("hidden_users", data.hidden_users)
    return {"status": "success", "message": "配置已保存"}

@app.get("/api/stats/dashboard")
def api_dashboard(user_id: Optional[str] = None):
    try:
        where, params = get_base_filter(user_id)
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)
        users = query_db(f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where} AND DateCreated > date('now', '-30 days')", params)
        dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
        base_stats = {"total_plays": plays[0]['c'], "active_users": users[0]['c'], "total_duration": dur[0]['c']}
        library_stats = {"movie": 0, "series": 0, "episode": 0}
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if key and host:
            try:
                res = requests.get(f"{host}/emby/Items/Counts?api_key={key}", timeout=2)
                if res.status_code == 200:
                    d = res.json(); library_stats = {"movie": d.get("MovieCount"), "series": d.get("SeriesCount"), "episode": d.get("EpisodeCount")}
            except: pass
        return {"status": "success", "data": {**base_stats, "library": library_stats}}
    except: return {"status": "error", "data": {"total_plays":0, "library": {}}}

@app.get("/api/stats/recent")
def api_recent_activity(user_id: Optional[str] = None):
    try:
        where, params = get_base_filter(user_id)
        sql = f"SELECT DateCreated, UserId, ItemId, ItemName, ItemType FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 1000"
        results = query_db(sql, params)
        if not results: return {"status": "success", "data": []}
        user_map = get_user_map(); final_data = []; seen_keys = set()
        for row in results:
            item = dict(row); item['UserName'] = user_map.get(item['UserId'], "User")
            clean_name = item['ItemName'].split(' - ')[0] if ' - ' in item['ItemName'] else item['ItemName']
            item['DisplayName'] = clean_name
            if item['ItemType'] == 'Episode':
                if clean_name in seen_keys: continue
                seen_keys.add(clean_name)
            final_data.append(item)
            if len(final_data) >= 20: break 
        return {"status": "success", "data": final_data}
    except: return {"status": "error", "data": []}

@app.get("/api/live")
def api_live_sessions():
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if not key or not host: return {"status": "error"}
    try:
        res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=2)
        if res.status_code == 200: return {"status": "success", "data": [s for s in res.json() if s.get("NowPlayingItem")]}
    except: pass
    return {"status": "success", "data": []}

@app.get("/api/stats/top_movies")
def api_top_movies(user_id: Optional[str] = None, category: str = 'all', sort_by: str = 'count'):
    try:
        where, params = get_base_filter(user_id)
        if category == 'Movie': where += " AND ItemType = 'Movie'"
        elif category == 'Episode': where += " AND ItemType = 'Episode'"
        sql = f"SELECT ItemName, ItemId, ItemType, PlayDuration FROM PlaybackActivity {where} LIMIT 2000"
        rows = query_db(sql, params); aggregated = {}
        for row in rows:
            clean = row['ItemName'].split(' - ')[0]
            if clean not in aggregated: aggregated[clean] = {'ItemName': clean, 'ItemId': row['ItemId'], 'PlayCount': 0, 'TotalTime': 0}
            aggregated[clean]['PlayCount'] += 1; aggregated[clean]['TotalTime'] += (row['PlayDuration'] or 0); aggregated[clean]['ItemId'] = row['ItemId']
        res = list(aggregated.values())
        res.sort(key=lambda x: x['TotalTime'] if sort_by == 'time' else x['PlayCount'], reverse=True)
        return {"status": "success", "data": res[:50]}
    except: return {"status": "error", "data": []}

@app.get("/api/stats/user_details")
def api_user_details(user_id: Optional[str] = None):
    try:
        where, params = get_base_filter(user_id)
        h_res = query_db(f"SELECT strftime('%H', DateCreated) as Hour, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Hour", params)
        h_data = {str(i).zfill(2): 0 for i in range(24)}
        if h_res: 
            for r in h_res: h_data[r['Hour']] = r['Plays']
        d_res = query_db(f"SELECT COALESCE(DeviceName, ClientName, 'Unknown') as Device, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Device ORDER BY Plays DESC LIMIT 10", params)
        l_res = query_db(f"SELECT DateCreated, ItemName, PlayDuration, COALESCE(DeviceName, ClientName) as Device, UserId FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 100", params)
        u_map = get_user_map(); logs = []
        if l_res:
            for r in l_res: l = dict(r); l['UserName'] = u_map.get(l['UserId'], "User"); logs.append(l)
        return {"status": "success", "data": {"hourly": h_data, "devices": [dict(r) for r in d_res] if d_res else [], "logs": logs}}
    except: return {"status": "error", "data": {"hourly": {}, "devices": [], "logs": []}}

@app.get("/api/stats/chart")
@app.get("/api/stats/trend")
def api_chart_stats(user_id: Optional[str] = None, dimension: str = 'day'):
    try:
        where, params = get_base_filter(user_id)
        sql = ""
        if dimension == 'week':
            where += " AND DateCreated > date('now', '-84 days')" 
            sql = f"SELECT strftime('%Y-W%W', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        elif dimension == 'month':
            where += " AND DateCreated > date('now', '-12 months')"
            sql = f"SELECT strftime('%Y-%m', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        else:
            where += " AND DateCreated > date('now', '-30 days')"
            sql = f"SELECT date(DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        results = query_db(sql, params); data = {}
        if results:
            for r in results: data[r['Label']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

@app.get("/api/stats/poster_data")
def api_poster_data(user_id: Optional[str] = None, period: str = 'all'):
    try:
        where_base, params = get_base_filter(user_id)
        date_filter = ""
        if period == 'week': date_filter = " AND DateCreated > date('now', '-7 days')"
        elif period == 'month': date_filter = " AND DateCreated > date('now', '-30 days')"
        elif period == 'year': date_filter = " AND DateCreated > date('now', '-1 year')"
        
        server_res = query_db(f"SELECT COUNT(*) as Plays FROM PlaybackActivity {get_base_filter('all')[0]} {date_filter}", get_base_filter('all')[1])
        server_plays = server_res[0]['Plays'] if server_res else 0
        raw_sql = f"SELECT ItemName, ItemId, ItemType, PlayDuration FROM PlaybackActivity {where_base + date_filter}"
        rows = query_db(raw_sql, params)
        total_plays = 0; total_duration = 0; aggregated = {} 
        if rows:
            for row in rows:
                total_plays += 1; dur = row['PlayDuration'] or 0; total_duration += dur; clean = row['ItemName'].split(' - ')[0]
                if clean not in aggregated: aggregated[clean] = {'ItemName': clean, 'ItemId': row['ItemId'], 'Count': 0, 'Duration': 0}
                aggregated[clean]['Count'] += 1; aggregated[clean]['Duration'] += dur; aggregated[clean]['ItemId'] = row['ItemId'] 
        top_list = list(aggregated.values()); top_list.sort(key=lambda x: x['Count'], reverse=True)
        return {"status": "success", "data": {"plays": total_plays, "hours": round(total_duration / 3600), "server_plays": server_plays, "top_list": top_list[:10], "tags": ["观影达人"]}}
    except: return {"status": "error", "data": {"plays": 0, "hours": 0}}

@app.get("/api/stats/top_users_list")
def api_top_users_list():
    try:
        res = query_db("SELECT UserId, COUNT(*) as Plays, SUM(PlayDuration) as TotalTime FROM PlaybackActivity GROUP BY UserId ORDER BY TotalTime DESC")
        if not res: return {"status": "success", "data": []}
        user_map = get_user_map(); hidden = cfg.get("hidden_users") or []; data = []
        for row in res:
            if row['UserId'] in hidden: continue
            u = dict(row); u['UserName'] = user_map.get(u['UserId'], f"User {str(u['UserId'])[:5]}"); data.append(u)
            if len(data) >= 5: break
        return {"status": "success", "data": data}
    except: return {"status": "success", "data": []}

# 🔥 修复：增加 Tag 支持，确保头像更新
@app.get("/api/proxy/image/{item_id}/{img_type}")
def proxy_image(item_id: str, img_type: str):
    target_id = item_id; key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if img_type == 'primary' and key:
        try:
            r = requests.get(f"{host}/emby/Items?Ids={item_id}&Fields=SeriesId,ParentId&Limit=1&api_key={key}", timeout=1)
            if r.status_code == 200:
                data = r.json()
                if data.get("Items"):
                    item = data["Items"][0]
                    if item.get('SeriesId'): target_id = item.get('SeriesId')
                    elif item.get('ParentId'): target_id = item.get('ParentId')
        except: pass
    suffix = "/Images/Backdrop?maxWidth=800" if img_type == 'backdrop' else "/Images/Primary?maxHeight=400"
    try:
        headers = {"Cache-Control": "public, max-age=31536000", "Access-Control-Allow-Origin": "*"}
        resp = requests.get(f"{host}/emby/Items/{target_id}{suffix}", timeout=3)
        if resp.status_code == 200: return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"), headers=headers)
    except: pass
    return RedirectResponse(FALLBACK_IMAGE_URL)

@app.get("/api/proxy/user_image/{user_id}")
def proxy_user_image(user_id: str, tag: Optional[str] = None):
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if not key: return Response(status_code=404)
    try:
        url = f"{host}/emby/Users/{user_id}/Images/Primary?width=200&height=200&mode=Crop"
        if tag: url += f"&tag={tag}"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            headers = {"Cache-Control": "public, max-age=31536000", "Access-Control-Allow-Origin": "*"}
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"), headers=headers)
    except: pass
    return Response(status_code=404)

@app.get("/api/stats/badges")
def api_badges(user_id: Optional[str] = None):
    try:
        where, params = get_base_filter(user_id); badges = []
        night_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '02' AND '05'", params)
        if night_res and night_res[0]['c'] > 5: badges.append({"id": "night", "name": "修仙党", "icon": "fa-moon", "color": "text-purple-500", "bg": "bg-purple-100", "desc": "深夜是灵魂最自由的时刻"})
        weekend_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%w', DateCreated) IN ('0', '6')", params)
        if weekend_res and weekend_res[0]['c'] > 10: badges.append({"id": "weekend", "name": "周末狂欢", "icon": "fa-champagne-glasses", "color": "text-pink-500", "bg": "bg-pink-100", "desc": "工作日唯唯诺诺，周末重拳出击"})
        dur_res = query_db(f"SELECT SUM(PlayDuration) as d FROM PlaybackActivity {where}", params)
        if dur_res and dur_res[0]['d'] and dur_res[0]['d'] > 360000: badges.append({"id": "liver", "name": "Emby肝帝", "icon": "fa-fire", "color": "text-red-500", "bg": "bg-red-100", "desc": "阅片无数"})
        return {"status": "success", "data": badges}
    except: return {"status": "success", "data": []}

@app.get("/api/stats/monthly_stats")
def api_monthly_stats(user_id: Optional[str] = None):
    try:
        where_base, params = get_base_filter(user_id)
        where = where_base + " AND DateCreated > date('now', '-12 months')"
        sql = f"SELECT strftime('%Y-%m', DateCreated) as Month, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Month ORDER BY Month"
        results = query_db(sql, params); data = {}
        if results: 
            for r in results: data[r['Month']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)