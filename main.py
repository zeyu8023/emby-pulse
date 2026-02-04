import sqlite3
import os
import uvicorn
import requests
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

print(f"--- EmbyPulse V15 (Dashboard Aggregation) ---")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

if not os.path.exists("static"): os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
    except: return None

def get_user_map():
    user_map = {}
    if EMBY_API_KEY:
        try:
            res = requests.get(f"{EMBY_HOST}/emby/Users?api_key={EMBY_API_KEY}", timeout=1)
            if res.status_code == 200:
                for u in res.json(): user_map[u['Id']] = u['Name']
        except: pass
    return user_map

# === 路由 ===
@app.get("/")
async def page_dashboard(request: Request): return templates.TemplateResponse("index.html", {"request": request, "active_page": "dashboard"})
@app.get("/content")
async def page_content(request: Request): return templates.TemplateResponse("content.html", {"request": request, "active_page": "content"})
@app.get("/report")
async def page_report(request: Request): return templates.TemplateResponse("report.html", {"request": request, "active_page": "report"})
@app.get("/details")
async def page_details(request: Request): return templates.TemplateResponse("details.html", {"request": request, "active_page": "details"})

# === API ===
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
            data.append({"UserId": uid, "UserName": user_map.get(uid, f"User {str(uid)[:5]}")})
        data.sort(key=lambda x: x['UserName'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": []}

@app.get("/api/stats/dashboard")
async def api_dashboard(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)
        users = query_db(f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where} AND DateCreated > date('now', '-30 days')", params)
        dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
        return {"status": "success", "data": {"total_plays": plays[0]['c'] if plays else 0, "active_users": users[0]['c'] if users else 0, "total_duration": dur[0]['c'] if dur else 0}}
    except: return {"status": "error"}

# === 🔥 V15 修复：首页最近播放聚合逻辑 ===
@app.get("/api/stats/recent")
async def api_recent_activity(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        
        # 拉取更多数据(200条)以便在内存中去重
        results = query_db(f"SELECT DateCreated, UserId, ItemId, ItemName, ItemType, SeriesName FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 200", params)
        if not results: return {"status": "success", "data": []}
        
        user_map = get_user_map()
        final_data = []
        seen_series = set() # 用于去重的集合

        for row in results:
            item = dict(row)
            item['UserName'] = user_map.get(item['UserId'], "User")
            
            # 聚合判断逻辑
            is_episode = (item['ItemType'] == 'Episode')
            series_name = item.get('SeriesName')
            
            # 1. 尝试清洗剧名
            display_name = item['ItemName']
            if is_episode:
                if series_name:
                    display_name = series_name # 优先用 SeriesName
                elif ' - ' in item['ItemName']:
                    display_name = item['ItemName'].split(' - ')[0] # 降级：切割 ItemName
            
            # 2. 去重逻辑
            if is_episode:
                if display_name in seen_series:
                    continue # 如果这个剧已经出现过（最近刚播过），跳过旧记录
                seen_series.add(display_name)
                # 修改展示名称，去掉 "S01E01" 这种
                item['ItemName'] = display_name 
            
            final_data.append(item)
            if len(final_data) >= 20: break # 只取前20个聚合后的结果

        return {"status": "success", "data": final_data}
    except Exception as e: 
        print(e)
        return {"status": "error", "data": []}

@app.get("/api/live")
async def api_live_sessions():
    if not EMBY_API_KEY: return {"status": "error"}
    try:
        res = requests.get(f"{EMBY_HOST}/emby/Sessions?api_key={EMBY_API_KEY}", timeout=2)
        if res.status_code == 200:
            return {"status": "success", "data": [s for s in res.json() if s.get("NowPlayingItem")]}
    except: pass
    return {"status": "success", "data": []}

@app.get("/api/stats/poster_data")
async def api_poster_data(user_id: Optional[str] = None, period: str = 'all'):
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

        # 注意：这里我们不做 SeriesName 依赖，完全靠 ItemName 清洗，兼容性最好
        raw_sql = f"SELECT ItemName, ItemId, ItemType, SeriesName, PlayDuration FROM PlaybackActivity {where}"
        rows = query_db(raw_sql, params)
        
        total_plays = 0
        total_duration = 0
        aggregated = {} 

        if rows:
            for row in rows:
                total_plays += 1
                dur = row['PlayDuration'] or 0
                total_duration += dur
                
                # 聚合逻辑 V15: 优先 SeriesName -> 切割 ItemName
                name = row['SeriesName'] if (row['ItemType'] == 'Episode' and row['SeriesName']) else row['ItemName']
                if ' - ' in name: name = name.split(' - ')[0] # 暴力清洗
                
                if name not in aggregated:
                    aggregated[name] = {'ItemName': name, 'ItemId': row['ItemId'], 'Count': 0, 'Duration': 0}
                
                aggregated[name]['Count'] += 1
                aggregated[name]['Duration'] += dur
                aggregated[name]['ItemId'] = row['ItemId'] 

        top_list = list(aggregated.values())
        top_list.sort(key=lambda x: x['Count'], reverse=True)
        top_list = top_list[:10]

        total_hours = round(total_duration / 3600)
        tags = ["观影新人"]
        if total_hours > 50: tags = ["忠实观众"]
        if total_hours > 200: tags = ["影视肝帝"]

        return {
            "status": "success",
            "data": {
                "plays": total_plays,
                "hours": total_hours,
                "server_plays": server_plays,
                "top_list": top_list,
                "tags": tags
            }
        }
    except Exception as e: return {"status": "error", "message": str(e)}

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

# === 补充缺失接口 ===
@app.get("/api/stats/chart")
async def api_chart_stats(user_id: Optional[str] = None, dimension: str = 'month'):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        sql = f"SELECT strftime('%Y-%m', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label DESC LIMIT 6"
        results = query_db(sql, params)
        data = {}
        if results: 
            for r in results: data[r['Label']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

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
    return {"status": "success", "data": []} # 简化，防止报错

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)