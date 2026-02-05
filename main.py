import sqlite3
import os
import uvicorn
import requests
import datetime
import json
import time
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict

# ... (前面的 import 不变)

# ================= 配置与持久化 =================
# 🔥 修改：将配置文件独立存放在 /app/config 目录，与 Emby 数据隔离
CONFIG_DIR = "/app/config"
if not os.path.exists(CONFIG_DIR):
    os.makedirs(CONFIG_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")

# 默认配置
DEFAULT_CONFIG = {
    "emby_host": os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/'),
    "emby_api_key": os.getenv("EMBY_API_KEY", "").strip(),
    "tmdb_api_key": os.getenv("TMDB_API_KEY", "").strip(),
    "hidden_users": [], # 用户黑名单 ID 列表
    "public_host": ""   # 公网访问地址(备用)
}

# 配置管理器
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
            except Exception as e:
                print(f"⚠️ Config Load Error: {e}")
    
    def save(self):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ Config Save Error: {e}")

    def get(self, key): return self.config.get(key, DEFAULT_CONFIG.get(key))
    def set(self, key, value): self.config[key] = value; self.save()
    def get_all(self): return self.config

cfg = ConfigManager()

# ================= 基础设置 =================
PORT = 10307
SECRET_KEY = os.getenv("SECRET_KEY", "embypulse_secret_key_2026")
FALLBACK_IMAGE_URL = "https://img.hotimg.com/a444d32a033994d5b.png"

# 内置 TMDB 壁纸库 (保底)
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

print(f"--- EmbyPulse V47 (Settings & User Filter) ---")
print(f"Config File: {CONFIG_FILE}")

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
    hidden_users: List[str] = []

# ================= 辅助函数 =================
def query_db(query, args=(), one=False):
    if not os.path.exists(DB_PATH): return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, args)
        rv = cur.fetchall()
        conn.close()
        return (rv[0] if rv else None) if one else rv
    except Exception as e:
        print(f"❌ SQL Error: {e}")
        return None

# 🔥 核心：构建带黑名单过滤的 SQL 条件
def get_base_filter(user_id_filter: Optional[str]):
    where = "WHERE 1=1"
    params = []
    
    # 1. 指定用户筛选
    if user_id_filter and user_id_filter != 'all':
        where += " AND UserId = ?"
        params.append(user_id_filter)
    
    # 2. 黑名单过滤 (仅当查看全服数据时生效)
    # 如果指定了查看某用户，即使他在黑名单也显示
    if (not user_id_filter or user_id_filter == 'all') and len(cfg.get("hidden_users")) > 0:
        hidden = cfg.get("hidden_users")
        placeholders = ','.join(['?'] * len(hidden))
        where += f" AND UserId NOT IN ({placeholders})"
        params.extend(hidden)
        
    return where, params

def get_user_map():
    user_map = {}
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    if key and host:
        try:
            res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=2)
            if res.status_code == 200:
                for u in res.json(): user_map[u['Id']] = u['Name']
        except: pass
    return user_map

