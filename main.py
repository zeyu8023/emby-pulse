import sqlite3
import os
import uvicorn
import requests
import datetime
import json
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

print(f"--- EmbyPulse V16 (Debug Edition) Starting ---")
print(f"DB Path: {DB_PATH}")
if os.path.exists(DB_PATH):
    print(f"✅ 数据库文件存在")
else:
    print(f"❌ 严重错误: 数据库文件不存在! 请检查 Docker 映射路径!")

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
def query_db(query, args=(), one=False):
    if not os.path.exists(DB_PATH): 
        print("❌ DB Query Failed: File not found")
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, args)
        rv = cur.fetchall()
        conn.close()
        return (rv[0] if rv else None) if one else rv
    except Exception as e:
        print(f"❌ SQL Execution Error: {e}\nQuery: {query}")
        return None

def get_user_map():
    user_map = {}
    if EMBY_API_KEY:
        try:
            res = requests.get(f"{EMBY_HOST}/emby/Users?api_key={EMBY_API_KEY}", timeout=1)
            if res.status_code == 200:
                for u in res.json(): user_map[u['Id']] = u['Name']
        except Exception as e:
            print(f"⚠️ User Map Error: {e}")
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

# ================= API 接口 =================

@app.get("/api/users")
async def api_get_users():
    print("\n🔍 [API] 请求用户列表...")
    try:
        results = query_db("SELECT DISTINCT UserId FROM PlaybackActivity")
        if not results: 
            print("⚠️ [API] PlaybackActivity 表为空或查询无结果")
            return {"status": "success", "data": []}
        
        user_map = get_user_map()
        data = []
        for row in results:
            uid = row['UserId']
            if not uid: continue
            name = user_map.get(uid, f"User {str(uid)[:5]}")
            data.append({"UserId": uid, "UserName": name})
        
        data.sort(key=lambda x: x['UserName'])
        print(f"✅ [API] 返回 {len(data)} 个用户")
        return {"status": "success", "data": data}
    except Exception as e: 
        print(f"❌ [API] Get Users Error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/stats/dashboard")
async def api_dashboard(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)
        users = query_db(f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where} AND DateCreated > date('now', '-30 days')", params)
        dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
        
        return {"status": "success", "data": {
            "total_plays": plays[0]['c'] if plays else 0,
            "active_users": users[0]['c'] if users else 0,
            "total_duration": dur[0]['c'] if dur else 0
        }}
    except: return {"status": "error", "data": {"total_plays":0, "active_users":0, "total_duration":0}}

# === 🔥 首页最近播放 (DEBUG版) ===
@app.get("/api/stats/recent")
async def api_recent_activity(user_id: Optional[str] = None):
    print(f"\n🔍 [Recent] 请求最近播放: User={user_id}")
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
            
        # 简单直接的查询，确保先出数据
        sql = f"SELECT DateCreated, UserId, ItemId, ItemName, ItemType, SeriesName FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 50"
        results = query_db(sql, params)
        
        if not results: 
            print("⚠️ [Recent] 查询结果为空")
            return {"status": "success", "data": []}

        print(f"✅ [Recent] 数据库返回 {len(results)} 条原始记录")
        
        user_map = get_user_map()
        data = []
        for row in results:
            item = dict(row)
            item['UserName'] = user_map.get(item['UserId'], "User")
            
            # 简单的名称处理，不做过度聚合防止数据消失
            display_name = item['ItemName']
            if item['ItemType'] == 'Episode' and item['SeriesName']:
                display_name = f"{item['SeriesName']} - {item['ItemName']}"
            
            item['DisplayName'] = display_name
            data.append(item)
            
        return {"status": "success", "data": data}
    except Exception as e: 
        print(f"❌ [Recent] Error: {e}")
        return {"status": "error", "data": []}

@app.get("/api/live")
async def api_live_sessions():
    if not EMBY_API_KEY: return {"status": "error", "message": "No API Key"}
    try:
        res = requests.get(f"{EMBY_HOST}/emby/Sessions?api_key={EMBY_API_KEY}", timeout=2)
        if res.status_code == 200:
            sessions = []
            for s in res.json():
                if s.get("NowPlayingItem"):
                    sessions.append(s)
            return {"status": "success", "data": sessions}
    except: pass
    return {"status": "success", "data": []}

