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
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "") # 获取 API Key

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

# === 数据库工具 ===
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
        print(f"DB Error: {e}")
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
    """获取有过播放记录的所有用户"""
    try:
        sql = "SELECT DISTINCT UserId, UserName FROM PlaybackActivity WHERE UserName IS NOT NULL ORDER BY UserName"
        users = query_db(sql)
        return {"status": "success", "data": [dict(u) for u in users] if users else []}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === API: 仪表盘数据 (支持筛选) ===
@app.get("/api/stats/dashboard")
async def api_dashboard(user_id: Optional[str] = None):
    try:
        # 构建 SQL 条件
        where_clause = "WHERE 1=1"
        params = []
        if user_id and user_id != 'all':
            where_clause += " AND UserId = ?"
            params.append(user_id)

        # 1. 总播放次数
        sql_plays = f"SELECT COUNT(*) as c FROM PlaybackActivity {where_clause}"
        res_plays = query_db(sql_plays, params)
        total_plays = res_plays[0]['c'] if res_plays else 0
        
        # 2. 活跃用户数 (如果是单用户模式，这里就是1)
        sql_users = f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where_clause} AND DateCreated > date('now', '-30 days')"
        res_users = query_db(sql_users, params)
        active_users = res_users[0]['c'] if res_users else 0
        
        # 3. 总时长
        sql_dur = f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where_clause}"
        res_duration = query_db(sql_dur, params)
        total_duration = res_duration[0]['c'] if res_duration and res_duration[0]['c'] else 0

        return {"status": "success", "data": {"total_plays": total_plays, "active_users": active_users, "total_duration": total_duration}}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === API: 热门内容 (支持筛选) ===
@app.get("/api/stats/top_movies")
async def api_top_movies(user_id: Optional[str] = None):
    where_clause = ""
    params = []
    if user_id and user_id != 'all':
        where_clause = "WHERE UserId = ?"
        params.append(user_id)

    # 包含 ItemType 以便判断是否为剧集
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

# === API: 智能图片中转 (核心升级) ===
@app.get("/api/proxy/image/{item_id}/{img_type}")
async def proxy_image(item_id: str, img_type: str):
    """
    智能获取封面：
    1. 如果是背景图 (backdrop)，直接取。
    2. 如果是封面 (primary) 且有 API Key：
       - 先查这个 Item 是不是 Episode。
       - 如果是 Episode，查它的 SeriesId。
       - 取 SeriesId 的封面 (这样就是漂亮的海报了)。
    """
    target_id = item_id
    
    # 只有在请求封面、且配了 API Key 时，才尝试“智能查找剧集海报”
    if img_type == 'primary' and EMBY_API_KEY:
        try:
            # 查 Item 详情
            info_url = f"{EMBY_HOST}/emby/Items/{item_id}?api_key={EMBY_API_KEY}"
            info_resp = requests.get(info_url, timeout=2)
            if info_resp.status_code == 200:
                data = info_resp.json()
                # 如果是单集，且有剧集ID，就用剧集ID取图
                if data.get('Type') == 'Episode' and data.get('SeriesId'):
                    target_id = data.get('SeriesId')
        except Exception:
            pass # 查不到就降级用原图

    # 拼接最终图片 URL
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
