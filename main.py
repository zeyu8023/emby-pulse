import sqlite3
import os
import uvicorn
import requests
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

# === 配置 ===
PORT = 10307
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")
EMBY_HOST = os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/')
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "").strip() # 去除可能存在的空格

print(f"--- EmbyPulse 启动 ---")
print(f"DB: {DB_PATH}")
print(f"API: {'✅ 已加载' if EMBY_API_KEY else '❌ 未加载'}")

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
        print(f"SQL Error: {e}")
        return None

def get_user_map():
    user_map = {}
    if EMBY_API_KEY:
        try:
            res = requests.get(f"{EMBY_HOST}/emby/Users?api_key={EMBY_API_KEY}", timeout=2)
            if res.status_code == 200:
                for u in res.json(): user_map[u['Id']] = u['Name']
        except: pass
    return user_map

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
        sql = "SELECT DISTINCT UserId FROM PlaybackActivity"
        results = query_db(sql)
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
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === API: 仪表盘数据 ===
@app.get("/api/stats/dashboard")
async def api_dashboard(user_id: Optional[str] = None):
    try:
        where = "WHERE 1=1"
        params = []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        res_plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)
        total_plays = res_plays[0]['c'] if res_plays else 0
        active_sql = f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where} AND DateCreated > date('now', '-30 days')"
        res_users = query_db(active_sql, params)
        active_users = res_users[0]['c'] if res_users else 0
        res_dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
        total_duration = res_dur[0]['c'] if res_dur and res_dur[0]['c'] else 0
        return {"status": "success", "data": {"total_plays": total_plays, "active_users": active_users, "total_duration": total_duration}}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === 🔥 超级去重版 API: 最近播放 ===
@app.get("/api/stats/recent")
async def api_recent_activity(user_id: Optional[str] = None):
    try:
        where = "WHERE 1=1"
        params = []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        
        # 1. 扩大搜索范围到 60 条，保证有足够数据去重
        sql = f"""
        SELECT DateCreated, UserId, ItemId, ItemName, ItemType, PlayDuration 
        FROM PlaybackActivity 
        {where}
        ORDER BY DateCreated DESC 
        LIMIT 60
        """
        results = query_db(sql, params)
        if not results: return {"status": "success", "data": []}

        raw_items = [dict(row) for row in results]
        user_map = get_user_map()
        
        # 2. 批量查元数据 (分批处理，每批 20 个，防止 URL 过长报错)
        metadata_map = {}
        all_ids = [item['ItemId'] for item in raw_items]
        
        if EMBY_API_KEY:
            chunk_size = 20
            for i in range(0, len(all_ids), chunk_size):
                chunk_ids = all_ids[i:i + chunk_size]
                if not chunk_ids: continue
                try:
                    ids_str = ",".join(chunk_ids)
                    url = f"{EMBY_HOST}/emby/Items?Ids={ids_str}&Fields=SeriesId,SeriesName,ParentId&api_key={EMBY_API_KEY}"
                    res = requests.get(url, timeout=4)
                    if res.status_code == 200:
                        for meta in res.json().get('Items', []):
                            metadata_map[meta['Id']] = meta
                except: pass

        # 3. 强力去重逻辑
        final_data = []
        seen_keys = set() 

        for item in raw_items:
            item['UserName'] = user_map.get(item['UserId'], "Unknown")
            
            # 默认值
            display_id = item['ItemId']
            display_title = item['ItemName']
            is_episode = False
            
            # A. 优先尝试 API 元数据
            meta = metadata_map.get(item['ItemId'])
            if meta:
                if meta.get('Type') == 'Episode':
                    is_episode = True
                    if meta.get('SeriesId'):
                        display_id = meta.get('SeriesId') # 用剧集ID做封面
                        if meta.get('SeriesName'):
                             display_title = meta.get('SeriesName') # 用剧集名做标题
            
            # B. 兜底策略：如果 API 没查到，但名字看起来像单集，强制文本分析
            # 例子: "海市蜃楼 - S01E04 - xxx" -> 截取 "海市蜃楼"
            if not meta or (is_episode and display_id == item['ItemId']):
                original_name = item['ItemName']
                # 简单特征识别
                if ' - ' in original_name:
                    parts = original_name.split(' - ')
                    # 假设第一部分是剧名
                    display_title = parts[0]
                    # 使用剧名作为去重键（权宜之计，虽然 ID 还是单集 ID，但至少能在列表中只保留一个名字）
                    # 注意：如果没有 API，我们拿不到 SeriesId，只能用单集封面，但我们可以控制不显示重复的“剧名”
                    
            # 构造唯一键：如果是剧集，我们希望只显示一次
            # 如果拿到了 SeriesId，用 SeriesId 去重 (完美)
            # 如果没拿到，用 清洗后的剧名 去重 (凑合，但能防止刷屏)
            if is_episode and meta and meta.get('SeriesId'):
                unique_key = meta.get('SeriesId')
            else:
                unique_key = display_title # 文本去重
            
            if unique_key not in seen_keys:
                seen_keys.add(unique_key)
                item['DisplayId'] = display_id
                item['DisplayTitle'] = display_title
                final_data.append(item)
            
            # 只展示 14 个，凑齐一排 (电脑端 7列 x 2行 = 14)
            if len(final_data) >= 14: 
                break
                
        return {"status": "success", "data": final_data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === API: 用户排行榜 ===
@app.get("/api/stats/top_users_list")
async def api_top_users_list():
    try:
        sql = """
        SELECT UserId, COUNT(*) as Plays, SUM(PlayDuration) as TotalTime
        FROM PlaybackActivity
        GROUP BY UserId
        ORDER BY TotalTime DESC
        LIMIT 5
        """
        results = query_db(sql)
        data = []
        user_map = get_user_map()
        if results:
            for row in results:
                u = dict(row)
                u['UserName'] = user_map.get(u['UserId'], f"User {str(u['UserId'])[:5]}")
                data.append(u)
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === API: 热门内容 ===
@app.get("/api/stats/top_movies")
async def api_top_movies(user_id: Optional[str] = None):
    where = ""
    params = []
    if user_id and user_id != 'all':
        where = "WHERE UserId = ?"
        params.append(user_id)
    sql = f"""
    SELECT ItemName, ItemId, COUNT(*) as PlayCount, SUM(PlayDuration) as TotalTime
    FROM PlaybackActivity
    {where}
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
    if img_type == 'primary' and EMBY_API_KEY:
        try:
            info_url = f"{EMBY_HOST}/emby/Items?Ids={item_id}&Fields=SeriesId,ParentId&Limit=1&api_key={EMBY_API_KEY}"
            info_resp = requests.get(info_url, timeout=3)
            if info_resp.status_code == 200:
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
        return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
    except:
        return Response(status_code=404)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
