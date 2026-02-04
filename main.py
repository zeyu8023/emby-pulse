import sqlite3
import os
import uvicorn
import requests
import datetime
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

# ================= 配置区域 =================
# 服务端口
PORT = 10307
# 数据库路径
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")
# Emby 服务器地址 (例如 http://192.168.1.5:8096)
EMBY_HOST = os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/')
# Emby API Key (必须填写，用于图片代理和实时监控)
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "").strip()
# 默认图片 (当 Emby 图片加载失败时的兜底图)
FALLBACK_IMAGE_URL = "https://img.hotimg.com/a444d32a033994d5b.png"

print(f"--- EmbyPulse Ultimate V8 Starting ---")
print(f"DB Path: {DB_PATH}")
print(f"API Key: {'✅ Loaded' if EMBY_API_KEY else '❌ Not Set (Images/Live disabled)'}")

app = FastAPI()

# 允许跨域请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件和模板目录
if not os.path.exists("static"): os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ================= 数据库工具 =================
def query_db(query, args=(), one=False):
    """通用数据库查询函数"""
    if not os.path.exists(DB_PATH):
        print(f"Error: DB file not found at {DB_PATH}")
        return None
    try:
        # 使用只读模式连接，防止锁库
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

def get_user_map():
    """获取 Emby 用户 ID 到 用户名 的映射表"""
    user_map = {}
    if EMBY_API_KEY:
        try:
            res = requests.get(f"{EMBY_HOST}/emby/Users?api_key={EMBY_API_KEY}", timeout=2)
            if res.status_code == 200:
                for u in res.json():
                    user_map[u['Id']] = u['Name']
        except:
            pass
    return user_map

# ================= 页面路由 =================
@app.get("/")
async def page_dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "active_page": "dashboard"})

@app.get("/content")
async def page_content(request: Request):
    return templates.TemplateResponse("content.html", {"request": request, "active_page": "content"})

@app.get("/report")
async def page_report(request: Request):
    return templates.TemplateResponse("report.html", {"request": request, "active_page": "report"})

@app.get("/details")
async def page_details(request: Request):
    return templates.TemplateResponse("details.html", {"request": request, "active_page": "details"})

# ================= API 接口 =================

@app.get("/api/users")
async def api_get_users():
    """获取所有有播放记录的用户"""
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

@app.get("/api/stats/dashboard")
async def api_dashboard(user_id: Optional[str] = None):
    """仪表盘核心数据统计"""
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
    except Exception as e: return {"status": "error", "message": str(e)}

@app.get("/api/stats/recent")
async def api_recent_activity(user_id: Optional[str] = None):
    """最近播放记录 (返回50条给前端展示)"""
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
            
        results = query_db(f"SELECT DateCreated, UserId, ItemId, ItemName, ItemType, PlayDuration FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 300", params)
        if not results: return {"status": "success", "data": []}

        raw_items = [dict(row) for row in results]
        user_map = get_user_map()
        metadata_map = {}
        
        # 预加载元数据
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
            
            # 尝试聚合剧集信息
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

@app.get("/api/live")
async def api_live_sessions():
    """实时播放监控"""
    if not EMBY_API_KEY: return {"status": "error", "message": "No API Key"}
    try:
        url = f"{EMBY_HOST}/emby/Sessions?api_key={EMBY_API_KEY}"
        res = requests.get(url, timeout=3)
        if res.status_code != 200: return {"status": "error", "data": []}
        
        sessions = []
        for s in res.json():
            if s.get("NowPlayingItem"):
                info = {
                    "User": s.get("UserName", "Guest"),
                    "Client": s.get("Client", "Unknown"),
                    "Device": s.get("DeviceName", "Unknown"),
                    "ItemName": s["NowPlayingItem"].get("Name"),
                    "SeriesName": s["NowPlayingItem"].get("SeriesName", ""),
                    "ItemId": s["NowPlayingItem"].get("Id"),
                    "IsTranscoding": s.get("PlayState", {}).get("PlayMethod") == "Transcode",
                    "Percentage": int((s.get("PlayState", {}).get("PositionTicks", 0) / (s["NowPlayingItem"].get("RunTimeTicks", 1) or 1)) * 100)
                }
                sessions.append(info)
        return {"status": "success", "data": sessions}
    except Exception as e: return {"status": "error", "message": str(e)}

