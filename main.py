import sqlite3
import os
import uvicorn
import requests
import secrets
import random
from fastapi import FastAPI, Request, Response, Depends, HTTPException, status, Form
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from typing import Optional

# ================= 配置区域 =================
PORT = 10307
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")
EMBY_HOST = os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/')
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "").strip() 
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "") 
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))

FALLBACK_IMAGE_URL = "https://img.hotimg.com/a444d32a033994d5b.png"

print(f"--- EmbyPulse V37 (Full Integrity Check) ---")
print(f"DB Path: {DB_PATH}")

app = FastAPI()

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400*7)

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

# ================= 🔐 权限控制核心 =================
def get_current_user(request: Request):
    user = request.session.get("user")
    if not user:
        # API请求返回401，页面请求返回None让路由处理跳转
        if request.url.path.startswith("/api") and not request.url.path.startswith("/api/auth"):
            raise HTTPException(status_code=401, detail="Not authenticated")
        return None
    return user

# ================= 页面路由 =================
@app.get("/login")
async def page_login(request: Request):
    if request.session.get("user"): return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/")
async def page_dashboard(request: Request): 
    user = request.session.get("user")
    if not user: return RedirectResponse("/login")
    return templates.TemplateResponse("index.html", {"request": request, "user": user, "active_page": "dashboard"})

@app.get("/report")
async def page_report(request: Request): 
    user = request.session.get("user")
    if not user: return RedirectResponse("/login")
    return templates.TemplateResponse("report.html", {"request": request, "user": user, "active_page": "report"})

@app.get("/content")
async def page_content(request: Request):
    user = request.session.get("user")
    if not user: return RedirectResponse("/login")
    return templates.TemplateResponse("content.html", {"request": request, "user": user, "active_page": "content"})

@app.get("/details")
async def page_details(request: Request):
    user = request.session.get("user")
    if not user: return RedirectResponse("/login")
    return templates.TemplateResponse("details.html", {"request": request, "user": user, "active_page": "details"})

