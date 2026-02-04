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

print(f"--- EmbyPulse V18 (All Features Restored) ---")
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
def query_db(query, args=(), one=False):
    if not os.path.exists(DB_PATH): return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, args)
        rv = cur.fetchall()
        conn.close()
        return (rv[0] if rv else None) if one else rv
    except Exception as e:
        print(f"❌ SQL Error: {e} | Query: {query}")
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

# ================= API: 基础数据 =================

@app.get("/api/users")
async def api_get_users():
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

# ================= API: 仪表盘 (Dashboard) =================

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
    except: return {"status": "error", "data": {"total_plays":0}}

@app.get("/api/stats/recent")
async def api_recent_activity(user_id: Optional[str] = None):
    # 首页最近播放：智能聚合，不依赖 SeriesName
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
            
            # 清洗名称 (模拟 SeriesName)
            if ' - ' in raw_name:
                clean_name = raw_name.split(' - ')[0]
            
            item['DisplayName'] = clean_name
            
            if item['ItemType'] == 'Episode':
                if clean_name in seen_keys: continue
                seen_keys.add(clean_name)
            
            final_data.append(item)
            if len(final_data) >= 20: break 

        return {"status": "success", "data": final_data}
    except Exception as e: return {"status": "error", "data": []}

@app.get("/api/live")
async def api_live_sessions():
    if not EMBY_API_KEY: return {"status": "error"}
    try:
        res = requests.get(f"{EMBY_HOST}/emby/Sessions?api_key={EMBY_API_KEY}", timeout=2)
        if res.status_code == 200:
            return {"status": "success", "data": [s for s in res.json() if s.get("NowPlayingItem")]}
    except: pass
    return {"status": "success", "data": []}

# ================= API: 内容排行 (Content Ranking) [已恢复] =================

@app.get("/api/stats/top_movies")
async def api_top_movies(user_id: Optional[str] = None, category: str = 'all', sort_by: str = 'count'):
    # 内容排行页面的数据源
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        
        if category == 'Movie': where += " AND ItemType = 'Movie'"
        elif category == 'Episode': where += " AND ItemType = 'Episode'"
        
        # 为了兼容性，我们先取出所有相关记录，然后在 Python 里聚合
        # 这样即使没有 SeriesName 也能排行
        sql = f"SELECT ItemName, ItemId, ItemType, PlayDuration FROM PlaybackActivity {where}"
        rows = query_db(sql, params)
        
        aggregated = {}
        for row in rows:
            raw_name = row['ItemName']
            # 聚合逻辑
            clean_name = raw_name
            if ' - ' in raw_name: clean_name = raw_name.split(' - ')[0]
            
            if clean_name not in aggregated:
                aggregated[clean_name] = {'ItemName': clean_name, 'ItemId': row['ItemId'], 'PlayCount': 0, 'TotalTime': 0}
            
            aggregated[clean_name]['PlayCount'] += 1
            aggregated[clean_name]['TotalTime'] += (row['PlayDuration'] or 0)
            aggregated[clean_name]['ItemId'] = row['ItemId'] # Keep latest ID

        # 转换为列表并排序
        result_list = list(aggregated.values())
        if sort_by == 'time':
            result_list.sort(key=lambda x: x['TotalTime'], reverse=True)
        else:
            result_list.sort(key=lambda x: x['PlayCount'], reverse=True)
            
        return {"status": "success", "data": result_list[:50]} # 返回前50
    except Exception as e: 
        print(f"❌ Top Movies Error: {e}")
        return {"status": "error", "data": []}

# ================= API: 数据洞察 (Details) [已恢复] =================

@app.get("/api/stats/user_details")
async def api_user_details(user_id: Optional[str] = None):
    # 数据洞察页面的数据源：小时分布、设备分布、最近日志
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        
        # 1. 24小时分布
        hourly_res = query_db(f"SELECT strftime('%H', DateCreated) as Hour, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Hour ORDER BY Hour", params)
        hourly_data = {str(i).zfill(2): 0 for i in range(24)}
        if hourly_res:
            for r in hourly_res: hourly_data[r['Hour']] = r['Plays']
            
        # 2. 设备分布 (兼容 ClientName)
        device_res = query_db(f"SELECT COALESCE(DeviceName, ClientName, 'Unknown') as Device, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Device ORDER BY Plays DESC LIMIT 10", params)
        
        # 3. 详细日志 (最近100条)
        logs_res = query_db(f"SELECT DateCreated, ItemName, PlayDuration, COALESCE(DeviceName, ClientName) as Device, UserId FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 100", params)
        
        user_map = get_user_map()
        logs_data = []
        if logs_res:
            for r in logs_res:
                l = dict(r)
                l['UserName'] = user_map.get(l['UserId'], "User")
                logs_data.append(l)
                
        return {"status": "success", "data": {
            "hourly": hourly_data, 
            "devices": [dict(r) for r in device_res] if device_res else [],
            "logs": logs_data
        }}
    except Exception as e: 
        print(f"❌ User Details Error: {e}")
        return {"status": "error", "data": {"hourly": {}, "devices": [], "logs": []}}

# ================= API: 趋势图表 (Chart) [已恢复] =================

@app.get("/api/stats/chart")
async def api_chart_stats(user_id: Optional[str] = None, dimension: str = 'month'):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        
        sql = ""
        # 过去12个月
        if dimension == 'year':
             where += " AND DateCreated > date('now', '-12 months')"
             sql = f"SELECT strftime('%Y-%m', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        # 过去30天
        elif dimension == 'day':
            where += " AND DateCreated > date('now', '-30 days')"
            sql = f"SELECT date(DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        # 默认
        else:
            where += " AND DateCreated > date('now', '-6 months')"
            sql = f"SELECT strftime('%Y-%m', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
            
        results = query_db(sql, params)
        data = {}
        if results:
            for r in results: data[r['Label']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

# ================= API: 海报生成 (Poster) =================

@app.get("/api/stats/poster_data")
async def api_poster_data(user_id: Optional[str] = None, period: str = 'all'):
    print(f"\n📊 [Poster] User={user_id}, Period={period}")
    try:
        where, params = "WHERE 1=1", []
        date_filter = ""
        if period == 'week': date_filter = " AND DateCreated > date('now', '-7 days')"
        elif period == 'month': date_filter = " AND DateCreated > date('now', '-30 days')"
        elif period == 'year': date_filter = " AND DateCreated > date('now', '-1 year')"
        
        # 全服数据
        server_res = query_db(f"SELECT COUNT(*) as Plays FROM PlaybackActivity WHERE 1=1 {date_filter}")
        server_plays = server_res[0]['Plays'] if server_res else 0

        # 个人数据
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
                
                # 智能聚合
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
        resp = requests.get(f"{EMBY_HOST}/emby/Items/{target_id}{suffix}", timeout=3)
        if resp.status_code == 200: return Response(content=resp.content, media_type="image/jpeg")
    except: pass
    return RedirectResponse(FALLBACK_IMAGE_URL)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)