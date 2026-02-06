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
        # 只要配了 Token 就启动，功能开关在发送时判断
        if not cfg.get("tg_bot_token"): return
        
        self.running = True
        self._set_commands()
        
        # 1. 启动消息监听 (响应 /stats 指令)
        self.poll_thread = threading.Thread(target=self._polling_loop, daemon=True)
        self.poll_thread.start()
        
        # 2. 启动定时任务 (早报)
        self.schedule_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.schedule_thread.start()
        
        print("🤖 Bot Started (Webhook Mode)")

    def stop(self): 
        self.running = False

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

    # ================= 业务逻辑 =================

    # 🔥 1. 处理播放开始 (来自 Webhook)
    def push_playback_start(self, data):
        # 检查总开关
        if not cfg.get("enable_notify") or not cfg.get("tg_chat_id"): return
        
        try:
            chat_id = str(cfg.get("tg_chat_id"))
            user = data.get("User", {})
            item = data.get("Item", {})
            session = data.get("Session", {})
            
            user_name = user.get("Name", "未知用户")
            device_name = session.get("DeviceName", "未知设备")
            client_name = session.get("Client", "")
            
            # 标题处理
            title = item.get("Name", "")
            if item.get("SeriesName"):
                title = f"{item.get('SeriesName')} - {title}"
            
            # 构建消息
            msg = (
                f"▶️ <b>开始播放</b>\n\n"
                f"👤 用户：{user_name}\n"
                f"🎬 内容：{title}\n"
                f"📱 设备：{device_name} ({client_name})\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%H:%M:%S')}"
            )
            
            # 发送封面图 (如果有)
            item_id = item.get("Id")
            if item_id:
                img_io = self._download_emby_image(item_id, 'Primary') # 或者是 Backdrop
                if img_io:
                    self.send_photo(chat_id, img_io, msg)
                    return

            # 没图就发文字
            self.send_message(chat_id, msg)
            
        except Exception as e:
            print(f"推播放通知失败: {e}")

    # 🔥 2. 处理入库通知 (来自 Webhook)
    def push_new_media(self, item_id):
        # 检查入库通知专用开关
        if not cfg.get("enable_library_notify") or not cfg.get("tg_chat_id"): return
        
        chat_id = str(cfg.get("tg_chat_id"))
        host = cfg.get("emby_host")
        key = cfg.get("emby_api_key")

        # 等待 Emby 刮削元数据 (5秒)
        time.sleep(5) 

        try:
            # 主动查询详情
            url = f"{host}/emby/Items/{item_id}?api_key={key}"
            res = requests.get(url, timeout=10)
            if res.status_code != 200: return
            
            item = res.json()
            
            # 提取信息
            name = item.get("Name", "")
            type_raw = item.get("Type", "Movie")
            overview = item.get("Overview", "暂无简介...")
            community_rating = item.get("CommunityRating", "N/A")
            
            if len(overview) > 150: overview = overview[:145] + "..."

            # 格式化标题
            type_cn = "电影"
            display_title = name
            
            if type_raw == "Episode":
                type_cn = "剧集"
                series_name = item.get("SeriesName", "")
                season_idx = item.get("ParentIndexNumber", 1)
                episode_idx = item.get("IndexNumber", 1)
                season_str = f"S{str(season_idx).zfill(2)}"
                episode_str = f"E{str(episode_idx).zfill(2)}"
                display_title = f"{series_name} {season_str} {episode_str}"
                if name and name != f"Episode {episode_idx}": display_title += f" {name}"
                    
            elif type_raw == "Season": return 
                
            current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            caption = (
                f"📺 <b>新入库 {type_cn} {display_title}</b>\n"
                f"⭐ 评分：{community_rating}/10 ｜ 📚 类型：{type_cn}\n"
                f"🕒 时间：{current_time}\n\n"
                f"📝 剧情：{overview}"
            )

            # 发送
            if item.get("ImageTags", {}).get("Primary"):
                img_io = self._download_emby_image(item_id, 'Primary')
                if img_io:
                    self.send_photo(chat_id, img_io, caption)
                else:
                    self.send_message(chat_id, caption)
            else:
                self.send_message(chat_id, caption)

        except Exception as e:
            print(f"推入库通知失败: {e}")

    # ================= 基础功能 =================

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
                            # 简单的鉴权
                            if admin_id and chat_id != admin_id: continue 
                            self._handle_message(u["message"], admin_id)
                else: time.sleep(5)
            except: time.sleep(5)

    def _handle_message(self, msg, admin_id):
        chat_id = str(msg.get("chat", {}).get("id"))
        text = msg.get("text", "").strip()
        if text.startswith("/stats"): self._cmd_stats(chat_id)
        elif text.startswith("/now"): self._cmd_now(chat_id)

    def _scheduler_loop(self):
        while self.running:
            try:
                now = datetime.datetime.now()
                if now.minute != self.last_check_min:
                    self.last_check_min = now.minute
                    # 每天早上9点推送日报 + 检查过期用户
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
        # 注意：这里改成了查询 API，因为不再维护 active_sessions 缓存
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            res = requests.get(f"{host}/emby/Sessions?api_key={key}")
            count = len([s for s in res.json() if s.get("NowPlayingItem")])
            self.send_message(chat_id, f"🟢 当前有 {count} 个正在播放的会话")
        except:
            self.send_message(chat_id, "❌ 无法连接到 Emby 服务器")

bot = TelegramBot()