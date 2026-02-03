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

# === 配置 ===
PORT = 10307
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")
EMBY_HOST = os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/')
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "").strip()
FALLBACK_IMAGE_URL = "https://img.hotimg.com/a444d32a033994d5b.png"

print(f"--- EmbyPulse 启动 ---")
print(f"DB: {DB_PATH}")
print(f"API: {'✅ 已加载' if EMBY_API_KEY else '❌ 未加载'}")

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
    except Exception: return None

def get_user_map():
    user_map = {}
    if EMBY_API_KEY:
        try:
            res = requests.get(f"{EMBY_HOST}/emby/Users?api_key={EMBY_API_KEY}", timeout=2)
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

# === API: 用户列表 ===
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
    except Exception as e: return {"status": "error", "message": str(e)}

# === API: 仪表盘 ===
@app.get("/api/stats/dashboard")
async def api_dashboard(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)
        users = query_db(f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where} AND DateCreated > date('now', '-30 days')", params)
        dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
        return {"status": "success", "data": {
            "total_plays": plays[0]['c'] if plays else 0,
            "active_users": users[0]['c'] if users else 0,
            "total_duration": dur[0]['c'] if dur else 0
        }}
    except Exception as e: return {"status": "error", "message": str(e)}

# === API: 最近播放 ===
@app.get("/api/stats/recent")
async def api_recent_activity(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        
        results = query_db(f"SELECT DateCreated, UserId, ItemId, ItemName, ItemType, PlayDuration FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 300", params)
        if not results: return {"status": "success", "data": []}

        raw_items = [dict(row) for row in results]
        user_map = get_user_map()
        metadata_map = {}
        
        if EMBY_API_KEY:
            all_ids = [i['ItemId'] for i in raw_items][:100]
            chunk_size = 20
            for i in range(0, len(all_ids), chunk_size):
                try:
                    ids = ",".join(all_ids[i:i+chunk_size])
                    url = f"{EMBY_HOST}/emby/Items?Ids={ids}&Fields=SeriesId,SeriesName,ParentId&api_key={EMBY_API_KEY}"
                    res = requests.get(url, timeout=3)
                    if res.status_code == 200:
                        for m in res.json().get('Items', []): metadata_map[m['Id']] = m
                except: pass

        final_data = []
        seen_keys = set() 
        for item in raw_items:
            item['UserName'] = user_map.get(item['UserId'], "Unknown")
            display_id = item['ItemId']
            display_title = item['ItemName']
            unique_key = item['ItemName']
            meta = metadata_map.get(item['ItemId'])
            
            if meta:
                if meta.get('Type') == 'Episode':
                    if meta.get('SeriesId'):
                        display_id = meta.get('SeriesId')
                        unique_key = meta.get('SeriesId')
                        if meta.get('SeriesName'): display_title = meta.get('SeriesName')
            elif ' - ' in item['ItemName']:
                 display_title = item['ItemName'].split(' - ')[0]
                 unique_key = display_title

            if unique_key not in seen_keys:
                seen_keys.add(unique_key)
                item['DisplayId'] = display_id
                item['DisplayTitle'] = display_title
                final_data.append(item)
            if len(final_data) >= 50: break 
        return {"status": "success", "data": final_data}
    except Exception as e: return {"status": "error", "message": str(e)}

# === API: 用户排行 ===
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

# === 🔥 升级版 API: 内容风云榜 (支持分类和排序) ===
@app.get("/api/stats/top_movies")
async def api_top_movies(
    user_id: Optional[str] = None, 
    category: str = 'all',  # all, Movie, Episode
    sort_by: str = 'count'  # count, duration
):
    try:
        where = "WHERE 1=1"
        params = []
        
        # 1. 用户过滤
        if user_id and user_id != 'all': 
            where += " AND UserId = ?"
            params.append(user_id)
            
        # 2. 分类过滤 (关键升级)
        if category == 'Movie':
            where += " AND ItemType = 'Movie'"
        elif category == 'Episode':
            where += " AND ItemType = 'Episode'"
            
        # 3. 排序逻辑
        order_clause = "ORDER BY PlayCount DESC"
        if sort_by == 'duration':
            order_clause = "ORDER BY TotalTime DESC"
            
        sql = f"""
        SELECT ItemName, ItemId, ItemType, COUNT(*) as PlayCount, SUM(PlayDuration) as TotalTime
        FROM PlaybackActivity
        {where}
        GROUP BY ItemId, ItemName
        {order_clause}
        LIMIT 20
        """
        
        results = query_db(sql, params)
        if not results: return {"status": "success", "data": []}
        
        # 数据处理：如果是剧集，尝试获取 SeriesId 以合并封面
        # (注：SQL层面的合并比较复杂，这里先做简单的ID聚合，封面逻辑交给前端代理)
        data = []
        for row in results:
            data.append(dict(row))
            
        return {"status": "success", "data": data}
    except Exception as e: 
        return {"status": "error", "message": str(e)}

# === API: 图片代理 ===
@app.get("/api/proxy/image/{item_id}/{img_type}")
async def proxy_image(item_id: str, img_type: str):
    target_id = item_id
    attempted_smart = False
    
    if img_type == 'primary' and EMBY_API_KEY:
        try:
            info_url = f"{EMBY_HOST}/emby/Items?Ids={item_id}&Fields=SeriesId,ParentId&Limit=1&api_key={EMBY_API_KEY}"
            info_resp = requests.get(info_url, timeout=2)
            if info_resp.status_code == 200:
                attempted_smart = True
                data = info_resp.json()
                if data.get("Items"):
                    item = data["Items"][0]
                    if item.get('Type') == 'Episode':
                        if item.get('SeriesId'): target_id = item.get('SeriesId')
                        elif item.get('ParentId'): target_id = item.get('ParentId')
        except: pass

    suffix = "/Images/Backdrop?maxWidth=800" if img_type == 'backdrop' else "/Images/Primary?maxHeight=400"
    
    try:
        resp = requests.get(f"{EMBY_HOST}/emby/Items/{target_id}{suffix}", timeout=5)
        if resp.status_code == 200:
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
        
        if attempted_smart and target_id != item_id:
            fallback_resp = requests.get(f"{EMBY_HOST}/emby/Items/{item_id}{suffix}", timeout=5)
            if fallback_resp.status_code == 200:
                return Response(content=fallback_resp.content, media_type=fallback_resp.headers.get("Content-Type", "image/jpeg"))
    except: pass
    
    return RedirectResponse(FALLBACK_IMAGE_URL)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
