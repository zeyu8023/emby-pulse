import threading
import time
import requests
import datetime
import io
import json
import logging
from app.core.config import cfg, REPORT_COVER_URL, FALLBACK_IMAGE_URL
from app.core.database import query_db, get_base_filter
from app.services.report_service import report_gen, HAS_PIL

# 初始化 Logger
logger = logging.getLogger("uvicorn")

class TelegramBot:
    def __init__(self):
        self.running = False
        self.poll_thread = None
        self.schedule_thread = None 
        self.offset = 0
        self.last_check_min = -1
        # 缓存正在播放的会话 ID
        self.active_sessions = {}
        
    def start(self):
        """启动机器人服务"""
        if self.running: return
        if not cfg.get("tg_bot_token"): return
        
        self.running = True
        self._set_commands()
        
        # 1. 消息轮询线程
        self.poll_thread = threading.Thread(target=self._polling_loop, daemon=True)
        self.poll_thread.start()
        
        # 2. 定时任务线程
        self.schedule_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.schedule_thread.start()
        
        print("🤖 Bot Service Started (Robust Mode)")

    def stop(self): 
        self.running = False

    def _get_proxies(self):
        proxy = cfg.get("proxy_url")
        return {"http": proxy, "https": proxy} if proxy else None

    # ================= 工具方法 =================

    def _get_location(self, ip):
        """查询 IP 归属地"""
        if not ip or ip in ['127.0.0.1', '::1', '0.0.0.0']: return "本地连接"
        try:
            res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=3)
            if res.status_code == 200:
                d = res.json()
                if d.get('status') == 'success':
                    return f"{d.get('country')} {d.get('regionName')} {d.get('city')}"
        except: pass
        return "未知位置"

    def _download_emby_image(self, item_id, img_type='Primary'):
        """下载图片流 (带重试机制)"""
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return None
        try:
            url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=1200&quality=90&api_key={key}"
            res = requests.get(url, timeout=15)
            if res.status_code == 200: return io.BytesIO(res.content)
        except Exception as e:
            logger.error(f"Image Download Failed: {e}")
        return None

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
            logger.error(f"Bot Send Photo Error: {e}")
            # 图片发送失败，降级发文字
            self.send_message(chat_id, caption)

    def send_message(self, chat_id, text, parse_mode="HTML"):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}, proxies=self._get_proxies(), timeout=10)
        except Exception as e: 
            logger.error(f"Bot Send Message Error: {e}")

    # ================= 核心业务逻辑 =================

    def save_playback_activity(self, data):
        """
        🔥 修复核心：将播放记录写入数据库
        """
        try:
            user = data.get("User", {})
            item = data.get("Item", {})
            session = data.get("Session", {})
            
            # 提取关键字段
            user_id = user.get("Id")
            user_name = user.get("Name")
            item_id = item.get("Id")
            item_name = item.get("Name")
            item_type = item.get("Type")
            device_name = session.get("DeviceName")
            client = session.get("Client")
            
            # 使用 UTC 时间或当前时间
            date_created = datetime.datetime.now().isoformat()
            
            # 存入 SQLite
            if user_id and item_id:
                sql = """
                INSERT INTO PlaybackActivity 
                (user_id, user_name, item_id, item_name, item_type, device_name, client, date_created) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """
                query_db(sql, (user_id, user_name, item_id, item_name, item_type, device_name, client, date_created))
                logger.info(f"✅ 播放记录已入库: {user_name} - {item_name}")
            else:
                logger.warning("⚠️ 播放数据不完整，未入库")
                
        except Exception as e:
            logger.error(f"❌ 写入播放记录失败: {e}")

    def push_playback_event(self, data, action="start"):
        """处理播放/停止通知"""
        if not cfg.get("enable_notify") or not cfg.get("tg_chat_id"): return
        
        try:
            chat_id = str(cfg.get("tg_chat_id"))
            user = data.get("User", {})
            item = data.get("Item", {})
            session = data.get("Session", {})
            
            user_name = user.get("Name", "未知用户")
            device_name = session.get("DeviceName", "未知设备")
            client_name = session.get("Client", "Emby")
            ip_address = session.get("RemoteEndPoint", "127.0.0.1")
            
            # 标题拼接
            title = item.get("Name", "未知内容")
            if item.get("SeriesName"): 
                idx = item.get("IndexNumber", 0)
                parent_idx = item.get("ParentIndexNumber", 1)
                title = f"{item.get('SeriesName')} S{str(parent_idx).zfill(2)}E{str(idx).zfill(2)} {title}"

            type_cn = "剧集" if item.get("Type") == "Episode" else "电影"
            
            # 🔥 进度计算双重保险
            ticks = data.get("PlaybackPositionTicks")
            if ticks is None: # 如果根节点没有，去 Session 里找
                ticks = session.get("PlayState", {}).get("PositionTicks")
            
            if ticks is None: ticks = 0 # 还没开始
            
            total_ticks = item.get("RunTimeTicks", 1)
            progress_text = "0%"
            if total_ticks and total_ticks > 0:
                pct = (ticks / total_ticks) * 100
                progress_text = f"{pct:.2f}%"

            emoji = "▶️" if action == "start" else "⏹️"
            action_text = "开始播放" if action == "start" else "停止播放"
            location = self._get_location(ip_address)

            msg = (
                f"{emoji} <b>【{user_name}】{action_text}</b>\n"
                f"📺 {title}\n"
                f"📚 类型：{type_cn}\n"
                f"🔄 进度：{progress_text}\n"
                f"🌐 地址：{ip_address} ({location})\n"
                f"📱 设备：{client_name} on {device_name}\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            # 尝试获取图片
            item_id = item.get("Id")
            img_io = None
            if item_id:
                img_io = self._download_emby_image(item_id, 'Backdrop') 
                if not img_io: img_io = self._download_emby_image(item_id, 'Primary')
            
            if img_io:
                self.send_photo(chat_id, img_io, msg)
            else:
                self.send_message(chat_id, msg)
            
        except Exception as e:
            logger.error(f"Push Playback Error: {e}")

    def push_new_media(self, item_id, fallback_item=None):
        """
        处理入库通知 (带兜底逻辑)
        :param item_id: 媒体 ID
        :param fallback_item: Webhook 里的原始数据 (如果 API 查不到就用这个)
        """
        if not cfg.get("enable_library_notify") or not cfg.get("tg_chat_id"): return
        
        chat_id = str(cfg.get("tg_chat_id"))
        host = cfg.get("emby_host"); key = cfg.get("emby_api_key")

        # 1. 尝试从 API 获取详情 (最多重试 3 次)
        api_item = None
        for i in range(3):
            wait_time = 10 + (i * 15)
            logger.info(f"⏳ 等待入库扫描 ({wait_time}s)... [第{i+1}次]")
            time.sleep(wait_time) 

            try:
                res = requests.get(f"{host}/emby/Items/{item_id}?api_key={key}", timeout=10)
                if res.status_code == 200:
                    api_item = res.json()
                    # 检查是否有图，如果没图且不是最后一次，继续等
                    if not api_item.get("ImageTags", {}).get("Primary") and i < 2:
                        logger.warning(f"⚠️ 获取详情成功但无图，继续等待...")
                        continue
                    break
                else:
                    logger.warning(f"⚠️ 获取详情失败 HTTP {res.status_code}，重试中...")
            except Exception as e:
                logger.error(f"❌ 请求 Emby API 出错: {e}")

        # 2. 决定使用的数据源 (API 优先，Webhook 原始数据兜底)
        final_item = api_item if api_item else fallback_item
        
        if not final_item:
            logger.error(f"❌ 彻底失败：API 查不到且无 Webhook 原始数据，放弃推送。")
            return

        if not api_item:
            logger.warning(f"⚠️ API 获取失败，启用 Webhook 原始数据进行兜底推送！")

        try:
            name = final_item.get("Name", "未知标题")
            type_raw = final_item.get("Type", "Movie")
            overview = final_item.get("Overview", "暂无简介...")
            rating = final_item.get("CommunityRating", "N/A")
            year = final_item.get("ProductionYear", "")
            
            if len(overview) > 150: overview = overview[:145] + "..."

            type_cn = "电影"
            display_title = name
            
            if type_raw == "Episode":
                type_cn = "剧集"
                s_name = final_item.get("SeriesName", "")
                s_idx = final_item.get("ParentIndexNumber", 1)
                e_idx = final_item.get("IndexNumber", 1)
                display_title = f"{s_name} S{str(s_idx).zfill(2)}E{str(e_idx).zfill(2)}"
                if name and "Episode" not in name: display_title += f" {name}"
            elif type_raw == "Series":
                type_cn = "剧集"
                
            caption = (
                f"📺 <b>新入库 {type_cn}</b>\n{display_title} ({year})\n\n"
                f"⭐ 评分：{rating}/10\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"📝 剧情：{overview}"
            )

            # 3. 发送图片 (如果 API 查到了就用 API 的图，否则用默认图)
            img_io = None
            if api_item:
                img_io = self._download_emby_image(item_id, 'Primary')
            
            if img_io:
                self.send_photo(chat_id, img_io, caption)
            else:
                logger.info("⚠️ 无有效封面图，使用默认海报推送")
                self.send_photo(chat_id, REPORT_COVER_URL, caption)

        except Exception as e: 
            logger.error(f"Push New Media Error: {e}")

    # ================= 指令系统 =================

    def _set_commands(self):
        token = cfg.get("tg_bot_token")
        cmds = [
            {"command": "stats", "description": "📊 超级日报"},
            {"command": "now", "description": "🟢 正在播放"},
            {"command": "latest", "description": "🆕 最近入库"},
            {"command": "recent", "description": "📜 播放历史"},
            {"command": "check", "description": "📡 系统检查"},
            {"command": "help", "description": "🤖 帮助菜单"}
        ]
        try: requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", json={"commands": cmds}, proxies=self._get_proxies(), timeout=10)
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
                            if admin_id and chat_id != admin_id: continue 
                            self._handle_message(u["message"], chat_id)
                else: time.sleep(5)
            except: time.sleep(5)

    def _handle_message(self, msg, chat_id):
        text = msg.get("text", "").strip()
        if text.startswith("/stats"): self._cmd_stats(chat_id)
        elif text.startswith("/now"): self._cmd_now(chat_id)
        elif text.startswith("/latest"): self._cmd_latest(chat_id)
        elif text.startswith("/recent"): self._cmd_recent(chat_id)
        elif text.startswith("/check"): self._cmd_check(chat_id)
        elif text.startswith("/help"): self._cmd_help(chat_id)

    # 1. 超级日报
    def _cmd_stats(self, chat_id):
        # 统计数据
        where, params = get_base_filter('all')
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day')", params)[0]['c']
        
        # 活跃用户 (排除空用户)
        users = query_db(f"SELECT DISTINCT user_name FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day') AND user_name IS NOT NULL AND user_name != ''", params)
        user_list = ", ".join([u['user_name'] for u in users]) if users else "暂无"

        caption = f"📊 <b>今日媒体日报</b>\n\n▶️ 今日播放：{plays} 次\n👥 活跃用户：{user_list}\n"

        if HAS_PIL:
            img = report_gen.generate_report('all', 'day')
            self.send_photo(chat_id, img, caption)
        else:
            self.send_photo(chat_id, REPORT_COVER_URL, caption)

    # 2. 正在播放
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
                
                ticks = s.get('PlayState', {}).get('PositionTicks', 0)
                total = s['NowPlayingItem'].get('RunTimeTicks', 1)
                pct = int((ticks / total) * 100) if total > 0 else 0
                
                msg += f"\n👤 <b>{user}</b> | 📱 {device}\n📺 {title}\n🔄 进度: {pct}%\n"
            self.send_message(chat_id, msg)
        except: self.send_message(chat_id, "❌ 无法连接 Emby 服务器")

    # 3. 最近入库
    def _cmd_latest(self, chat_id):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            url = f"{host}/emby/Items?SortBy=DateCreated&SortOrder=Descending&IncludeItemTypes=Movie,Episode&Limit=5&Recursive=true&api_key={key}"
            res = requests.get(url, timeout=10)
            items = res.json().get("Items", [])
            
            if not items:
                self.send_message(chat_id, "📭 最近没有新内容入库")
                return

            msg = "🆕 <b>最近入库 Top 5</b>\n"
            for item in items:
                name = item.get("Name")
                if item.get("SeriesName"): name = f"{item.get('SeriesName')} - {name}"
                date = item.get("DateCreated", "")[:10]
                msg += f"\n📅 {date} | {name}"
            
            self.send_message(chat_id, msg)
        except Exception as e: self.send_message(chat_id, f"❌ 查询失败: {str(e)}")

    # 4. 播放历史
    def _cmd_recent(self, chat_id):
        try:
            rows = query_db("SELECT user_name, item_name, date_created FROM PlaybackActivity ORDER BY date_created DESC LIMIT 10")
            if not rows:
                self.send_message(chat_id, "📭 暂无播放记录")
                return
            
            msg = "📜 <b>最近 10 条播放记录</b>\n"
            for r in rows:
                try:
                    date = r['date_created'].split('T')[0][5:]
                    time_part = r['date_created'].split('T')[1][:5]
                except:
                    date = r['date_created']; time_part = ""
                msg += f"\n⏰ {date} {time_part} | {r['user_name']}\n🎬 {r['item_name']}\n"
            self.send_message(chat_id, msg)
        except Exception as e: self.send_message(chat_id, f"❌ 查询失败: {str(e)}")

    # 5. 系统检查
    def _cmd_check(self, chat_id):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        start = time.time()
        try:
            res = requests.get(f"{host}/emby/System/Info?api_key={key}", timeout=5)
            latency = int((time.time() - start) * 1000)
            if res.status_code == 200:
                info = res.json()
                
                # 兼容不同版本 IP 字段
                local = info.get('LocalAddress')
                if not local and info.get('LocalAddresses'): local = info['LocalAddresses'][0]
                
                wan = info.get('WanAddress')
                if not wan and info.get('RemoteAddresses'): wan = info['RemoteAddresses'][0]

                msg = (
                    f"✅ <b>系统连接正常</b>\n\n"
                    f"📡 延迟: {latency}ms\n"
                    f"🖥️ Emby: {info.get('ServerName')} ({info.get('Version')})\n"
                    f"🏠 内网: {local or '未知'}\n"
                    f"🌍 外网: {wan or '未知'}"
                )
                self.send_message(chat_id, msg)
            else: self.send_message(chat_id, f"⚠️ 连接异常: HTTP {res.status_code}")
        except Exception as e: self.send_message(chat_id, f"❌ 连接错误: {str(e)}")

    def _cmd_help(self, chat_id):
        msg = (
            "🤖 <b>EmbyPulse 机器人助手</b>\n\n"
            "/stats - 查看今日日报 (图表)\n"
            "/now - 查看正在播放的会话\n"
            "/latest - 查看最近入库的影片\n"
            "/recent - 查看最近播放记录\n"
            "/check - 检查服务器连接状态\n"
        )
        self.send_message(chat_id, msg)

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
    
    def push_now(self, user_id, period, theme):
        if not cfg.get("tg_chat_id"): return False
        if HAS_PIL:
            img = report_gen.generate_report(user_id, period, theme)
            self.send_photo(str(cfg.get("tg_chat_id")), img, f"🚀 <b>立即推送</b>")
        else:
            self._cmd_stats(str(cfg.get("tg_chat_id")))
        return True

bot = TelegramBot()