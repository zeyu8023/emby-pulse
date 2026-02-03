import sqlite3
import os
import uvicorn
import requests
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

# === 配置 ===
PORT = 10307
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")
# 获取 Emby 地址，默认为本地
EMBY_HOST = os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/')

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件
if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

# 设置模板引擎
templates = Jinja2Templates(directory="templates")

# === 数据库工具 ===
def query_db(query, args=(), one=False):
    if not os.path.exists(DB_PATH):
        # 避免报错，返回 None
        return None
    try:
        # 只读模式连接
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, args)
        rv = cur.fetchall()
        conn.close()
        return (rv[0] if rv else None) if one else rv
    except Exception as e:
        print(f"Database Error: {e}")
        return None

# === 页面路由 ===

@app.get("/")
async def page_dashboard(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "active_page": "dashboard"
    })

@app.get("/content")
async def page_content(request: Request):
    return templates.TemplateResponse("content.html", {
        "request": request,
        "active_page": "content"
    })

@app.get("/report")
async def page_report(request: Request):
    return templates.TemplateResponse("report.html", {
        "request": request,
        "active_page": "report"
    })

# === API 数据接口 ===

@app.get("/api/stats/dashboard")
async def api_dashboard():
    try:
        res_plays = query_db("SELECT COUNT(*) as c FROM PlaybackActivity")
        total_plays = res_plays[0]['c'] if res_plays else 0
        
        res_users = query_db("SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity WHERE DateCreated > date('now', '-30 days')")
        active_users = res_users[0]['c'] if res_users else 0
        
        res_duration = query_db("SELECT SUM(PlayDuration) as c FROM PlaybackActivity")
        total_duration = res_duration[0]['c'] if res_duration and res_duration[0]['c'] else 0

        return {
            "status": "success",
            "data": {
                "total_plays": total_plays,
                "active_users": active_users,
                "total_duration": total_duration
            }
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/stats/top_movies")
async def api_top_movies():
    sql = """
    SELECT ItemName, ItemId, COUNT(*) as PlayCount, SUM(PlayDuration) as TotalTime
    FROM PlaybackActivity
    GROUP BY ItemId, ItemName
    ORDER BY PlayCount DESC
    LIMIT 10
    """
    try:
        results = query_db(sql)
        data = []
        if results:
            for row in results:
                data.append(dict(row))
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# 🔥🔥🔥 新增：图片中转接口 🔥🔥🔥
@app.get("/api/proxy/image/{item_id}/{img_type}")
async def proxy_image(item_id: str, img_type: str):
    """
    中转 Emby 图片，解决内网/HTTPS 混合内容问题
    img_type: 'primary' (封面) 或 'backdrop' (背景)
    """
    if img_type == 'backdrop':
        emby_url = f"{EMBY_HOST}/emby/Items/{item_id}/Images/Backdrop?maxWidth=800&quality=80"
    else:
        emby_url = f"{EMBY_HOST}/emby/Items/{item_id}/Images/Primary?maxHeight=400&quality=90"
    
    try:
        # 后端请求 Emby
        resp = requests.get(emby_url, timeout=5)
        if resp.status_code == 200:
            # 直接把图片数据“转发”给浏览器
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
        else:
            return Response(status_code=404)
    except Exception as e:
        print(f"Proxy Error: {e}")
        return Response(status_code=404)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