# === 🔥 映迹工坊核心数据接口 (V8 - 智能聚合 + 全服数据 + Top10) ===
@app.get("/api/stats/poster_data")
async def api_poster_data(user_id: Optional[str] = None, period: str = 'all'):
    """
    period: 'all' | 'year' | 'month' | 'week'
    返回: 个人统计, 全服统计, Top10(智能聚合后)
    """
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        
        # 1. 时间过滤
        date_filter = ""
        if period == 'week': date_filter = " AND DateCreated > date('now', '-7 days')"
        elif period == 'month': date_filter = " AND DateCreated > date('now', '-30 days')"
        elif period == 'year': date_filter = " AND DateCreated > date('now', '-1 year')"
        
        where += date_filter
        
        # 2. 全服数据统计 (Server Stats)
        server_where = f"WHERE 1=1 {date_filter}" 
        server_sql = f"SELECT COUNT(*) as Plays FROM PlaybackActivity {server_where}"
        server_res = query_db(server_sql)
        server_plays = server_res[0]['Plays'] if server_res else 0

        # 3. 个人数据 - 原始记录拉取
        # 我们拉取所有记录到内存处理，以实现“同一剧集聚合”的逻辑
        raw_sql = f"SELECT ItemName, ItemId, ItemType, SeriesName, PlayDuration FROM PlaybackActivity {where}"
        raw_rows = query_db(raw_sql, params)
        
        total_plays = 0
        total_duration = 0
        aggregated = {} # 聚合字典

        if raw_rows:
            for row in raw_rows:
                total_plays += 1
                dur = row['PlayDuration'] or 0
                total_duration += dur
                
                # --- 聚合核心逻辑 ---
                # 优先级: 如果有 SeriesName，则按 SeriesName 聚合；否则按 ItemName 聚合
                if row['ItemType'] == 'Episode' and row['SeriesName']:
                    key = row['SeriesName']
                    # 剧集的图片 ID 暂时用这一集的，前端代理会自动尝试查找 SeriesId
                    display_id = row['ItemId'] 
                else:
                    # 电影
                    key = row['ItemName']
                    # 去除 " - 1080p" 等后缀，提高聚合度
                    if ' - ' in key and row['ItemType'] == 'Movie':
                        key = key.split(' - ')[0]
                    display_id = row['ItemId']

                if key not in aggregated:
                    aggregated[key] = {
                        'ItemName': key,
                        'ItemId': display_id, 
                        'Count': 0,
                        'Duration': 0
                    }
                
                aggregated[key]['Count'] += 1
                aggregated[key]['Duration'] += dur
                # 更新 ID (保持最新)
                aggregated[key]['ItemId'] = display_id

        # 4. 排序并取 Top 10
        top_list = list(aggregated.values())
        # 优先按次数，其次按时长
        top_list.sort(key=lambda x: (x['Count'], x['Duration']), reverse=True)
        top_list = top_list[:10]

        # 5. 计算标签
        total_hours = round(total_duration / 3600)
        tags = []
        if total_hours > 500: tags.append("影视肝帝")
        elif total_hours > 100: tags.append("忠实观众")
        
        # 简单的时间段统计 (为了性能不再查库，如果需要精确可另加逻辑)
        if not tags: tags.append("佛系观众")

        return {
            "status": "success",
            "data": {
                "plays": total_plays,
                "hours": total_hours,
                "server_plays": server_plays,
                "top_list": top_list,
                "tags": tags[:2],
                "active_hour": "--" 
            }
        }
    except Exception as e: return {"status": "error", "message": str(e)}

