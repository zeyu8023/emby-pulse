from fastapi import APIRouter
from typing import Optional
from app.core.config import cfg
from app.core.database import query_db, get_base_filter
import requests

router = APIRouter()

# --- 内部工具函数：获取用户映射 ---
# 独立于 bot_service，防止循环依赖导致 API 崩溃
def get_user_map_local():
    user_map = {}
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    if key and host:
        try:
            res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=2)
            if res.status_code == 200:
                for u in res.json(): 
                    user_map[u['Id']] = u['Name']
        except: 
            pass
    return user_map

@router.get("/api/stats/dashboard")
def api_dashboard(user_id: Optional[str] = None):
    try:
        where, params = get_base_filter(user_id)
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)[0]['c']
        # 活跃用户：过去30天有播放记录的
        users = query_db(f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where} AND DateCreated > date('now', '-30 days')", params)[0]['c']
        dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)[0]['c'] or 0
        
        base = {"total_plays": plays, "active_users": users, "total_duration": dur}
        lib = {"movie": 0, "series": 0, "episode": 0}
        
        key = cfg.get("emby_api_key")
        host = cfg.get("emby_host")
        if key and host:
            try:
                # 增加超时，防止卡住
                res = requests.get(f"{host}/emby/Items/Counts?api_key={key}", timeout=5)
                if res.status_code == 200:
                    d = res.json()
                    lib = {
                        "movie": d.get("MovieCount", 0), 
                        "series": d.get("SeriesCount", 0), 
                        "episode": d.get("EpisodeCount", 0)
                    }
            except Exception as e: 
                print(f"⚠️ Dashboard Emby API Error: {e}")
                
        return {"status": "success", "data": {**base, "library": lib}}
    except Exception as e: 
        print(f"⚠️ Dashboard DB Error: {e}")
        return {"status": "error", "data": {"total_plays":0, "library": {}}}

@router.get("/api/stats/recent")
def api_recent_activity(user_id: Optional[str] = None):
    try:
        where, params = get_base_filter(user_id)
        # 获取最近 50 条记录
        results = query_db(f"SELECT DateCreated, UserId, ItemId, ItemName, ItemType FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 50", params)
        
        if not results: 
            return {"status": "success", "data": []}
            
        user_map = get_user_map_local()
        data = []
        for row in results:
            item = dict(row)
            # 填充用户名，没有则显示 User
            item['UserName'] = user_map.get(item['UserId'], "User")
            item['DisplayName'] = item['ItemName']
            data.append(item)
            
        return {"status": "success", "data": data}
    except Exception as e: 
        print(f"⚠️ Recent Activity Error: {e}")
        return {"status": "error", "data": []}

@router.get("/api/live")
def api_live_sessions():
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    if not key: return {"status": "error"}
    try:
        res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=3)
        if res.status_code == 200: 
            # 只返回正在播放的会话
            return {"status": "success", "data": [s for s in res.json() if s.get("NowPlayingItem")]}
    except: pass
    return {"status": "success", "data": []}

@router.get("/api/stats/top_movies")
def api_top_movies(user_id: Optional[str] = None, category: str = 'all', sort_by: str = 'count'):
    try:
        where, params = get_base_filter(user_id)
        if category == 'Movie': where += " AND ItemType = 'Movie'"
        elif category == 'Episode': where += " AND ItemType = 'Episode'"
        
        # 限制查询量，优化性能
        sql = f"SELECT ItemName, ItemId, ItemType, PlayDuration FROM PlaybackActivity {where} LIMIT 5000"
        rows = query_db(sql, params)
        
        aggregated = {}
        for row in rows:
            # 清洗标题，合并 'Series - Episode'
            clean = row['ItemName'].split(' - ')[0]
            if clean not in aggregated: 
                aggregated[clean] = {'ItemName': clean, 'ItemId': row['ItemId'], 'PlayCount': 0, 'TotalTime': 0}
            
            aggregated[clean]['PlayCount'] += 1
            aggregated[clean]['TotalTime'] += (row['PlayDuration'] or 0)
            aggregated[clean]['ItemId'] = row['ItemId']
            
        res = list(aggregated.values())
        res.sort(key=lambda x: x['TotalTime'] if sort_by == 'time' else x['PlayCount'], reverse=True)
        
        return {"status": "success", "data": res[:50]}
    except: return {"status": "error", "data": []}

@router.get("/api/stats/user_details")
def api_user_details(user_id: Optional[str] = None):
    try:
        where, params = get_base_filter(user_id)
        
        # 1. 小时分布
        h_res = query_db(f"SELECT strftime('%H', DateCreated) as Hour, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Hour", params)
        h_data = {str(i).zfill(2): 0 for i in range(24)}
        if h_res:
            for r in h_res: h_data[r['Hour']] = r['Plays']
            
        # 2. 设备分布
        d_res = query_db(f"SELECT COALESCE(DeviceName, ClientName, 'Unknown') as Device, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Device ORDER BY Plays DESC LIMIT 10", params)
        
        # 3. 详细日志
        l_res = query_db(f"SELECT DateCreated, ItemName, PlayDuration, COALESCE(DeviceName, ClientName) as Device, UserId FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 100", params)
        
        u_map = get_user_map_local()
        logs = []
        if l_res:
            for r in l_res: 
                l = dict(r)
                l['UserName'] = u_map.get(l['UserId'], "User")
                logs.append(l)
                
        return {"status": "success", "data": {"hourly": h_data, "devices": [dict(r) for r in d_res] if d_res else [], "logs": logs}}
    except Exception as e: 
        print(f"Details Error: {e}")
        return {"status": "error", "data": {"hourly": {}, "devices": [], "logs": []}}

