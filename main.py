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

# 启动日志
print(f"--- EmbyPulse 启动 ---")
print(f"数据库路径: {DB_PATH}")
print(f"API Key: {'✅ 已加载' if EMBY_API_KEY else '❌ 未加载'}")

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
        print(f"❌ 错误: 找不到数据库文件 {DB_PATH}")
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
        print(f"❌ SQL执行错误: {e}")
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

# === API: 用户列表 (暴力修复版) ===
@app.get("/api/users")
async def api_get_users():
    try:
        print("🔍 正在扫描用户列表...")
        # 1. 简单粗暴：只查 UserId 和 UserName，不分组
        sql = "SELECT UserId, UserName FROM PlaybackActivity"
        results = query_db(sql)
        
        if not results:
            print("⚠️ 警告: 数据库没有返回任何播放记录")
            return {"status": "success", "data": []}

        # 2. 在 Python 内存中去重 (由 Python 处理最稳妥)
        users_map = {}
        for row in results:
            uid = row['UserId']
            name = row['UserName']
            
            # 跳过空ID
            if not uid: continue
            
            # 如果名字为空，给个默认名
            if not name: name = f"User {str(uid)[:5]}"
            
            # 存入字典 (自动去重，保留最后一次遇到的名字)
            users_map[uid] = name

        # 3. 转回列表并排序
        data = [{"UserId": k, "UserName": v} for k, v in users_map.items()]
        data.sort(key=lambda x: x['UserName']) # 按名字排序
        
        print(f"✅ 成功找到 {len(data)} 个用户: {[u['UserName'] for u in data]}")
        return {"status": "success", "data": data}
        
    except Exception as e:
        print(f"❌ 用户API严重错误: {e}")
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

        res_plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where_clause}", params)
        total_plays = res_plays[0]['c'] if res_plays else 0
        
        active_sql = f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where_clause} AND DateCreated > date('now', '-30 days')"
        res_users = query_db(active_sql, params)
        active_users = res_users[0]['c'] if res_users else 0
        
        res_dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where_clause}", params)
        total_duration = res_dur[0]['c'] if res_dur and res_dur[0]['c'] else 0

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

# === API: 图片代理 ===
@app.get("/api/proxy/image/{item_id}/{img_type}")
async def proxy_image(item_id: str, img_type: str):
    target_id = item_id
    
    # 智能搜图
    if img_type == 'primary' and EMBY_API_KEY:
        try:
            info_url = f"{EMBY_HOST}/emby/Items?Ids={item_id}&Fields=SeriesId,ParentId&Limit=1&api_key={EMBY_API_KEY}"
            info_resp = requests.get(info_url, timeout=3)
            if info_resp.status_code == 200:
                data = info_resp.json()
                if data.get("Items"):
                    item_info = data["Items"][0]
                    if item_info.get('Type') == 'Episode':
                        if item_info.get('SeriesId'): target_id = item_info.get('SeriesId')
                        elif item_info.get('ParentId'): target_id = item_info.get('ParentId')
        except Exception:
            pass

    if img_type == 'backdrop':
        emby_url = f"{EMBY_HOST}/emby/Items/{target_id}/Images/Backdrop?maxWidth=800&quality=80"
    else:
        emby_url = f"{EMBY_HOST}/emby/Items/{target_id}/Images/Primary?maxHeight=400&quality=90"
    
    try:
        resp = requests.get(emby_url, timeout=5)
        return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
    except:
        return Response(status_code=404)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