# === 趋势图接口 ===
@app.get("/api/stats/chart")
async def api_chart_stats(user_id: Optional[str] = None, dimension: str = 'month'):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        
        sql = ""
        if dimension == 'year':
            sql = f"SELECT strftime('%Y', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label DESC LIMIT 5"
        elif dimension == 'day':
            where += " AND DateCreated > date('now', '-30 days')"
            sql = f"SELECT date(DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        else:
            where += " AND DateCreated > date('now', '-12 months')"
            sql = f"SELECT strftime('%Y-%m', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
            
        results = query_db(sql, params)
        data = {}
        if results:
            rows = results[::-1] if dimension == 'year' else results
            for r in rows: data[r['Label']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

# === 详情页接口 ===
@app.get("/api/stats/user_details")
async def api_user_details(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        
        hourly_res = query_db(f"SELECT strftime('%H', DateCreated) as Hour, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Hour ORDER BY Hour", params)
        hourly_data = {str(i).zfill(2): 0 for i in range(24)}
        if hourly_res:
            for r in hourly_res: hourly_data[r['Hour']] = r['Plays']
            
        device_res = query_db(f"SELECT COALESCE(DeviceName, ClientName, 'Unknown') as Device, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Device ORDER BY Plays DESC", params)
        logs_res = query_db(f"SELECT DateCreated, ItemName, PlayDuration, COALESCE(DeviceName, ClientName) as Device, UserId FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 100", params)
        
        user_map = get_user_map()
        logs_data = []
        if logs_res:
            for r in logs_res:
                l = dict(r)
                l['UserName'] = user_map.get(l['UserId'], "User")
                logs_data.append(l)
        return {"status": "success", "data": {"hourly": hourly_data, "devices": [dict(r) for r in device_res] if device_res else [], "logs": logs_data}}
    except Exception as e: return {"status": "error", "message": str(e)}

# === 榜单接口 ===
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
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        if category == 'Movie': where += " AND ItemType = 'Movie'"
        elif category == 'Episode': where += " AND ItemType = 'Episode'"
        order = "ORDER BY PlayCount DESC" if sort_by == 'count' else "ORDER BY TotalTime DESC"
        sql = f"SELECT ItemName, ItemId, ItemType, COUNT(*) as PlayCount, SUM(PlayDuration) as TotalTime FROM PlaybackActivity {where} GROUP BY ItemId, ItemName {order} LIMIT 20"
        results = query_db(sql, params)
        return {"status": "success", "data": [dict(r) for r in results] if results else []}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.get("/api/stats/badges")
async def api_badges(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        badges = []
        night_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '02' AND '05'", params)
        if night_res and night_res[0]['c'] > 5:
            badges.append({"id": "night", "name": "修仙党", "icon": "fa-moon", "color": "text-purple-500", "bg": "bg-purple-100", "desc": "深夜是灵魂最自由的时刻"})
        dur_res = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
        if dur_res and dur_res[0]['c'] and dur_res[0]['c'] > 360000:
            badges.append({"id": "king", "name": "影视肝帝", "icon": "fa-crown", "color": "text-yellow-600", "bg": "bg-yellow-100", "desc": "阅片量惊人"})
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

# === 图片代理 (核心) ===
@app.get("/api/proxy/image/{item_id}/{img_type}")
async def proxy_image(item_id: str, img_type: str):
    """代理图片，支持智能回退到剧集封面"""
    target_id = item_id
    attempted_smart = False
    
    if img_type == 'primary' and EMBY_API_KEY:
        try:
            info_resp = requests.get(f"{EMBY_HOST}/emby/Items?Ids={item_id}&Fields=SeriesId,ParentId&Limit=1&api_key={EMBY_API_KEY}", timeout=2)
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
        
        # 失败回退
        if attempted_smart and target_id != item_id:
            fallback_resp = requests.get(f"{EMBY_HOST}/emby/Items/{item_id}{suffix}", timeout=5)
            if fallback_resp.status_code == 200:
                return Response(content=fallback_resp.content, media_type=fallback_resp.headers.get("Content-Type", "image/jpeg"))
    except: pass
    
    return RedirectResponse(FALLBACK_IMAGE_URL)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)