# ================= 🔐 认证 API =================
@app.post("/api/auth/login")
async def api_login(request: Request, username: str = Form(...), password: str = Form(...)):
    headers = {"X-Emby-Authorization": 'MediaBrowser Client="EmbyPulse", Device="Web", DeviceId="EmbyPulseServer", Version="1.0.0"'}
    payload = {"Username": username, "Pw": password}
    try:
        url = f"{EMBY_HOST}/emby/Users/AuthenticateByName"
        resp = requests.post(url, json=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            user_info = data.get("User", {})
            request.session["user"] = {
                "id": user_info.get("Id"),
                "name": user_info.get("Name"),
                "is_admin": user_info.get("Policy", {}).get("IsAdministrator", False),
                "token": data.get("AccessToken")
            }
            return {"status": "success"}
        else:
            return JSONResponse(status_code=401, content={"status": "error", "message": "用户名或密码错误"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/api/auth/logout")
async def api_logout(request: Request):
    request.session.clear()
    return {"status": "success"}

@app.get("/api/auth/me")
async def api_me(user: dict = Depends(get_current_user)):
    return {"status": "success", "data": user}

@app.get("/api/tmdb/backdrop")
async def api_tmdb_backdrop():
    fallback = ["https://image.tmdb.org/t/p/original/mSDsSDwaP3E7dEfUPWy4J0djt4O.jpg"]
    if not TMDB_API_KEY: return {"url": random.choice(fallback)}
    try:
        url = f"https://api.themoviedb.org/3/trending/movie/week?api_key={TMDB_API_KEY}&language=zh-CN"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            valid_bgs = [m['backdrop_path'] for m in results if m.get('backdrop_path')]
            if valid_bgs: return {"url": f"https://image.tmdb.org/t/p/original{random.choice(valid_bgs)}"}
    except: pass
    return {"url": random.choice(fallback)}

# ================= 📊 核心业务 API (所有功能回归) =================

@app.get("/api/users")
async def api_get_users(current_user: dict = Depends(get_current_user)):
    try:
        if not current_user['is_admin']:
            return {"status": "success", "data": [{"UserId": current_user['id'], "UserName": current_user['name']}]}
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

@app.get("/api/stats/dashboard")
async def api_dashboard(user_id: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    if not current_user['is_admin']: user_id = current_user['id']
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
async def api_recent_activity(user_id: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    if not current_user['is_admin']: user_id = current_user['id']
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
            clean_name = item['ItemName'].split(' - ')[0] if ' - ' in item['ItemName'] else item['ItemName']
            item['DisplayName'] = clean_name
            if item['ItemType'] == 'Episode':
                if clean_name in seen_keys: continue
                seen_keys.add(clean_name)
            final_data.append(item)
            if len(final_data) >= 20: break 
        return {"status": "success", "data": final_data}
    except Exception as e: return {"status": "error", "data": []}

@app.get("/api/live")
async def api_live_sessions(current_user: dict = Depends(get_current_user)):
    # 实时会话通常需要管理员权限，或者只显示自己的，这里为了简单，如果有 KEY 就显示
    if not EMBY_API_KEY: return {"status": "error"}
    try:
        res = requests.get(f"{EMBY_HOST}/emby/Sessions?api_key={EMBY_API_KEY}", timeout=2)
        if res.status_code == 200:
            return {"status": "success", "data": [s for s in res.json() if s.get("NowPlayingItem")]}
    except: pass
    return {"status": "success", "data": []}

@app.get("/api/stats/top_movies")
async def api_top_movies(user_id: Optional[str] = None, category: str = 'all', sort_by: str = 'count', current_user: dict = Depends(get_current_user)):
    if not current_user['is_admin']: user_id = current_user['id']
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        if category == 'Movie': where += " AND ItemType = 'Movie'"
        elif category == 'Episode': where += " AND ItemType = 'Episode'"
        sql = f"SELECT ItemName, ItemId, ItemType, PlayDuration FROM PlaybackActivity {where}"
        rows = query_db(sql, params)
        aggregated = {}
        for row in rows:
            clean_name = row['ItemName'].split(' - ')[0] if ' - ' in row['ItemName'] else row['ItemName']
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
async def api_user_details(user_id: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    if not current_user['is_admin']: user_id = current_user['id']
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

@app.get("/api/stats/chart")
async def api_chart_stats(user_id: Optional[str] = None, dimension: str = 'month', current_user: dict = Depends(get_current_user)):
    if not current_user['is_admin']: user_id = current_user['id']
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        sql = ""
        if dimension == 'year':
             where += " AND DateCreated > date('now', '-12 months')"
             sql = f"SELECT strftime('%Y-%m', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        elif dimension == 'day':
            where += " AND DateCreated > date('now', '-30 days')"
            sql = f"SELECT date(DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        else:
            where += " AND DateCreated > date('now', '-6 months')"
            sql = f"SELECT strftime('%Y-%m', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        results = query_db(sql, params)
        data = {}
        if results:
            for r in results: data[r['Label']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

@app.get("/api/stats/poster_data")
async def api_poster_data(user_id: Optional[str] = None, period: str = 'all', current_user: dict = Depends(get_current_user)):
    if not current_user['is_admin']: user_id = current_user['id']
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
        
        total_plays, total_duration = 0, 0
        aggregated = {} 
        if rows:
            for row in rows:
                total_plays += 1
                dur = row['PlayDuration'] or 0
                total_duration += dur
                clean_name = row['ItemName'].split(' - ')[0] if ' - ' in row['ItemName'] else row['ItemName']
                if clean_name not in aggregated:
                    aggregated[clean_name] = {'ItemName': clean_name, 'ItemId': row['ItemId'], 'Count': 0, 'Duration': 0}
                aggregated[clean_name]['Count'] += 1
                aggregated[clean_name]['Duration'] += dur
                aggregated[clean_name]['ItemId'] = row['ItemId'] 

        top_list = list(aggregated.values())
        top_list.sort(key=lambda x: x['Count'], reverse=True)
        top_list = top_list[:10]
        
        return {"status": "success", "data": {"plays": total_plays, "hours": round(total_duration / 3600), "server_plays": server_plays, "top_list": top_list}}
    except Exception as e: return {"status": "error", "message": str(e), "data": {"plays": 0, "hours": 0, "server_plays": 0, "top_list": []}}

@app.get("/api/stats/top_users_list")
async def api_top_users_list():
    try:
        res = query_db("SELECT UserId, COUNT(*) as Plays, SUM(PlayDuration) as TotalTime FROM PlaybackActivity GROUP BY UserId ORDER BY TotalTime DESC LIMIT 5")
        if not res: return {"status": "success", "data": []}
        user_map = get_user_map()
        data = []
        for row in res:
            u = dict(row); u['UserName'] = user_map.get(u['UserId'], f"User {str(u['UserId'])[:5]}"); data.append(u)
        return {"status": "success", "data": data}
    except: return {"status": "success", "data": []}

@app.get("/api/proxy/image/{item_id}/{img_type}")
async def proxy_image(item_id: str, img_type: str):
    target_id = item_id
    if img_type == 'primary' and EMBY_API_KEY:
        try:
            r = requests.get(f"{EMBY_HOST}/emby/Items?Ids={item_id}&Fields=SeriesId,ParentId&Limit=1&api_key={EMBY_API_KEY}", timeout=1)
            if r.status_code == 200 and r.json().get("Items"):
                item = r.json()["Items"][0]
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
async def proxy_user_image(user_id: str):
    if not EMBY_API_KEY: return Response(status_code=404)
    try:
        url = f"{EMBY_HOST}/emby/Users/{user_id}/Images/Primary?maxHeight=200"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            headers = {"Cache-Control": "public, max-age=31536000", "Access-Control-Allow-Origin": "*"}
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"), headers=headers)
    except: pass
    return Response(status_code=404)

@app.get("/api/stats/badges")
async def api_badges(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        badges = []
        night_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '02' AND '05'", params)
        if night_res and night_res[0]['c'] > 5:
            badges.append({"id": "night", "name": "修仙党", "icon": "fa-moon", "color": "text-purple-500", "bg": "bg-purple-100", "desc": "深夜是灵魂最自由的时刻"})
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