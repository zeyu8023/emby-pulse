import threading
import time
import requests
import datetime
import io
import logging
import urllib.parse
import json # 引入 json 用于构建按钮
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
        # 缓存用户ID到用户名的映射
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
        print("🤖 Bot Service Started (Enhanced Search UI)")

    def stop(self): self.running = False

    def _get_proxies(self):
        proxy = cfg.get("proxy_url")
        return {"http": proxy, "https": proxy} if proxy else None

    def _get_username(self, user_id):
        if user_id in self.user_cache: return self.user_cache[user_id]
        
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return user_id
        
        try:
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
                url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=600&quality=90&tag={image_tag}"
            else:
                url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=600&quality=90&api_key={key}"
            
            res = requests.get(url, timeout=15)
            if res.status_code == 200: return io.BytesIO(res.content)
        except: pass
        return None

    # 修改 send_photo 支持 reply_markup (按钮)
    def send_photo(self, chat_id, photo_io, caption, parse_mode="HTML", reply_markup=None):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
                
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
            
            target_id = item.get("Id")
            if item.get("Type") == "Episode" and item.get("SeriesId"):
                target_id = item.get("SeriesId")
            
            img_io = self._download_emby_image(target_id, 'Primary') 
            if not img_io: img_io = self._download_emby_image(item.get("Id"), 'Backdrop')

            if img_io: self.send_photo(chat_id, img_io, msg)
            else: self.send_message(chat_id, msg)
        except: pass

    def push_new_media(self, item_id, fallback_item=None):
        if not cfg.get("enable_library_notify") or not cfg.get("tg_chat_id"): return
        cid = str(cfg.get("tg_chat_id")); host = cfg.get("emby_host"); key = cfg.get("emby_api_key")

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
            name = final.get("Name", "未知")
            type_raw = final.get("Type", "Movie")
            overview = final.get("Overview", "暂无简介...")
            rating = final.get("CommunityRating", "N/A")
            year = final.get("ProductionYear", "")
            
            if len(overview) > 150: overview = overview[:140] + "..."
            
            type_cn = "电影"
            display_title = name
            if type_raw == "Episode":
                type_cn = "剧集"
                s_name = final.get("SeriesName", "")
                s_idx = final.get("ParentIndexNumber", 1)
                e_idx = final.get("IndexNumber", 1)
                display_title = f"{s_name} S{str(s_idx).zfill(2)}E{str(e_idx).zfill(2)}"
                if name and "Episode" not in name: display_title += f" {name}"
            elif type_raw == "Series": type_cn = "剧集"

            caption = (
                f"📺 <b>新入库 {type_cn}</b>\n{display_title} ({year})\n\n"
                f"⭐ 评分：{rating}/10\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"📝 剧情：{overview}"
            )

            target_id = item_id
            use_tag = final.get("ImageTags", {}).get("Primary")
            
            if type_raw == "Episode" and final.get("SeriesId"):
                target_id = final.get("SeriesId")
                use_tag = None 

            img_io = self._download_emby_image(target_id, 'Primary', image_tag=use_tag)
            
            if img_io: self.send_photo(cid, img_io, caption)
            else: self.send_photo(cid, REPORT_COVER_URL, caption)
        except: pass

    # ================= 指令系统 =================

    def _set_commands(self):
        token = cfg.get("tg_bot_token")
        cmds = [{"command": "search", "description": "🔍 搜索资源"},
                {"command": "stats", "description": "📊 今日日报"},
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
        if text.startswith("/search"): self._cmd_search(cid, text)
        elif text.startswith("/stats"): self._cmd_stats(cid, 'day')
        elif text.startswith("/weekly"): self._cmd_stats(cid, 'week')
        elif text.startswith("/monthly"): self._cmd_stats(cid, 'month')
        elif text.startswith("/yearly"): self._cmd_stats(cid, 'year')
        elif text.startswith("/now"): self._cmd_now(cid)
        elif text.startswith("/latest"): self._cmd_latest(cid)
        elif text.startswith("/recent"): self._cmd_recent(cid)
        elif text.startswith("/check"): self._cmd_check(cid)
        elif text.startswith("/help"): self._cmd_help(cid)

    # 🔥 核心升级：富媒体搜索
    def _cmd_search(self, chat_id, text):
        parts = text.split(' ', 1)
        if len(parts) < 2:
            return self.send_message(chat_id, "🔍 <b>搜索格式错误</b>\n请使用: <code>/search 关键词</code>\n例如: <code>/search 庆余年</code>")
        
        keyword = parts[1].strip()
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        
        try:
            # 1. 增加请求字段：评分、类型、年代、分级
            encoded_key = urllib.parse.quote(keyword)
            fields = "CommunityRating,ProductionYear,Genres,Overview,OfficialRating,ProviderIds"
            url = f"{host}/emby/Items?SearchTerm={encoded_key}&IncludeItemTypes=Movie,Series&Recursive=true&Fields={fields}&Limit=5&api_key={key}"
            
            res = requests.get(url, timeout=10)
            items = res.json().get("Items", [])
            
            if not items:
                return self.send_message(chat_id, f"📭 未找到与 <b>{keyword}</b> 相关的资源")
            
            # 2. 构建主展示信息 (第一个结果)
            top = items[0]
            name = top.get("Name")
            year = top.get("ProductionYear", "")
            year_str = f"({year})" if year else ""
            
            # 评分
            rating = top.get("CommunityRating")
            score_str = f"⭐️ <b>{rating}</b>" if rating else "⭐️ N/A"
            
            # 类型 (最多显示3个)
            genres = top.get("Genres", [])
            genre_str = " / ".join(genres[:3]) if genres else "暂无分类"
            
            # 简介
            overview = top.get("Overview", "暂无简介")
            if len(overview) > 100: overview = overview[:100] + "..."
            
            # 类型图标
            type_icon = "🎬" if top.get("Type") == "Movie" else "📺"
            
            # 构建富文本
            caption = (
                f"{type_icon} <b>{name}</b> {year_str}\n"
                f"{score_str}  |  🎭 {genre_str}\n"
                f"───────────────\n"
                f"📝 <b>简介</b>: {overview}\n"
            )
            
            # 3. 处理"其他结果"
            if len(items) > 1:
                caption += "\n🔎 <b>其他匹配:</b>\n"
                for i, sub in enumerate(items[1:]):
                    sub_year = f"({sub.get('ProductionYear')})" if sub.get('ProductionYear') else ""
                    sub_score = f"⭐️{sub.get('CommunityRating')}" if sub.get('CommunityRating') else ""
                    caption += f"{i+2}. {sub.get('Name')} {sub_year} {sub_score}\n"

            # 4. 🔥 核心升级：生成播放按钮
            # 优先使用配置的 public_host，如果没有则用 emby_host
            # 注意：emby_host 如果是内网IP，在外网点按钮是打不开的
            base_url = cfg.get("emby_public_host") or host
            # 移除末尾斜杠以防万一
            if base_url.endswith('/'): base_url = base_url[:-1]
            
            # Emby Web 播放链接格式
            play_url = f"{base_url}/web/index.html#!/item?id={top.get('Id')}&serverId={top.get('ServerId')}"
            
            buttons = [
                [{"text": "▶️ 立即播放", "url": play_url}],
                # 如果有 IMDb ID，可以加个 IMDb 按钮 (可选)
                # [{"text": "🌐 IMDb", "url": f"https://www.imdb.com/title/{top['ProviderIds'].get('Imdb')}"}] if top.get('ProviderIds', {}).get('Imdb') else []
            ]
            
            keyboard = {"inline_keyboard": [btn for btn in buttons if btn]}

            # 5. 发送带按钮的消息
            img_io = self._download_emby_image(top.get("Id"), 'Primary')
            if img_io:
                self.send_photo(chat_id, img_io, caption, reply_markup=keyboard)
            else:
                # 如果没图，发文本消息带按钮
                # send_message 需要改写支持 reply_markup，这里简单处理：发图片失败就发文本
                # 为了保持代码简洁，这里暂时只发文本，不带按钮(send_message没加markup参数)
                # 建议：如果没图，用一张默认图发送，这样就能带按钮了
                self.send_photo(chat_id, REPORT_COVER_URL, caption, reply_markup=keyboard)

        except Exception as e:
            logger.error(f"Search Error: {e}")
            self.send_message(chat_id, "❌ 搜索时发生错误")

    def _cmd_stats(self, chat_id, period='day'):
        where, params = get_base_filter('all') 
        titles = {'day': '今日日报', 'week': '本周周报', 'month': '本月月报', 'year': '年度报告'}
        title_cn = titles.get(period, '数据报表')

        if period == 'week': time_filter = "date('now', '-7 days')"
        elif period == 'month': time_filter = "date('now', 'start of month')"
        elif period == 'year': time_filter = "date('now', 'start of year')"
        else: time_filter = "date('now', 'start of day')" 

        where += f" AND DateCreated > {time_filter}"
        
        try:
            plays_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)
            if not plays_res: raise Exception("DB Error")
            plays = plays_res[0]['c']
            
            dur_res = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
            dur = dur_res[0]['c'] if dur_res and dur_res[0]['c'] else 0
            hours = round(dur / 3600, 1)
            
            users_res = query_db(f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where}", params)
            users = users_res[0]['c'] if users_res else 0

            top_users = query_db(f"SELECT UserId, SUM(PlayDuration) as t FROM PlaybackActivity {where} GROUP BY UserId ORDER BY t DESC LIMIT 5", params)
            user_str = ""
            if top_users:
                for i, u in enumerate(top_users):
                    name = self._get_username(u['UserId'])
                    h = round(u['t'] / 3600, 1)
                    prefix = ['🥇','🥈','🥉'][i] if i < 3 else f"{i+1}."
                    user_str += f"{prefix} {name} ({h}h)\n"
            else:
                user_str = "暂无数据"

            tops = query_db(f"SELECT ItemName, COUNT(*) as c FROM PlaybackActivity {where} GROUP BY ItemName ORDER BY c DESC LIMIT 10", params)
            top_content = ""
            if tops:
                for i, item in enumerate(tops):
                    prefix = ['🥇','🥈','🥉'][i] if i < 3 else f"{i+1}."
                    top_content += f"{prefix} {item['ItemName']} ({item['c']}次)\n"
            else:
                top_content = "暂无数据"

            caption = (
                f"📊 <b>EmbyPulse {title_cn}</b>\n───────────────\n"
                f"📈 <b>数据大盘</b>\n▶️ 总播放量: {plays} 次\n⏱️ 活跃时长: {hours} 小时\n👥 活跃人数: {users} 人\n"
                f"───────────────\n🏆 <b>活跃用户 Top 5</b>\n{user_str}"
                f"───────────────\n🔥 <b>热门内容 Top 10</b>\n{top_content}"
            )

            if HAS_PIL:
                img = report_gen.generate_report('all', period)
                if img: self.send_photo(chat_id, img, caption)
                else: self.send_message(chat_id, caption)
            else:
                self.send_photo(chat_id, REPORT_COVER_URL, caption)

        except Exception as e:
            logger.error(f"Stats Error: {e}")
            self.send_message(chat_id, f"❌ 统计失败: 数据库查询错误")

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
        try:
            rows = query_db("SELECT UserId, ItemName, DateCreated FROM PlaybackActivity ORDER BY DateCreated DESC LIMIT 10")
            if not rows: return self.send_message(cid, "📭 无记录")
            msg = "📜 <b>最近播放</b>\n"
            for r in rows:
                date = r['DateCreated'][:16].replace('T', ' ')
                name = self._get_username(r['UserId'])
                msg += f"\n⏰ {date} | {name}\n🎬 {r['ItemName']}\n"
            self.send_message(cid, msg)
        except Exception as e: 
            logger.error(f"Recent Error: {e}")
            self.send_message(cid, f"❌ 查询失败")

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
        self.send_message(cid, "🤖 /search, /stats, /weekly, /monthly, /now, /latest, /recent, /check")

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
        try:
            users = query_db("SELECT user_id, expire_date FROM users_meta WHERE expire_date IS NOT NULL AND expire_date != ''")
            if not users: return
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
            for u in users:
                if u['expire_date'] < today:
                    try: requests.post(f"{host}/emby/Users/{u['user_id']}/Policy?api_key={key}", json={"IsDisabled": True})
                    except: pass
        except: pass
    
    def push_now(self, user_id, period, theme):
        if not cfg.get("tg_chat_id"): return False
        self._cmd_stats(str(cfg.get("tg_chat_id")), period)
        return True

bot = TelegramBot()