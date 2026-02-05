import sqlite3
import os
import uvicorn
import requests
import datetime
import json
import time
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

# ================= 配置区域 =================
PORT = 10307
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")
EMBY_HOST = os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/')
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "").strip()
FALLBACK_IMAGE_URL = "https://img.hotimg.com/a444d32a033994d5b.png"

print(f"--- EmbyPulse V42 (Library Stats & Dynamic Chart) ---")
print(f"DB Path: {DB_PATH}")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if not os.path.exists("static"): os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ================= 数据库工具 =================
# 优化: 改为 def 避免 async/sync 混用导致的阻塞
def query_db(query, args=(), one=False):
    if not os.path.exists(DB_PATH): return None
    try:
        # 增加 timeout 防止高并发下的死锁
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

def get_user_map():
    user_map = {}
    if EMBY_API_KEY:
        try:
            res = requests.get(f"{EMBY_HOST}/emby/Users?api_key={EMBY_API_KEY}", timeout=1)
            if res.status_code == 200:
                for u in res.json(): user_map[u['Id']] = u['Name']
        except: pass
    return user_map

# ================= 页面路由 =================
@app.get("/")
async def page_dashboard(request: Request): return templates.TemplateResponse("index.html", {"request": request, "active_page": "dashboard"})
@app.get("/content")
async def page_content(request: Request): return templates.TemplateResponse("content.html", {"request": request, "active_page": "content"})
@app.get("/report")
async def page_report(request: Request): return templates.TemplateResponse("report.html", {"request": request, "active_page": "report"})
@app.get("/details")
async def page_details(request: Request): return templates.TemplateResponse("details.html", {"request": request, "active_page": "details"})

# ================= API: 基础用户 =================
@app.get("/api/users")
def api_get_users():
    try:
        results = query_db("SELECT DISTINCT UserId FROM PlaybackActivity")
        if not results: return {"status": "success", "data": []}
        user_map = get_user_map()
        data = []
        for row in results:
            uid = row['UserId']
            if not uid: continue
            name = user_map.get(uid, f"User {str(uid)[:5]}")
            data.append({"UserId": uid, "UserName": name})
        data.sort(key=lambda x: x['UserName'])
        return {"status": "success", "data": data}
    except Exception as e: return {"status": "error", "message": str(e)}

# ================= API: 仪表盘 (含媒体库统计) =================
@app.get("/api/stats/dashboard")
def api_dashboard(user_id: Optional[str] = None):
    try:
        # 1. 播放历史统计
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)
        users = query_db(f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where} AND DateCreated > date('now', '-30 days')", params)
        dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
        
        base_stats = {
            "total_plays": plays[0]['c'] if plays else 0,
            "active_users": users[0]['c'] if users else 0,
            "total_duration": dur[0]['c'] if dur else 0
        }

        # 2. 媒体库库存统计 (调用 Emby API)
        # 只有在全服模式下才显示库存，或者你可以决定任何时候都显示
        library_stats = {"movie": 0, "series": 0, "episode": 0}
        if EMBY_API_KEY:
            try:
                # 调用 Emby Items Counts 接口
                url = f"{EMBY_HOST}/emby/Items/Counts?api_key={EMBY_API_KEY}"
                res = requests.get(url, timeout=2)
                if res.status_code == 200:
                    data = res.json()
                    library_stats["movie"] = data.get("MovieCount", 0)
                    library_stats["series"] = data.get("SeriesCount", 0)
                    library_stats["episode"] = data.get("EpisodeCount", 0)
            except Exception as e:
                print(f"⚠️ Library Stats Error: {e}")

        # 合并数据返回
        return {"status": "success", "data": {**base_stats, "library": library_stats}}

    except: return {"status": "error", "data": {"total_plays":0, "library": {}}}