# ================= ⚙️ 设置相关路由 =================
@app.get("/settings")
async def page_settings(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("settings.html", {"request": request, "active_page": "settings", "user": request.session.get("user")})

@app.get("/api/settings")
def api_get_settings(request: Request):
    if not request.session.get("user"): return {"status": "error", "message": "Unauthorized"}
    conf = cfg.get_all().copy()
    # 脱敏处理 (可选，这里为了方便修改暂不脱敏)
    return {"status": "success", "data": conf}

@app.post("/api/settings")
def api_save_settings(data: SettingsModel, request: Request):
    if not request.session.get("user"): return {"status": "error", "message": "Unauthorized"}
    
    # 验证 Emby 连接性
    try:
        test_url = f"{data.emby_host.rstrip('/')}/emby/System/Info?api_key={data.emby_api_key}"
        res = requests.get(test_url, timeout=5)
        if res.status_code != 200:
            return {"status": "error", "message": "Emby 连接失败，请检查地址或密钥"}
    except:
        return {"status": "error", "message": "无法连接到 Emby 服务器"}

    # 保存配置
    cfg.set("emby_host", data.emby_host.rstrip('/'))
    cfg.set("emby_api_key", data.emby_api_key)
    cfg.set("tmdb_api_key", data.tmdb_api_key)
    cfg.set("hidden_users", data.hidden_users)
    
    return {"status": "success", "message": "配置已保存"}

# ================= 🔐 认证路由 =================
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
            if not user_info.get("Policy", {}).get("IsAdministrator", False):
                return {"status": "error", "message": "仅限 Emby 管理员登录"}
            
            request.session["user"] = {
                "id": user_info.get("Id"),
                "name": user_info.get("Name"),
                "is_admin": True
            }
            return {"status": "success", "message": "登录成功"}
        else:
            return {"status": "error", "message": "用户名或密码错误"}
    except Exception as e:
        return {"status": "error", "message": f"连接失败: {str(e)}"}

@app.get("/logout")
async def api_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")

@app.get("/api/wallpaper")
def api_get_wallpaper():
    tmdb_key = cfg.get("tmdb_api_key")
    if tmdb_key:
        try:
            url = f"https://api.themoviedb.org/3/trending/all/week?api_key={tmdb_key}&language=zh-CN"
            res = requests.get(url, timeout=3)
            if res.status_code == 200:
                data = res.json()
                results = [i for i in data.get("results", []) if i.get("backdrop_path")]
                if results:
                    target = random.choice(results)
                    return {"status": "success", "url": f"https://image.tmdb.org/t/p/original{target['backdrop_path']}", "title": target.get("title") or target.get("name")}
        except: pass
    return {"status": "success", "url": random.choice(TMDB_FALLBACK_POOL), "title": "Cinematic Collection"}

# ================= 页面路由 =================
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

# ================= 核心 API (已应用配置) =================
@app.get("/api/users")
def api_get_users():
    try:
        # 获取所有有播放记录的用户
        results = query_db("SELECT DISTINCT UserId FROM PlaybackActivity")
        if not results: return {"status": "success", "data": []}
        
        user_map = get_user_map() # 从 Emby 实时获取名字
        hidden_users = cfg.get("hidden_users") or []
        
        data = []
        for row in results:
            uid = row['UserId']
            if not uid: continue
            name = user_map.get(uid, f"User {str(uid)[:5]}")
            # 标记是否被隐藏
            is_hidden = uid in hidden_users
            data.append({"UserId": uid, "UserName": name, "IsHidden": is_hidden})
            
        data.sort(key=lambda x: x['UserName'])
        return {"status": "success", "data": data}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.get("/api/stats/dashboard")
def api_dashboard(user_id: Optional[str] = None):
    try:
        where, params = get_base_filter(user_id)
        
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)
        users = query_db(f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where} AND DateCreated > date('now', '-30 days')", params)
        dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
        
        base_stats = {
            "total_plays": plays[0]['c'] if plays else 0,
            "active_users": users[0]['c'] if users else 0,
            "total_duration": dur[0]['c'] if dur else 0
        }

        library_stats = {"movie": 0, "series": 0, "episode": 0}
        key = cfg.get("emby_api_key")
        host = cfg.get("emby_host")
        if key and host:
            try:
                url = f"{host}/emby/Items/Counts?api_key={key}"
                res = requests.get(url, timeout=2)
                if res.status_code == 200:
                    data = res.json()
                    library_stats["movie"] = data.get("MovieCount", 0)
                    library_stats["series"] = data.get("SeriesCount", 0)
                    library_stats["episode"] = data.get("EpisodeCount", 0)
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
        user_map = get_user_map()
        final_data = []
        seen_keys = set() 
        for row in results:
            item = dict(row)
            item['UserName'] = user_map.get(item['UserId'], "User")
            raw_name = item['ItemName']
            clean_name = raw_name
            if ' - ' in raw_name: clean_name = raw_name.split(' - ')[0]
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
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    if not key or not host: return {"status": "error"}
    try:
        res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=2)
        if res.status_code == 200:
            return {"status": "success", "data": [s for s in res.json() if s.get("NowPlayingItem")]}
    except: pass
    return {"status": "success", "data": []}

