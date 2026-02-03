import sqlite3
import os
import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# === 配置 ===
PORT = 10307
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")

app = FastAPI()

# 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件 (确保 static 目录存在)
if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

# 设置模板引擎
templates = Jinja2Templates(directory="templates")

# === 数据库工具 ===
def query_db(query, args=(), one=False):
    """
    连接 SQLite 数据库并执行查询。
    如果数据库文件不存在，返回空数据，防止报错。
    """
    if not os.path.exists(DB_PATH):
        print(f"⚠️ Warning: Database file not found at {DB_PATH}")
        return None
    
    try:
        # 使用只读模式 (ro) 打开，避免锁死 Emby
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, args)
        rv = cur.fetchall()
        conn.close()
        return (rv[0] if rv else None) if one else rv
    except Exception as e:
        print(f"❌ Database Error: {e}")
        return None

# === 页面路由 (HTML渲染) ===

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

# === API 接口 (数据提供) ===

@app.get("/api/stats/dashboard")
async def api_dashboard():
    """获取仪表盘基础数据"""
    try:
        # 1. 总播放次数
        res_plays = query_db("SELECT COUNT(*) as c FROM PlaybackActivity")
        total_plays = res_plays[0]['c'] if res_plays else 0
        
        # 2. 活跃用户数 (最近30天)
        res_users = query_db("""
            SELECT COUNT(DISTINCT UserId) as c 
            FROM PlaybackActivity 
            WHERE DateCreated > date('now', '-30 days')
        """)
        active_users = res_users[0]['c'] if res_users else 0
        
        # 3. 总时长 (秒)
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
    """获取播放最多的内容"""
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

# === 启动入口 ===
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