# === 🔥 海报数据接口 (DEBUG版) ===
@app.get("/api/stats/poster_data")
async def api_poster_data(user_id: Optional[str] = None, period: str = 'all'):
    print(f"\n📊 [Poster] 生成请求: User={user_id}, Period={period}")
    try:
        where, params = "WHERE 1=1", []
        date_filter = ""
        
        if period == 'week': date_filter = " AND DateCreated > date('now', '-7 days')"
        elif period == 'month': date_filter = " AND DateCreated > date('now', '-30 days')"
        elif period == 'year': date_filter = " AND DateCreated > date('now', '-1 year')"
        
        # 全服数据
        server_sql = f"SELECT COUNT(*) as Plays FROM PlaybackActivity WHERE 1=1 {date_filter}"
        server_res = query_db(server_sql)
        server_plays = server_res[0]['Plays'] if server_res else 0
        print(f"   🌍 全服播放: {server_plays}")

        # 个人数据
        if user_id and user_id != 'all': 
            where += " AND UserId = ?"
            params.append(user_id)
        where += date_filter

        raw_sql = f"SELECT ItemName, ItemId, ItemType, SeriesName, PlayDuration FROM PlaybackActivity {where}"
        print(f"   🔍 SQL: {raw_sql} | Params: {params}")
        
        rows = query_db(raw_sql, params)
        
        total_plays = 0
        total_duration = 0
        aggregated = {} 

        if rows:
            print(f"   ✅ 查到 {len(rows)} 条个人记录，开始聚合...")
            for row in rows:
                total_plays += 1
                dur = row['PlayDuration'] or 0
                total_duration += dur
                
                # 智能聚合逻辑
                name = row['SeriesName'] if (row['ItemType'] == 'Episode' and row['SeriesName']) else row['ItemName']
                if not name: name = row['ItemName'] # Fallback
                if not name: name = "Unknown"
                
                # 清洗后缀
                if ' - ' in name: name = name.split(' - ')[0]
                
                if name not in aggregated:
                    aggregated[name] = {'ItemName': name, 'ItemId': row['ItemId'], 'Count': 0, 'Duration': 0}
                
                aggregated[name]['Count'] += 1
                aggregated[name]['Duration'] += dur
                aggregated[name]['ItemId'] = row['ItemId'] 
        else:
            print("   ⚠️ 查无个人记录")

        top_list = list(aggregated.values())
        top_list.sort(key=lambda x: x['Count'], reverse=True)
        top_list = top_list[:10]

        total_hours = round(total_duration / 3600)
        
        print(f"   🏁 聚合完成: Top榜 {len(top_list)} 个条目")

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
    except Exception as e:
        print(f"❌ [Poster] Fatal Error: {e}")
        return {"status": "error", "message": str(e), "data": {"plays": 0, "hours": 0, "server_plays": 0, "top_list": []}}

# === 图片代理 (增强容错) ===
@app.get("/api/proxy/image/{item_id}/{img_type}")
async def proxy_image(item_id: str, img_type: str):
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
        # 直接透传 Emby 响应
        emby_url = f"{EMBY_HOST}/emby/Items/{target_id}{suffix}"
        # print(f"Proxying: {emby_url}") 
        resp = requests.get(emby_url, timeout=5)
        if resp.status_code == 200:
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
    except: pass
    
    return RedirectResponse(FALLBACK_IMAGE_URL)

# === 其他接口 ===
@app.get("/api/stats/chart")
async def api_chart_stats(user_id: Optional[str] = None, dimension: str = 'month'):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        sql = ""
        if dimension == 'year':
            sql = f"SELECT strftime('%Y', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label DESC LIMIT 5"
        elif dimension == 'day':
            where += " AND DateCreated > date('now', '-30 days')"
            sql = f"SELECT date(DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        else:
            where += " AND DateCreated > date('now', '-12 months')"
            sql = f"SELECT strftime('%Y-%m', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        results = query_db(sql, params)
        data = {}
        if results:
            rows = results[::-1] if dimension == 'year' else results
            for r in rows: data[r['Label']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

@app.get("/api/stats/user_details")
async def api_user_details(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        
        hourly_res = query_db(f"SELECT strftime('%H', DateCreated) as Hour, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Hour ORDER BY Hour", params)
        hourly_data = {str(i).zfill(2): 0 for i in range(24)}
        if hourly_res:
            for r in hourly_res: hourly_data[r['Hour']] = r['Plays']
            
        device_res = query_db(f"SELECT COALESCE(DeviceName, ClientName, 'Unknown') as Device, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Device ORDER BY Plays DESC", params)
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

@app.get("/api/stats/top_users_list")
async def api_top_users_list():
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

@app.get("/api/stats/top_movies")
async def api_top_movies(user_id: Optional[str] = None, category: str = 'all', sort_by: str = 'count'):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        if category == 'Movie': where += " AND ItemType = 'Movie'"
        elif category == 'Episode': where += " AND ItemType = 'Episode'"
        order = "ORDER BY PlayCount DESC" if sort_by == 'count' else "ORDER BY TotalTime DESC"
        sql = f"SELECT ItemName, ItemId, ItemType, COUNT(*) as PlayCount, SUM(PlayDuration) as TotalTime FROM PlaybackActivity {where} GROUP BY ItemId, ItemName {order} LIMIT 20"
        results = query_db(sql, params)
        return {"status": "success", "data": [dict(r) for r in results] if results else []}
    except: return {"status": "error", "data": []}

@app.get("/api/stats/badges")
async def api_badges(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        badges = []
        night_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '02' AND '05'", params)
        if night_res and night_res[0]['c'] > 5:
            badges.append({"id": "night", "name": "修仙党", "icon": "fa-moon", "color": "text-purple-500", "bg": "bg-purple-100", "desc": "深夜是灵魂最自由的时刻"})
        dur_res = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
        if dur_res and dur_res[0]['c'] and dur_res[0]['c'] > 360000:
            badges.append({"id": "king", "name": "影视肝帝", "icon": "fa-crown", "color": "text-yellow-600", "bg": "bg-yellow-100", "desc": "阅片量惊人"})
        return {"status": "success", "data": badges}
    except: return {"status": "success", "data": []}

@app.get("/api/stats/monthly_stats")
async def api_monthly_stats(user_id: Optional[str] = None):
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