@app.get("/api/stats/top_movies")
def api_top_movies(user_id: Optional[str] = None, category: str = 'all', sort_by: str = 'count'):
    try:
        where, params = get_base_filter(user_id)
        if category == 'Movie': where += " AND ItemType = 'Movie'"
        elif category == 'Episode': where += " AND ItemType = 'Episode'"
        sql = f"SELECT ItemName, ItemId, ItemType, PlayDuration FROM PlaybackActivity {where} LIMIT 2000"
        rows = query_db(sql, params)
        if not rows: return {"status": "success", "data": []}
        aggregated = {}
        for row in rows:
            raw_name = row['ItemName']
            clean_name = raw_name
            if ' - ' in raw_name: clean_name = raw_name.split(' - ')[0]
            if clean_name not in aggregated:
                aggregated[clean_name] = {'ItemName': clean_name, 'ItemId': row['ItemId'], 'PlayCount': 0, 'TotalTime': 0}
            aggregated[clean_name]['PlayCount'] += 1
            aggregated[clean_name]['TotalTime'] += (row['PlayDuration'] or 0)
            aggregated[clean_name]['ItemId'] = row['ItemId']
        result_list = list(aggregated.values())
        if sort_by == 'time': result_list.sort(key=lambda x: x['TotalTime'], reverse=True)
        else: result_list.sort(key=lambda x: x['PlayCount'], reverse=True)
        return {"status": "success", "data": result_list[:50]}
    except: return {"status": "error", "data": []}