@app.get("/api/stats/recent")
def api_recent_activity(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        sql = f"SELECT DateCreated, UserId, ItemId, ItemName, ItemType FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 200"
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
    except Exception as e: return {"status": "error", "data": []}

@app.get("/api/live")
def api_live_sessions():
    if not EMBY_API_KEY: return {"status": "error"}
    try:
        res = requests.get(f"{EMBY_HOST}/emby/Sessions?api_key={EMBY_API_KEY}", timeout=2)
        if res.status_code == 200:
            return {"status": "success", "data": [s for s in res.json() if s.get("NowPlayingItem")]}
    except: pass
    return {"status": "success", "data": []}

# ================= API: 排行/洞察/图表 =================
@app.get("/api/stats/top_movies")
def api_top_movies(user_id: Optional[str] = None, category: str = 'all', sort_by: str = 'count'):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
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
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
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

# 🔥 核心升级: 支持多维度的动态图表接口
@app.get("/api/stats/chart")
@app.get("/api/stats/trend")
def api_chart_stats(user_id: Optional[str] = None, dimension: str = 'day'):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        
        sql = ""
        # 1. 按周 (Week): 最近 12 周
        if dimension == 'week':
            where += " AND DateCreated > date('now', '-84 days')" # 12周 = 84天
            # SQLite 没有直接的 ISO 周函数，用 strftime('%W')
            # 统计总时长 (Duration)
            sql = f"SELECT strftime('%Y-W%W', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        
        # 2. 按月 (Month): 最近 12 个月
        elif dimension == 'month':
            where += " AND DateCreated > date('now', '-12 months')"
            sql = f"SELECT strftime('%Y-%m', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        
        # 3. 默认按日 (Day): 最近 30 天
        else:
            where += " AND DateCreated > date('now', '-30 days')"
            sql = f"SELECT date(DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
            
        results = query_db(sql, params)
        data = {}
        if results:
            for r in results: 
                # 返回的是秒，前端需要转为小时
                data[r['Label']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except Exception as e:
        print(f"Chart Error: {e}")
        return {"status": "error", "data": {}}

# ================= API: 海报生成 =================
@app.get("/api/stats/poster_data")
def api_poster_data(user_id: Optional[str] = None, period: str = 'all'):
    try:
        where, params = "WHERE 1=1", []
        date_filter = ""
        if period == 'week': date_filter = " AND DateCreated > date('now', '-7 days')"
        elif period == 'month': date_filter = " AND DateCreated > date('now', '-30 days')"
        elif period == 'year': date_filter = " AND DateCreated > date('now', '-1 year')"
        
        server_res = query_db(f"SELECT COUNT(*) as Plays FROM PlaybackActivity WHERE 1=1 {date_filter}")
        server_plays = server_res[0]['Plays'] if server_res else 0

        if user_id and user_id != 'all': 
            where += " AND UserId = ?"
            params.append(user_id)
        where += date_filter

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
        
        return {
            "status": "success",
            "data": {
                "plays": total_plays,
                "hours": total_hours,
                "server_plays": server_plays,
                "top_list": top_list,
                "tags": ["观影达人"]
            }
        }
    except Exception as e: return {"status": "error", "message": str(e), "data": {"plays": 0, "hours": 0, "server_plays": 0, "top_list": []}}

# ================= 辅助 API =================
@app.get("/api/stats/top_users_list")
def api_top_users_list():
    try:
        res = query_db("SELECT UserId, COUNT(*) as Plays, SUM(PlayDuration) as TotalTime FROM PlaybackActivity GROUP BY UserId ORDER BY TotalTime DESC LIMIT 5")
        if not res: return {"status": "success", "data": []}
        user_map = get_user_map()
        data = []
        for row in res:
            u = dict(row)
            u['UserName'] = user_map.get(u['UserId'], f"User {str(u['UserId'])[:5]}")
            data.append(u)
        return {"status": "success", "data": data}
    except: return {"status": "success", "data": []}

@app.get("/api/proxy/image/{item_id}/{img_type}")
def proxy_image(item_id: str, img_type: str):
    target_id = item_id
    if img_type == 'primary' and EMBY_API_KEY:
        try:
            r = requests.get(f"{EMBY_HOST}/emby/Items?Ids={item_id}&Fields=SeriesId,ParentId&Limit=1&api_key={EMBY_API_KEY}", timeout=1)
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
        resp = requests.get(f"{EMBY_HOST}/emby/Items/{target_id}{suffix}", timeout=3)
        if resp.status_code == 200: 
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"), headers=headers)
    except: pass
    return RedirectResponse(FALLBACK_IMAGE_URL)

@app.get("/api/proxy/user_image/{user_id}")
def proxy_user_image(user_id: str):
    if not EMBY_API_KEY: return Response(status_code=404)
    try:
        url = f"{EMBY_HOST}/emby/Users/{user_id}/Images/Primary?width=200&height=200&mode=Crop"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            headers = {"Cache-Control": "public, max-age=31536000", "Access-Control-Allow-Origin": "*"}
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"), headers=headers)
    except: pass
    return Response(status_code=404)

@app.get("/api/stats/badges")
def api_badges(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        
        badges = []
        
        night_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '02' AND '05'", params)
        if night_res and night_res[0]['c'] > 5:
            badges.append({"id": "night", "name": "修仙党", "icon": "fa-moon", "color": "text-purple-500", "bg": "bg-purple-100", "desc": "深夜是灵魂最自由的时刻"})
            
        weekend_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%w', DateCreated) IN ('0', '6')", params)
        if weekend_res and weekend_res[0]['c'] > 10:
             badges.append({"id": "weekend", "name": "周末狂欢", "icon": "fa-champagne-glasses", "color": "text-pink-500", "bg": "bg-pink-100", "desc": "工作日唯唯诺诺，周末重拳出击"})

        dur_res = query_db(f"SELECT SUM(PlayDuration) as d FROM PlaybackActivity {where}", params)
        total_dur = dur_res[0]['d'] if dur_res and dur_res[0]['d'] else 0
        if total_dur > 360000:
             badges.append({"id": "liver", "name": "Emby肝帝", "icon": "fa-fire", "color": "text-red-500", "bg": "bg-red-100", "desc": "阅片无数，心中的码比片还厚"})

        type_res = query_db(f"SELECT ItemType, COUNT(*) as c FROM PlaybackActivity {where} GROUP BY ItemType", params)
        type_counts = {row['ItemType']: row['c'] for row in type_res or []}
        movies = type_counts.get('Movie', 0)
        episodes = type_counts.get('Episode', 0)
        
        if movies > 20 and movies > episodes:
             badges.append({"id": "movie", "name": "电影迷", "icon": "fa-film", "color": "text-blue-500", "bg": "bg-blue-100", "desc": "两小时体验一种人生"})
        elif episodes > 50 and episodes > movies:
             badges.append({"id": "series", "name": "追剧狂魔", "icon": "fa-tv", "color": "text-green-500", "bg": "bg-green-100", "desc": "下一集...再看一集就睡"})

        morning_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '06' AND '09'", params)
        if morning_res and morning_res[0]['c'] > 5:
            badges.append({"id": "morning", "name": "早起鸟", "icon": "fa-sun", "color": "text-orange-500", "bg": "bg-orange-100", "desc": "一日之计在于晨"})

        return {"status": "success", "data": badges}
    except Exception as e:
        print(f"Badge Error: {e}")
        return {"status": "success", "data": []}

@app.get("/api/stats/monthly_stats")
def api_monthly_stats(user_id: Optional[str] = None):
    try:
        where, params = "WHERE DateCreated > date('now', '-12 months')", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        sql = f"SELECT strftime('%Y-%m', DateCreated) as Month, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Month ORDER BY Month"
        results = query_db(sql, params)
        data = {}
        if results:
            for r in results: data[r['Month']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)