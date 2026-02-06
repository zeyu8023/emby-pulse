import threading
import time
import requests
import datetime
import io
import re
from app.core.config import cfg, REPORT_COVER_URL
from app.core.database import query_db, get_base_filter
from app.services.report_service import report_gen, HAS_PIL
import logging

# 初始化 Logger
logger = logging.getLogger("uvicorn")

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
        if not cfg.get("enable_bot") or not cfg.get("tg_bot_token"): return
        self.running = True
        self._set_commands()
        self.poll_thread = threading.Thread(target=self._polling_loop, daemon=True); self.poll_thread.start()
        if cfg.get("enable_notify"):
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True); self.monitor_thread.start()
        self.schedule_thread = threading.Thread(target=self._scheduler_loop, daemon=True); self.schedule_thread.start()
        print("🤖 Bot Started!")

    def stop(self): self.running = False

    def _get_proxies(self):
        proxy = cfg.get("proxy_url")
        return {"http": proxy, "https": proxy} if proxy else None

    def _download_emby_image(self, item_id, img_type='Primary'):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return None
        try:
            url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=1200&quality=90&api_key={key}"
            res = requests.get(url, timeout=10)
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
                data['photo'] = photo_io
                requests.post(url, data=data, proxies=self._get_proxies(), timeout=20)
            else:
                photo_io.seek(0)
                files = {"photo": ("image.jpg", photo_io, "image/jpeg")}
                requests.post(url, data=data, files=files, proxies=self._get_proxies(), timeout=20)
        except Exception as e: 
            print(f"Bot Photo Error: {e}")
            self.send_message(chat_id, caption)

    def send_message(self, chat_id, text, parse_mode="HTML"):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}, proxies=self._get_proxies(), timeout=10)
        except Exception as e: print(f"Bot Send Error: {e}")

    def _set_commands(self):
        token = cfg.get("tg_bot_token")
        commands = [{"command": "stats", "description": "📊 日报"}, {"command": "now", "description": "🟢 状态"}]
        try: requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", json={"commands": commands}, proxies=self._get_proxies(), timeout=10)
        except: pass

    def _polling_loop(self):
        token = cfg.get("tg_bot_token"); admin_id = str(cfg.get("tg_chat_id"))
        while self.running:
            try:
                url = f"https://api.telegram.org/bot{token}/getUpdates"
                params = {"offset": self.offset, "timeout": 30}
                res = requests.get(url, params=params, proxies=self._get_proxies(), timeout=35)
                if res.status_code == 200:
                    for u in res.json().get("result", []):
                        self.offset = u["update_id"] + 1
                        if "message" in u: 
                            chat_id = str(u["message"]["chat"]["id"])
                            if admin_id and chat_id != admin_id: self.send_message(chat_id, "🚫 Denied"); continue
                            self._handle_message(u["message"], admin_id)
                else: time.sleep(5)
            except: time.sleep(5)

    def _handle_message(self, msg, admin_id):
        chat_id = str(msg.get("chat", {}).get("id"))
        text = msg.get("text", "").strip()
        if text.startswith("/stats"): self._cmd_stats(chat_id)
        elif text.startswith("/now"): self._cmd_now(chat_id)

    def _monitor_loop(self):
        admin_id = str(cfg.get("tg_chat_id"))
        while self.running and cfg.get("enable_notify"):
            try:
                key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
                if not key or not host: time.sleep(30); continue
                res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=5)
                if res.status_code == 200:
                    current_ids = []
                    for s in res.json():
                        if s.get("NowPlayingItem"):
                            sid = s.get("Id"); current_ids.append(sid)
                            if sid not in self.active_sessions:
                                msg = f"▶️ <b>{s.get('UserName')}</b>\n📺 {s['NowPlayingItem'].get('Name')}"
                                self.send_message(admin_id, msg)
                                self.active_sessions[sid] = True
                    stopped = [sid for sid in self.active_sessions if sid not in current_ids]
                    for sid in stopped: del self.active_sessions[sid]
                time.sleep(10)
            except: time.sleep(10)

    def _scheduler_loop(self):
        while self.running:
            try:
                now = datetime.datetime.now()
                if now.minute != self.last_check_min:
                    self.last_check_min = now.minute
                    if now.hour == 9 and now.minute == 0:
                        self._check_user_expiration()
                        if cfg.get("tg_chat_id"): self._cmd_stats(str(cfg.get("tg_chat_id")))
                time.sleep(5)
            except: time.sleep(60)

    def _check_user_expiration(self):
        users = query_db("SELECT user_id, expire_date FROM users_meta WHERE expire_date IS NOT NULL AND expire_date != ''")
        if not users: return
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        for u in users:
            if u['expire_date'] < today:
                try:
                    policy = {"IsDisabled": True}
                    requests.post(f"{host}/emby/Users/{u['user_id']}/Policy?api_key={key}", json=policy)
                except: pass

    def push_now(self, user_id, period, theme):
        if not cfg.get("tg_chat_id"): return False
        if HAS_PIL:
            img = report_gen.generate_report(user_id, period, theme)
            self.send_photo(str(cfg.get("tg_chat_id")), img, f"🚀 <b>立即推送</b>")
        else:
            self._cmd_stats(str(cfg.get("tg_chat_id")))
        return True

    # 🔥 新增：入库通知推送逻辑
    def push_new_media(self, item_id):
        if not cfg.get("enable_library_notify") or not cfg.get("tg_chat_id"): return
        
        chat_id = str(cfg.get("tg_chat_id"))
        host = cfg.get("emby_host")
        key = cfg.get("emby_api_key")

        # 1. 稍微等待几秒，确保 Emby 完成了元数据刮削
        time.sleep(5) 

        try:
            # 2. 主动查询 Emby API 获取详情
            url = f"{host}/emby/Items/{item_id}?api_key={key}"
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                print(f"查询 Item 详情失败: {res.text}")
                return
            
            item = res.json()
            
            # 3. 提取并格式化信息
            name = item.get("Name", "")
            type_raw = item.get("Type", "Movie")
            overview = item.get("Overview", "暂无简介...")
            community_rating = item.get("CommunityRating", "N/A")
            
            # 截断过长的简介
            if len(overview) > 150:
                overview = overview[:145] + "..."

            # 类型与标题处理
            type_cn = "电影"
            display_title = name
            
            if type_raw == "Episode":
                type_cn = "剧集"
                series_name = item.get("SeriesName", "")
                season_idx = item.get("ParentIndexNumber", 1)
                episode_idx = item.get("IndexNumber", 1)
                # 格式化 S01 E01
                season_str = f"S{str(season_idx).zfill(2)}"
                episode_str = f"E{str(episode_idx).zfill(2)}"
                display_title = f"{series_name} {season_str} {episode_str}"
                
                # 如果单集有特定标题且不等于集数，加上
                if name and name != f"Episode {episode_idx}" and name != f"Episode {episode_idx}": 
                    display_title += f" {name}"
                    
            elif type_raw == "Season":
                return # 季度入库不发
                
            # 4. 构建消息文本
            current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            caption = (
                f"📺 <b>新入库 {type_cn} {display_title}</b>\n"
                f"⭐ 评分：{community_rating}/10 ｜ 📚 类型：{type_cn}\n"
                f"🕒 时间：{current_time}\n\n"
                f"📝 剧情：{overview}"
            )

            # 5. 获取图片并发送
            # 优先使用 Primary 图片
            if item.get("ImageTags", {}).get("Primary"):
                img_io = self._download_emby_image(item_id, 'Primary')
                if img_io:
                    self.send_photo(chat_id, img_io, caption)
                else:
                    self.send_message(chat_id, caption)
            else:
                self.send_message(chat_id, caption)
                
            print(f"入库通知已发送: {display_title}")

        except Exception as e:
            print(f"发送入库通知异常: {e}")

    def _cmd_stats(self, chat_id):
        if HAS_PIL:
            img = report_gen.generate_report('all', 'day')
            self.send_photo(chat_id, img, "📊 <b>今日日报</b>")
        else:
            where, params = get_base_filter('all')
            plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day')", params)[0]['c']
            msg = f"📊 <b>今日日报</b>\n▶️ 播放: {plays} 次"
            self.send_photo(chat_id, REPORT_COVER_URL, msg)

    def _cmd_now(self, chat_id):
        self.send_message(chat_id, f"🟢 {len(self.active_sessions)} 个会话正在播放")

bot = TelegramBot()