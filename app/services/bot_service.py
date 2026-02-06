import threading
import time
import requests
import datetime
import io
from app.core.config import cfg, REPORT_COVER_URL
from app.core.database import query_db, get_base_filter
from app.services.report_service import report_gen, HAS_PIL

class TelegramBot:
    def __init__(self):
        self.running = False
        self.poll_thread = None
        self.schedule_thread = None 
        self.offset = 0
        self.last_check_min = -1
        
    def start(self):
        if self.running: return
        if not cfg.get("tg_bot_token"): return
        
        self.running = True
        self._set_commands()
        
        # 启动轮询和定时任务
        self.poll_thread = threading.Thread(target=self._polling_loop, daemon=True)
        self.poll_thread.start()
        
        self.schedule_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.schedule_thread.start()
        
        print("🤖 Bot Started (Full Features)")

    def stop(self): 
        self.running = False

    def _get_proxies(self):
        proxy = cfg.get("proxy_url")
        return {"http": proxy, "https": proxy} if proxy else None

    def _download_emby_image(self, item_id, img_type='Primary'):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return None
        try:
            # 增加 maxRequest 限制避免下载原图过大
            url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=1200&quality=90&api_key={key}"
            res = requests.get(url, timeout=15)
            if res.status_code == 200: return io.BytesIO(res.content)
        except Exception as e:
            print(f"下载图片失败: {e}")
        return None

    # ================= 发送消息封装 =================
    def send_photo(self, chat_id, photo_io, caption, parse_mode="HTML"):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode}
            
            if isinstance(photo_io, str): # URL
                data['photo'] = photo_io
                requests.post(url, data=data, proxies=self._get_proxies(), timeout=20)
            else: # BytesIO
                photo_io.seek(0)
                files = {"photo": ("image.jpg", photo_io, "image/jpeg")}
                requests.post(url, data=data, files=files, proxies=self._get_proxies(), timeout=30)
        except Exception as e: 
            print(f"Bot Photo Error: {e}")
            self.send_message(chat_id, caption) # 降级为发文字

    def send_message(self, chat_id, text, parse_mode="HTML"):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}, proxies=self._get_proxies(), timeout=10)
        except Exception as e: print(f"Bot Send Error: {e}")

    # ================= Webhook 触发逻辑 =================

    # 1. 播放开始
    def push_playback_start(self, data):
        if not cfg.get("enable_notify") or not cfg.get("tg_chat_id"): return
        
        try:
            chat_id = str(cfg.get("tg_chat_id"))
            user = data.get("User", {})
            item = data.get("Item", {})
            session = data.get("Session", {})
            
            user_name = user.get("Name", "未知用户")
            device_name = session.get("DeviceName", "未知设备")
            client_name = session.get("Client", "")
            title = item.get("Name", "")
            if item.get("SeriesName"): title = f"{item.get('SeriesName')} - {title}"
            
            msg = (
                f"▶️ <b>开始播放</b>\n\n"
                f"👤 用户：{user_name}\n"
                f"🎬 内容：{title}\n"
                f"📱 设备：{device_name} ({client_name})\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%H:%M:%S')}"
            )
            
            item_id = item.get("Id")
            if item_id:
                # 尝试下载背景图或海报
                img_io = self._download_emby_image(item_id, 'Backdrop') 
                if not img_io: img_io = self._download_emby_image(item_id, 'Primary')
                
                if img_io:
                    self.send_photo(chat_id, img_io, msg)
                    return

            self.send_message(chat_id, msg)
        except Exception as e: print(f"Push Playback Error: {e}")

    # 2. 新资源入库
    def push_new_media(self, item_id):
        if not cfg.get("enable_library_notify") or not cfg.get("tg_chat_id"): return
        
        chat_id = str(cfg.get("tg_chat_id"))
        host = cfg.get("emby_host"); key = cfg.get("emby_api_key")

        time.sleep(5) # 等待刮削

        try:
            url = f"{host}/emby/Items/{item_id}?api_key={key}"
            res = requests.get(url, timeout=10)
            if res.status_code != 200: return
            
            item = res.json()
            name = item.get("Name", "")
            type_raw = item.get("Type", "Movie")
            overview = item.get("Overview", "暂无简介...")
            rating = item.get("CommunityRating", "N/A")
            
            if len(overview) > 150: overview = overview[:145] + "..."

            type_cn = "电影"
            display_title = name
            
            if type_raw == "Episode":
                type_cn = "剧集"
                s_name = item.get("SeriesName", "")
                s_idx = item.get("ParentIndexNumber", 1)
                e_idx = item.get("IndexNumber", 1)
                display_title = f"{s_name} S{str(s_idx).zfill(2)} E{str(e_idx).zfill(2)}"
                if name and "Episode" not in name: display_title += f" {name}"
            elif type_raw == "Season": return 
                
            caption = (
                f"📺 <b>新入库 {type_cn}</b>\n{display_title}\n\n"
                f"⭐ 评分：{rating}/10\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"📝 剧情：{overview}"
            )

            # 优先下载海报
            img_io = self._download_emby_image(item_id, 'Primary')
            if img_io:
                self.send_photo(chat_id, img_io, caption)
            else:
                self.send_message(chat_id, caption)

        except Exception as e: print(f"Push New Media Error: {e}")

    # ================= 机器人指令系统 =================

    def _set_commands(self):
        token = cfg.get("tg_bot_token")
        # 🔥 恢复所有指令
        commands = [
            {"command": "stats", "description": "📊 今日日报"},
            {"command": "now", "description": "🟢 正在播放"},
            {"command": "recent", "description": "📜 最近播放"},
            {"command": "check", "description": "📡 连接检查"}
        ]
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
                            # 简单的白名单鉴权
                            if admin_id and chat_id != admin_id: 
                                self.send_message(chat_id, "🚫 您无权使用此机器人")
                                continue 
                            self._handle_message(u["message"], chat_id)
                else: time.sleep(5)
            except: time.sleep(5)

    def _handle_message(self, msg, chat_id):
        text = msg.get("text", "").strip()
        # 路由分发
        if text.startswith("/stats"): self._cmd_stats(chat_id)
        elif text.startswith("/now"): self._cmd_now(chat_id)
        elif text.startswith("/recent"): self._cmd_recent(chat_id) # 🔥 恢复
        elif text.startswith("/check"): self._cmd_check(chat_id)   # 🔥 恢复

    def _scheduler_loop(self):
        while self.running:
            try:
                now = datetime.datetime.now()
                if now.minute != self.last_check_min:
                    self.last_check_min = now.minute
                    if now.hour == 9 and now.minute == 0:
                        self._check_user_expiration()
                        if cfg.get("tg_chat_id") and cfg.get("enable_bot"):
                            self._cmd_stats(str(cfg.get("tg_chat_id")))
                time.sleep(5)
            except: time.sleep(60)

    def _check_user_expiration(self):
        users = query_db("SELECT user_id, expire_date FROM users_meta WHERE expire_date IS NOT NULL AND expire_date != ''")
        if not users: return
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        for u in users:
            if u['expire_date'] < today:
                try: requests.post(f"{host}/emby/Users/{u['user_id']}/Policy?api_key={key}", json={"IsDisabled": True})
                except: pass

    # ================= 指令实现 =================

    def _cmd_stats(self, chat_id):
        if HAS_PIL:
            img = report_gen.generate_report('all', 'day')
            self.send_photo(chat_id, img, "📊 <b>今日媒体日报</b>")
        else:
            where, params = get_base_filter('all')
            plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day')", params)[0]['c']
            self.send_photo(chat_id, REPORT_COVER_URL, f"📊 <b>今日日报</b>\n▶️ 总播放量: {plays} 次")

    def _cmd_now(self, chat_id):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=5)
            sessions = [s for s in res.json() if s.get("NowPlayingItem")]
            if not sessions:
                self.send_message(chat_id, "🟢 当前没有正在播放的会话")
                return
            
            msg = f"🟢 <b>正在播放 ({len(sessions)})</b>\n"
            for s in sessions:
                user = s.get('UserName')
                title = s['NowPlayingItem'].get('Name')
                device = s.get('DeviceName')
                msg += f"\n👤 {user}\n📺 {title}\n📱 {device}\n"
            self.send_message(chat_id, msg)
        except: self.send_message(chat_id, "❌ 无法连接 Emby 服务器")

    # 🔥 恢复：最近播放
    def _cmd_recent(self, chat_id):
        try:
            rows = query_db("SELECT user_name, item_name, date_created FROM PlaybackActivity ORDER BY date_created DESC LIMIT 5")
            if not rows:
                self.send_message(chat_id, "📭 暂无播放记录")
                return
            
            msg = "📜 <b>最近 5 条播放记录</b>\n"
            for r in rows:
                t = r['date_created'].split('T')[1][:5] # 取时间 HH:MM
                msg += f"\n⏰ {t} | {r['user_name']}\n🎬 {r['item_name']}\n"
            self.send_message(chat_id, msg)
        except Exception as e: self.send_message(chat_id, f"❌ 查询失败: {str(e)}")

    # 🔥 恢复：连接检查
    def _cmd_check(self, chat_id):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        start = time.time()
        try:
            res = requests.get(f"{host}/emby/System/Info?api_key={key}", timeout=5)
            latency = int((time.time() - start) * 1000)
            if res.status_code == 200:
                info = res.json()
                msg = (
                    f"✅ <b>Emby 连接正常</b>\n\n"
                    f"📡 延迟: {latency}ms\n"
                    f"🖥️ 服务器: {info.get('ServerName')}\n"
                    f"📦 版本: {info.get('Version')}\n"
                    f"🏠 局域网: {info.get('LocalAddress')}"
                )
                self.send_message(chat_id, msg)
            else:
                self.send_message(chat_id, f"⚠️ 连接失败: HTTP {res.status_code}")
        except Exception as e:
            self.send_message(chat_id, f"❌ 连接错误: {str(e)}")

    def push_now(self, user_id, period, theme):
        if not cfg.get("tg_chat_id"): return False
        if HAS_PIL:
            img = report_gen.generate_report(user_id, period, theme)
            self.send_photo(str(cfg.get("tg_chat_id")), img, f"🚀 <b>立即推送</b>")
        else:
            self._cmd_stats(str(cfg.get("tg_chat_id")))
        return True

bot = TelegramBot()