@app.get("/api/stats/user_details")
def api_user_details(user_id: Optional[str] = None):
    try:
        where, params = get_base_filter(user_id)
        hourly_res = query_db(f"SELECT strftime('%H', DateCreated) as Hour, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Hour ORDER BY Hour", params)
        hourly_data = {str(i).zfill(2): 0 for i in range(24)}
        if hourly_res:
            for r in hourly_res: hourly_data[r['Hour']] = r['Plays']
        device_res = query_db(f"SELECT COALESCE(DeviceName, ClientName, 'Unknown') as Device, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Device ORDER BY Plays DESC LIMIT 10", params)
        logs_res = query_db(f"SELECT DateCreated, ItemName, PlayDuration, COALESCE(DeviceName, ClientName) as Device, UserId FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 100", params)
        user_map = get_user_map()
        logs_data = []
        if logs_res:
            for r in logs_res:
                l = dict(r)
                l['UserName'] = user_map.get(l['UserId'], "User")
                logs_data.append(l)
        return {"status": "success", "data": {"hourly": hourly_data, "devices": [dict(r) for r in device_res] if device_res else [], "logs": logs_data}}
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
        results = query_db(sql, params)
        data = {}
        if results:
            for r in results: data[r['Label']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

@app.get("/api/stats/poster_data")
def api_poster_data(user_id: Optional[str] = None, period: str = 'all'):
    try:
        where_base, params = get_base_filter(user_id) # 这里已经包含了 hidden_users 过滤
        date_filter = ""
        if period == 'week': date_filter = " AND DateCreated > date('now', '-7 days')"
        elif period == 'month': date_filter = " AND DateCreated > date('now', '-30 days')"
        elif period == 'year': date_filter = " AND DateCreated > date('now', '-1 year')"
        
        # 服务器总播放也应该排除黑名单用户
        server_res = query_db(f"SELECT COUNT(*) as Plays FROM PlaybackActivity {get_base_filter('all')[0]} {date_filter}", get_base_filter('all')[1])
        server_plays = server_res[0]['Plays'] if server_res else 0

        where = where_base + date_filter
        
        raw_sql = f"SELECT ItemName, ItemId, ItemType, PlayDuration FROM PlaybackActivity {where}"
        rows = query_db(raw_sql, params)
        
        total_plays = 0
        total_duration = 0
        aggregated = {} 

        if rows:
            for row in rows:
                total_plays += 1
                dur = row['PlayDuration'] or 0
                total_duration += dur
                raw_name = row['ItemName']
                clean_name = raw_name
                if ' - ' in raw_name: clean_name = raw_name.split(' - ')[0]
                if clean_name not in aggregated:
                    aggregated[clean_name] = {'ItemName': clean_name, 'ItemId': row['ItemId'], 'Count': 0, 'Duration': 0}
                aggregated[clean_name]['Count'] += 1
                aggregated[clean_name]['Duration'] += dur
                aggregated[clean_name]['ItemId'] = row['ItemId'] 

        top_list = list(aggregated.values())
        top_list.sort(key=lambda x: x['Count'], reverse=True)
        top_list = top_list[:10]
        total_hours = round(total_duration / 3600)
        
        return {"status": "success", "data": {"plays": total_plays, "hours": total_hours, "server_plays": server_plays, "top_list": top_list, "tags": ["观影达人"]}}
    except Exception as e: return {"status": "error", "message": str(e), "data": {"plays": 0, "hours": 0, "server_plays": 0, "top_list": []}}

@app.get("/api/stats/top_users_list")
def api_top_users_list():
    try:
        # 获取所有用户统计
        res = query_db("SELECT UserId, COUNT(*) as Plays, SUM(PlayDuration) as TotalTime FROM PlaybackActivity GROUP BY UserId ORDER BY TotalTime DESC")
        if not res: return {"status": "success", "data": []}
        
        user_map = get_user_map()
        hidden_users = cfg.get("hidden_users") or []
        data = []
        
        for row in res:
            uid = row['UserId']
            # 过滤黑名单用户
            if uid in hidden_users: continue
            
            u = dict(row)
            u['UserName'] = user_map.get(uid, f"User {str(uid)[:5]}")
            data.append(u)
            if len(data) >= 5: break # 只取前5
            
        return {"status": "success", "data": data}
    except: return {"status": "success", "data": []}

@app.get("/api/proxy/image/{item_id}/{img_type}")
def proxy_image(item_id: str, img_type: str):
    target_id = item_id
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
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
        if resp.status_code == 200: 
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"), headers=headers)
    except: pass
    return RedirectResponse(FALLBACK_IMAGE_URL)

@app.get("/api/proxy/user_image/{user_id}")
def proxy_user_image(user_id: str):
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    if not key: return Response(status_code=404)
    try:
        url = f"{host}/emby/Users/{user_id}/Images/Primary?width=200&height=200&mode=Crop"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            headers = {"Cache-Control": "public, max-age=31536000", "Access-Control-Allow-Origin": "*"}
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"), headers=headers)
    except: pass
    return Response(status_code=404)

@app.get("/api/stats/badges")
def api_badges(user_id: Optional[str] = None):
    try:
        where, params = get_base_filter(user_id)
        badges = []
        night_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '02' AND '05'", params)
        if night_res and night_res[0]['c'] > 5: badges.append({"id": "night", "name": "修仙党", "icon": "fa-moon", "color": "text-purple-500", "bg": "bg-purple-100", "desc": "深夜是灵魂最自由的时刻"})
        weekend_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%w', DateCreated) IN ('0', '6')", params)
        if weekend_res and weekend_res[0]['c'] > 10: badges.append({"id": "weekend", "name": "周末狂欢", "icon": "fa-champagne-glasses", "color": "text-pink-500", "bg": "bg-pink-100", "desc": "工作日唯唯诺诺，周末重拳出击"})
        dur_res = query_db(f"SELECT SUM(PlayDuration) as d FROM PlaybackActivity {where}", params)
        total_dur = dur_res[0]['d'] if dur_res and dur_res[0]['d'] else 0
        if total_dur > 360000: badges.append({"id": "liver", "name": "Emby肝帝", "icon": "fa-fire", "color": "text-red-500", "bg": "bg-red-100", "desc": "阅片无数，心中的码比片还厚"})
        type_res = query_db(f"SELECT ItemType, COUNT(*) as c FROM PlaybackActivity {where} GROUP BY ItemType", params)
        type_counts = {row['ItemType']: row['c'] for row in type_res or []}
        movies = type_counts.get('Movie', 0); episodes = type_counts.get('Episode', 0)
        if movies > 20 and movies > episodes: badges.append({"id": "movie", "name": "电影迷", "icon": "fa-film", "color": "text-blue-500", "bg": "bg-blue-100", "desc": "两小时体验一种人生"})
        elif episodes > 50 and episodes > movies: badges.append({"id": "series", "name": "追剧狂魔", "icon": "fa-tv", "color": "text-green-500", "bg": "bg-green-100", "desc": "下一集...再看一集就睡"})
        morning_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '06' AND '09'", params)
        if morning_res and morning_res[0]['c'] > 5: badges.append({"id": "morning", "name": "早起鸟", "icon": "fa-sun", "color": "text-orange-500", "bg": "bg-orange-100", "desc": "一日之计在于晨"})
        return {"status": "success", "data": badges}
    except: return {"status": "success", "data": []}

@app.get("/api/stats/monthly_stats")
def api_monthly_stats(user_id: Optional[str] = None):
    try:
        where_base, params = get_base_filter(user_id)
        where = where_base + " AND DateCreated > date('now', '-12 months')"
        sql = f"SELECT strftime('%Y-%m', DateCreated) as Month, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Month ORDER BY Month"
        results = query_db(sql, params)
        data = {}
        if results:
            for r in results: data[r['Month']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)