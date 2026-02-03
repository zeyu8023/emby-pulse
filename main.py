import sqlite3
import os
import uvicorn
import requests
from fastapi import FastAPI, Request, Response, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

# === 配置 ===
PORT = 10307
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")
EMBY_HOST = os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/')
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "")

# 启动时打印配置检查
print(f"--- 启动检查 ---")
print(f"DB_PATH: {DB_PATH}")
print(f"EMBY_HOST: {EMBY_HOST}")
print(f"EMBY_API_KEY: {'已加载 (长度 ' + str(len(EMBY_API_KEY)) + ')' if EMBY_API_KEY else '❌ 未加载 (封面功能将失效)'}")
print(f"----------------")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

def query_db(query, args=(), one=False):
    if not os.path.exists(DB_PATH): 
        print(f"❌ 数据库文件不存在: {DB_PATH}")
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
        print(f"❌ 数据库错误: {e}")
        return None

# === 页面路由 ===
@app.get("/")
async def page_dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "active_page": "dashboard"})

@app.get("/content")
async def page_content(request: Request):
    return templates.TemplateResponse("content.html", {"request": request, "active_page": "content"})

@app.get("/report")
async def page_report(request: Request):
    return templates.TemplateResponse("report.html", {"request": request, "active_page": "report"})

# === API: 用户列表 ===
@app.get("/api/users")
async def api_get_users():
    try:
        # 这里加了 UserName IS NOT NULL 且不为空字符串
        sql = "SELECT DISTINCT UserId, UserName FROM PlaybackActivity WHERE UserName IS NOT NULL AND UserName != '' ORDER BY UserName"
        users = query_db(sql)
        user_list = [dict(u) for u in users] if users else []
        print(f"✅ 加载用户列表: 找到 {len(user_list)} 个用户")
        return {"status": "success", "data": user_list}
    except Exception as e:
        print(f"❌ 用户列表加载失败: {e}")
        return {"status": "error", "message": str(e)}

# === API: 仪表盘 ===
@app.get("/api/stats/dashboard")
async def api_dashboard(user_id: Optional[str] = None):
    try:
        where_clause = "WHERE 1=1"
        params = []
        if user_id and user_id != 'all':
            where_clause += " AND UserId = ?"
            params.append(user_id)

        sql_plays = f"SELECT COUNT(*) as c FROM PlaybackActivity {where_clause}"
        res_plays = query_db(sql_plays, params)
        total_plays = res_plays[0]['c'] if res_plays else 0
        
        sql_users = f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where_clause} AND DateCreated > date('now', '-30 days')"
        res_users = query_db(sql_users, params)
        active_users = res_users[0]['c'] if res_users else 0
        
        sql_dur = f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where_clause}"
        res_duration = query_db(sql_dur, params)
        total_duration = res_duration[0]['c'] if res_duration and res_duration[0]['c'] else 0

        return {"status": "success", "data": {"total_plays": total_plays, "active_users": active_users, "total_duration": total_duration}}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === API: 热门内容 ===
@app.get("/api/stats/top_movies")
async def api_top_movies(user_id: Optional[str] = None):
    where_clause = ""
    params = []
    if user_id and user_id != 'all':
        where_clause = "WHERE UserId = ?"
        params.append(user_id)

    sql = f"""
    SELECT ItemName, ItemId, ItemType, COUNT(*) as PlayCount, SUM(PlayDuration) as TotalTime
    FROM PlaybackActivity
    {where_clause}
    GROUP BY ItemId, ItemName
    ORDER BY PlayCount DESC
    LIMIT 10
    """
    try:
        results = query_db(sql, params)
        data = [dict(row) for row in results] if results else []
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === API: 智能图片中转 ===
@app.get("/api/proxy/image/{item_id}/{img_type}")
async def proxy_image(item_id: str, img_type: str):
    target_id = item_id
    
    # 智能查找逻辑
    if img_type == 'primary' and EMBY_API_KEY:
        try:
            info_url = f"{EMBY_HOST}/emby/Items/{item_id}?api_key={EMBY_API_KEY}"
            info_resp = requests.get(info_url, timeout=2)
            
            if info_resp.status_code == 200:
                data = info_resp.json()
                # 逻辑：如果是 Episode，优先找 SeriesId，其次找 ParentId
                if data.get('Type') == 'Episode':
                    if data.get('SeriesId'):
                        target_id = data.get('SeriesId')
                    elif data.get('ParentId'): # 有时候只有 ParentId
                        target_id = data.get('ParentId')
            else:
                print(f"⚠️ Emby API 访问失败 [{info_resp.status_code}]: {info_url}")
        except Exception as e:
            print(f"⚠️ 智能查图出错: {e}")

    # 拼图
    if img_type == 'backdrop':
        emby_url = f"{EMBY_HOST}/emby/Items/{target_id}/Images/Backdrop?maxWidth=800&quality=80"
    else:
        emby_url = f"{EMBY_HOST}/emby/Items/{target_id}/Images/Primary?maxHeight=400&quality=90"
    
    try:
        resp = requests.get(emby_url, timeout=5)
        if resp.status_code == 200:
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
        else:
            return Response(status_code=404)
    except Exception:
        return Response(status_code=404)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