@router.get("/api/stats/chart")
@router.get("/api/stats/trend")
def api_chart_stats(user_id: Optional[str] = None, dimension: str = 'day'):
    try:
        where, params = get_base_filter(user_id)
        
        date_format = "%Y-%m-%d"
        limit = "30 days"
        
        if dimension == 'week':
            # 按周聚合 (SQLite strftime %W)
            sql = f"SELECT strftime('%Y-%W', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} AND DateCreated > date('now', '-120 days') GROUP BY Label ORDER BY Label"
        elif dimension == 'month':
            sql = f"SELECT strftime('%Y-%m', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} AND DateCreated > date('now', '-365 days') GROUP BY Label ORDER BY Label"
        else:
            # 默认按天
            sql = f"SELECT date(DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} AND DateCreated > date('now', '-30 days') GROUP BY Label ORDER BY Label"

        results = query_db(sql, params)
        data = {}
        if results:
            for r in results: data[r['Label']] = int(r['Duration'])
            
        return {"status": "success", "data": data}
    except Exception as e: 
        print(f"Chart Error: {e}")
        return {"status": "error", "data": {}}

@router.get("/api/stats/poster_data")
def api_poster_data(user_id: Optional[str] = None, period: str = 'all'):
    # ... (保持原样，或者如果您需要我也一起贴出来)
    # 为节省篇幅，这里复用您已有的逻辑，只需确保 query_db 可用
    try:
        where_base, params = get_base_filter(user_id)
        date_filter = ""
        if period == 'week': date_filter = " AND DateCreated > date('now', '-7 days')"
        elif period == 'month': date_filter = " AND DateCreated > date('now', '-30 days')"
        server_res = query_db(f"SELECT COUNT(*) as Plays FROM PlaybackActivity {get_base_filter('all')[0]} {date_filter}", get_base_filter('all')[1])
        server_plays = server_res[0]['Plays'] if server_res else 0
        raw_sql = f"SELECT ItemName, ItemId, ItemType, PlayDuration FROM PlaybackActivity {where_base + date_filter}"
        rows = query_db(raw_sql, params)
        total_plays = 0; total_duration = 0; aggregated = {} 
        if rows:
            for row in rows:
                total_plays += 1; dur = row['PlayDuration'] or 0; total_duration += dur; clean = row['ItemName'].split(' - ')[0]
                if clean not in aggregated: aggregated[clean] = {'ItemName': clean, 'ItemId': row['ItemId'], 'Count': 0, 'Duration': 0}
                aggregated[clean]['Count'] += 1; aggregated[clean]['Duration'] += dur; aggregated[clean]['ItemId'] = row['ItemId'] 
        top_list = list(aggregated.values()); top_list.sort(key=lambda x: x['Count'], reverse=True)
        return {"status": "success", "data": {"plays": total_plays, "hours": round(total_duration / 3600), "server_plays": server_plays, "top_list": top_list[:10], "tags": ["观影达人"]}}
    except: return {"status": "error", "data": {"plays": 0, "hours": 0}}

@router.get("/api/stats/top_users_list")
def api_top_users_list():
    try:
        # 获取播放时长最多的前 10 名用户
        res = query_db("SELECT UserId, COUNT(*) as Plays, SUM(PlayDuration) as TotalTime FROM PlaybackActivity GROUP BY UserId ORDER BY TotalTime DESC LIMIT 10")
        if not res: return {"status": "success", "data": []}
        
        user_map = get_user_map_local()
        hidden = cfg.get("hidden_users") or []
        data = []
        
        for row in res:
            if row['UserId'] in hidden: continue
            u = dict(row)
            u['UserName'] = user_map.get(u['UserId'], f"User {str(u['UserId'])[:5]}")
            data.append(u)
            if len(data) >= 5: break # 只取前5展示
            
        return {"status": "success", "data": data}
    except Exception as e: 
        print(f"Top Users Error: {e}")
        return {"status": "success", "data": []}

@router.get("/api/stats/badges")
def api_badges(user_id: Optional[str] = None):
    # ... (逻辑不变，确保 query_db 可用即可) ...
    try:
        where, params = get_base_filter(user_id); badges = []
        night_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '02' AND '05'", params)
        if night_res and night_res[0]['c'] > 5: badges.append({"id": "night", "name": "修仙党", "icon": "fa-moon", "color": "text-purple-500", "bg": "bg-purple-100", "desc": "深夜是灵魂最自由的时刻"})
        weekend_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%w', DateCreated) IN ('0', '6')", params)
        if weekend_res and weekend_res[0]['c'] > 10: badges.append({"id": "weekend", "name": "周末狂欢", "icon": "fa-champagne-glasses", "color": "text-pink-500", "bg": "bg-pink-100", "desc": "工作日唯唯诺诺，周末重拳出击"})
        dur_res = query_db(f"SELECT SUM(PlayDuration) as d FROM PlaybackActivity {where}", params)
        if dur_res and dur_res[0]['d'] and dur_res[0]['d'] > 360000: badges.append({"id": "liver", "name": "Emby肝帝", "icon": "fa-fire", "color": "text-red-500", "bg": "bg-red-100", "desc": "阅片无数"})
        return {"status": "success", "data": badges}
    except: return {"status": "success", "data": []}

@router.get("/api/stats/monthly_stats")
def api_monthly_stats(user_id: Optional[str] = None):
    try:
        where_base, params = get_base_filter(user_id)
        where = where_base + " AND DateCreated > date('now', '-12 months')"
        sql = f"SELECT strftime('%Y-%m', DateCreated) as Month, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Month ORDER BY Month"
        results = query_db(sql, params); data = {}
        if results: 
            for r in results: data[r['Month']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}