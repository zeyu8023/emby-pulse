import threading
import time
import requests
import datetime
import io
import logging
from app.core.config import cfg, REPORT_COVER_URL, FALLBACK_IMAGE_URL
from app.core.database import query_db, get_base_filter
from app.services.report_service import report_gen, HAS_PIL

logger = logging.getLogger("uvicorn")

class TelegramBot:
    def __init__(self):
        self.running = False
        self.poll_thread = None
        self.schedule_thread = None 
        self.offset = 0
        self.last_check_min = -1
        # 简单的用户 ID 缓存
        self.user_cache = {}
        
    def start(self):
        if self.running: return
        if not cfg.get("tg_bot_token"): return
        self.running = True
        self._set_commands()
        self.poll_thread = threading.Thread(target=self._polling_loop, daemon=True)
        self.poll_thread.start()
        self.schedule_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.schedule_thread.start()
        print("🤖 Bot Service Started (Fixed Schema Mode)")

    def stop(self): self.running = False

    def _get_proxies(self):
        proxy = cfg.get("proxy_url")
        return {"http": proxy, "https": proxy} if proxy else None

    # 🔥 新增：ID 转 用户名
    def _get_username(self, user_id):
        if user_id in self.user_cache: return self.user_cache[user_id]
        
        # 没缓存，去 API 查
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return user_id
        try:
            # 查所有用户刷新缓存
            res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=2)
            if res.status_code == 200:
                for u in res.json():
                    self.user_cache[u['Id']] = u['Name']
        except: pass
        return self.user_cache.get(user_id, "Unknown User")

    def _get_location(self, ip):
        if not ip or ip in ['127.0.0.1', '::1', '0.0.0.0']: return "本地连接"
        try:
            res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=3)
            if res.status_code == 200:
                d = res.json()
                if d.get('status') == 'success':
                    return f"{d.get('country')} {d.get('regionName')} {d.get('city')}"
        except: pass
        return "未知位置"

    def _download_emby_image(self, item_id, img_type='Primary', image_tag=None):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return None
        try:
            if image_tag:
                url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=1200&quality=90&tag={image_tag}"
            else:
                url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=1200&quality=90&api_key={key}"
            res = requests.get(url, timeout=15)
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
                requests.post(url, data=data, files=files, proxies=self._get_proxies(), timeout=30)
        except Exception as e: 
            logger.error(f"Send Photo Error: {e}")
            self.send_message(chat_id, caption)

    def send_message(self, chat_id, text, parse_mode="HTML"):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}, proxies=self._get_proxies(), timeout=10)
        except Exception as e: logger.error(f"Send Message Error: {e}")

    # ================= 业务逻辑 =================

    # 只读模式，不需要写入
    def save_playback_activity(self, data):
        pass 

    def push_playback_event(self, data, action="start"):
        if not cfg.get("enable_notify") or not cfg.get("tg_chat_id"): return
        try:
            chat_id = str(cfg.get("tg_chat_id"))
            user = data.get("User", {})
            item = data.get("Item", {})
            session = data.get("Session", {})
            
            title = item.get("Name", "未知内容")
            if item.get("SeriesName"): 
                idx = item.get("IndexNumber", 0)
                parent_idx = item.get("ParentIndexNumber", 1)
                title = f"{item.get('SeriesName')} S{str(parent_idx).zfill(2)}E{str(idx).zfill(2)} {title}"

            type_cn = "剧集" if item.get("Type") == "Episode" else "电影"
            
            ticks = data.get("PlaybackPositionTicks")
            if ticks is None: ticks = session.get("PlayState", {}).get("PositionTicks", 0)
            total = item.get("RunTimeTicks", 1)
            pct = f"{(ticks / total * 100):.2f}%" if total > 0 else "0.00%"

            emoji = "▶️" if action == "start" else "⏹️"
            act = "开始播放" if action == "start" else "停止播放"
            ip = session.get("RemoteEndPoint", "127.0.0.1")
            loc = self._get_location(ip)

            msg = (
                f"{emoji} <b>【{user.get('Name')}】{act}</b>\n"
                f"📺 {title}\n"
                f"📚 类型：{type_cn}\n"
                f"🔄 进度：{pct}\n"
                f"🌐 地址：{ip} ({loc})\n"
                f"📱 设备：{session.get('Client')} on {session.get('DeviceName')}\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            img = self._download_emby_image(item.get("Id"), 'Backdrop') or self._download_emby_image(item.get("Id"), 'Primary')
            if img: self.send_photo(chat_id, img, msg)
            else: self.send_message(cid, msg)
        except: pass

    def push_new_media(self, item_id, fallback_item=None):
        if not cfg.get("enable_library_notify") or not cfg.get("tg_chat_id"): return
        cid = str(cfg.get("tg_chat_id")); host = cfg.get("emby_host"); key = cfg.get("emby_api_key")

        direct_tag = None
        if fallback_item:
            direct_tag = fallback_item.get("ImageTags", {}).get("Primary")

        if direct_tag: item = fallback_item
        else:
            item = None
            for i in range(3):
                time.sleep(10 + i*15)
                try:
                    res = requests.get(f"{host}/emby/Items/{item_id}?api_key={key}", timeout=10)
                    if res.status_code == 200:
                        item = res.json()
                        if item.get("ImageTags", {}).get("Primary"): break
                except: pass
        
        final = item if item else fallback_item
        if not final: return

        try:
            name = final.get("Name", "")
            if final.get("Type") == "Episode":
                name = f"{final.get('SeriesName','')} S{str(final.get('ParentIndexNumber',1)).zfill(2)}E{str(final.get('IndexNumber',1)).zfill(2)}"
            
            caption = (
                f"📺 <b>新入库 {final.get('Type','影视')}</b>\n{name} ({final.get('ProductionYear','')})\n\n"
                f"⭐ 评分：{final.get('CommunityRating','N/A')}/10\n"
                f"📝 剧情：{final.get('Overview','暂无简介...')[:140]}..."
            )
            img_tag = final.get("ImageTags", {}).get("Primary")
            img_io = self._download_emby_image(item_id, 'Primary', image_tag=img_tag)
            
            if img_io: self.send_photo(cid, img_io, caption)
            else: self.send_photo(cid, REPORT_COVER_URL, caption)
        except: pass

    # ================= 指令系统 =================

    def _set_commands(self):
        token = cfg.get("tg_bot_token")
        cmds = [{"command": "stats", "description": "📊 今日日报"},
                {"command": "weekly", "description": "📅 本周周报"},
                {"command": "monthly", "description": "🗓️ 本月月报"},
                {"command": "yearly", "description": "📜 年度总结"},
                {"command": "now", "description": "🟢 正在播放"},
                {"command": "latest", "description": "🆕 最近入库"},
                {"command": "recent", "description": "📜 播放历史"},
                {"command": "check", "description": "📡 系统检查"},
                {"command": "help", "description": "🤖 帮助菜单"}]
        try: requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", json={"commands": cmds}, proxies=self._get_proxies(), timeout=10)
        except: pass

    def _polling_loop(self):
        token = cfg.get("tg_bot_token"); admin_id = str(cfg.get("tg_chat_id"))
        while self.running:
            try:
                res = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"offset": self.offset, "timeout": 30}, proxies=self._get_proxies(), timeout=35)
                if res.status_code == 200:
                    for u in res.json().get("result", []):
                        self.offset = u["update_id"] + 1
                        if "message" in u:
                            cid = str(u["message"]["chat"]["id"])
                            if admin_id and cid != admin_id: continue
                            self._handle_message(u["message"], cid)
                else: time.sleep(5)
            except: time.sleep(5)

    def _handle_message(self, msg, cid):
        text = msg.get("text", "").strip()
        if text.startswith("/stats"): self._cmd_stats(cid, 'day')
        elif text.startswith("/weekly"): self._cmd_stats(cid, 'week')
        elif text.startswith("/monthly"): self._cmd_stats(cid, 'month')
        elif text.startswith("/yearly"): self._cmd_stats(cid, 'year')
        elif text.startswith("/now"): self._cmd_now(cid)
        elif text.startswith("/latest"): self._cmd_latest(cid)
        elif text.startswith("/recent"): self._cmd_recent(cid)
        elif text.startswith("/check"): self._cmd_check(cid)
        elif text.startswith("/help"): self._cmd_help(cid)

    # 🔥 修复：使用 UserId 聚合，然后查名字
    def _cmd_stats(self, chat_id, period='day'):
        where, params = get_base_filter('all') 
        
        # 1. 确定时间
        if period == 'week': time_filter = "date('now', '-7 days')"
        elif period == 'month': time_filter = "date('now', 'start of month')"
        elif period == 'year': time_filter = "date('now', 'start of year')"
        else: time_filter = "date('now', 'start of day')" # day

        where += f" AND DateCreated > {time_filter}"
        
        try:
            # 2. 查数据 (用 UserId)
            plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)[0]['c']
            dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)[0]['c'] or 0
            hours = round(dur / 3600, 1)
            
            # 活跃人数 (DISTINCT UserId)
            users = query_db(f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where}", params)[0]['c']

            # 榜首 (UserId)
            top = query_db(f"SELECT UserId, SUM(PlayDuration) as t FROM PlaybackActivity {where} GROUP BY UserId ORDER BY t DESC LIMIT 1", params)
            
            top_str = "暂无"
            if top:
                name = self._get_username(top[0]['UserId']) # ID -> Name
                u_h = round(top[0]['t'] / 3600, 1)
                top_str = f"{name} ({u_h}h)"

            # 热门内容
            tops = query_db(f"SELECT ItemName, COUNT(*) as c FROM PlaybackActivity {where} GROUP BY ItemName ORDER BY c DESC LIMIT 3", params)
            top_content = ""
            for i, item in enumerate(tops):
                top_content += f"{['🥇','🥈','🥉'][i]} {item['ItemName']} ({item['c']}次)\n"

            caption = (
                f"📊 <b>EmbyPulse {period}报告</b>\n───────────────\n"
                f"📈 <b>数据大盘</b>\n▶️ 总播放量: {plays} 次\n⏱️ 活跃时长: {hours} 小时\n"
                f"👥 活跃人数: {users} 人\n👑 榜首之星: {top_str}\n"
                f"───────────────\n🔥 <b>热门内容 Top 3</b>\n{top_content or '暂无数据'}"
            )

            if HAS_PIL:
                img = report_gen.generate_report('all', period)
                if img: self.send_photo(chat_id, img, caption)
                else: self.send_message(chat_id, caption)
            else:
                self.send_photo(chat_id, REPORT_COVER_URL, caption)

        except Exception as e:
            logger.error(f"Stats Error: {e}")
            self.send_message(chat_id, f"❌ 数据库查询失败")

    def _cmd_now(self, cid):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=5)
            sessions = [s for s in res.json() if s.get("NowPlayingItem")]
            if not sessions: return self.send_message(cid, "🟢 当前无播放")
            msg = f"🟢 <b>正在播放 ({len(sessions)})</b>\n"
            for s in sessions:
                title = s['NowPlayingItem'].get('Name')
                pct = int(s.get('PlayState', {}).get('PositionTicks', 0) / s['NowPlayingItem'].get('RunTimeTicks', 1) * 100)
                msg += f"\n👤 <b>{s.get('UserName')}</b> | 🔄 {pct}%\n📺 {title}\n"
            self.send_message(cid, msg)
        except: self.send_message(cid, "❌ 连接失败")

    def _cmd_latest(self, cid):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            url = f"{host}/emby/Items?SortBy=DateCreated&SortOrder=Descending&IncludeItemTypes=Movie,Episode&Limit=5&Recursive=true&api_key={key}"
            items = requests.get(url, timeout=10).json().get("Items", [])
            msg = "🆕 <b>最近入库</b>\n"
            for i in items:
                name = i.get("Name")
                if i.get("SeriesName"): name = f"{i.get('SeriesName')} - {name}"
                msg += f"\n📅 {i.get('DateCreated', '')[:10]} | {name}"
            self.send_message(cid, msg)
        except: self.send_message(cid, "❌ 查询失败")

    def _cmd_recent(self, cid):
        # 修正：查 UserId, ItemName
        try:
            rows = query_db("SELECT UserId, ItemName, DateCreated FROM PlaybackActivity ORDER BY DateCreated DESC LIMIT 10")
            if not rows: return self.send_message(cid, "📭 无记录")
            msg = "📜 <b>最近播放</b>\n"
            for r in rows:
                date = r['DateCreated'][:16].replace('T', ' ')
                name = self._get_username(r['UserId'])
                msg += f"\n⏰ {date} | {name}\n🎬 {r['ItemName']}\n"
            self.send_message(cid, msg)
        except Exception as e: self.send_message(cid, f"❌ 查询失败")

    def _cmd_check(self, cid):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        start = time.time()
        try:
            res = requests.get(f"{host}/emby/System/Info?api_key={key}", timeout=5)
            if res.status_code == 200:
                info = res.json()
                local = (info.get('LocalAddresses') or [info.get('LocalAddress')])[0]
                wan = (info.get('RemoteAddresses') or [info.get('WanAddress')])[0]
                self.send_message(cid, f"✅ <b>在线</b>\n延迟: {int((time.time()-start)*1000)}ms\n内网: {local}\n外网: {wan}")
        except: self.send_message(cid, "❌ 离线")

    def _cmd_help(self, cid):
        self.send_message(cid, "🤖 /stats, /weekly, /monthly, /now, /latest, /recent, /check")

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
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        for u in users:
            if u['expire_date'] < today:
                try: requests.post(f"{host}/emby/Users/{u['user_id']}/Policy?api_key={key}", json={"IsDisabled": True})
                except: pass
    
    def push_now(self, user_id, period, theme):
        if not cfg.get("tg_chat_id"): return False
        self._cmd_stats(str(cfg.get("tg_chat_id")), period)
        return True

bot = TelegramBot()