from fastapi import APIRouter, Request
from app.core.config import cfg
import requests

router = APIRouter()

def get_emby_auth():
    return cfg.get("emby_host"), cfg.get("emby_api_key")

# 🔥 任务名称汉化字典 (仅作为标题美化，描述使用 Emby 原生的)
TRANS_MAP = {
    # 核心/系统
    "Scan Media Library": "扫描媒体库",
    "Refresh People": "刷新人物信息",
    "Rotate Log File": "日志轮转与归档",
    "Check for application updates": "检查主程序更新",
    "Check for plugin updates": "检查插件更新",
    "Cache file cleanup": "清理系统缓存",
    "Clean Transcode Directory": "清理转码临时文件",
    "Hardware Detection": "硬件转码能力检测",
    "Emby Server Backup": "服务器配置备份",
    
    # 媒体处理
    "Convert media": "媒体格式转换",
    "Create Playlists": "生成智能播放列表",
    "Extract Chapter Images": "提取章节预览图",
    "Chapter image extraction": "提取章节预览图",
    "Thumbnail image extraction": "提取视频缩略图",
    "Download subtitles": "自动下载字幕",
    "Organize new media files": "自动整理新文件",
    
    # 常见插件
    "Build Douban Cache": "构建豆瓣缓存",
    "Download OCR Data": "下载 OCR 数据",
    "Detect Episode Intros": "检测跳过片头",
    "Extract Intro Fingerprint": "提取片头指纹",
    "Extract MediaInfo": "提取媒体编码信息",
    "Extract Video Thumbnail": "提取视频缩略图",
    "Delete Persons": "清理无效人物",
    "Trakt Sync": "Trakt 同步",
    "Export Library to Trakt": "同步库到 Trakt",
    "Import playstates from Trakt.tv": "从 Trakt 导入播放状态"
}

# 🔥 核心类别排序与汉化 (不在这个列表里的，会自动显示原名)
CAT_MAP = {
    "Library": {"name": "📚 媒体库", "order": 1},
    "System": {"name": "⚡ 系统核心", "order": 2},
    "Maintenance": {"name": "🧹 维护保养", "order": 3},
    "Application": {"name": "📱 应用程序", "order": 4},
    "Metadata": {"name": "📝 元数据", "order": 5},
    "Downloads": {"name": "📥 下载管理", "order": 6},
    "Sync": {"name": "🔄 同步与备份", "order": 7},
    "Live TV": {"name": "📺 电视直播", "order": 8},
    "Transcoding": {"name": "🎞️ 转码", "order": 9}
}

@router.get("/api/tasks")
def get_scheduled_tasks(request: Request):
    """获取所有计划任务列表"""
    if not request.session.get("user"): return {"status": "error", "message": "Unauthorized"}
    
    host, key = get_emby_auth()
    if not host or not key: return {"status": "error", "message": "Emby 未配置"}

    try:
        url = f"{host}/emby/ScheduledTasks?api_key={key}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            raw_tasks = res.json()
            grouped = {}
            
            for t in raw_tasks:
                # 1. 汉化名称 (保留原名)
                origin_name = t.get('Name', '')
                display_name = TRANS_MAP.get(origin_name, origin_name)
                
                # 2. 处理描述
                desc = t.get('Description', '')
                
                # 3. 识别类别 (核心逻辑修改点)
                cat_raw = t.get('Category', 'Other')
                
                if cat_raw in CAT_MAP:
                    # 命中核心预设分类
                    cat_display = CAT_MAP[cat_raw]["name"]
                    sort_order = CAT_MAP[cat_raw]["order"]
                else:
                    # 🔥 没命中的（插件），直接用原名！
                    # 例如: Category="Trakt" -> 显示 "🧩 Trakt"
                    cat_display = f"🧩 {cat_raw}"
                    sort_order = 99 # 排在核心分类后面
                
                # 4. 构建数据对象
                task_obj = {
                    "Id": t.get("Id"),
                    "Name": display_name,
                    "OriginalName": origin_name,
                    "Description": desc,
                    "State": t.get("State"),
                    "CurrentProgressPercentage": t.get("CurrentProgressPercentage"),
                    "LastExecutionResult": t.get("LastExecutionResult"),
                    "Triggers": t.get("Triggers")
                }

                # 5. 归类 (使用分类名称作为 Key，防止不同插件合并)
                if cat_display not in grouped:
                    grouped[cat_display] = {
                        "title": cat_display, 
                        "order": sort_order, # 记录排序权重
                        "tasks": []
                    }
                grouped[cat_display]["tasks"].append(task_obj)
            
            # 6. 转列表并排序
            final_list = list(grouped.values())
            
            # 排序逻辑：
            # 第一优先级: order (核心分类 1-9 先排，插件 99 后排)
            # 第二优先级: title (插件之间按字母顺序排)
            final_list.sort(key=lambda x: (x['order'], x['title']))
            
            # 组内任务排序 (按名称)
            for group in final_list:
                group["tasks"].sort(key=lambda x: x['Name'])

            return {"status": "success", "data": final_list}
            
        return {"status": "error", "message": f"Emby Error: {res.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/api/tasks/{task_id}/start")
def start_task(task_id: str, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    host, key = get_emby_auth()
    try:
        url = f"{host}/emby/ScheduledTasks/Running/{task_id}?api_key={key}"
        requests.post(url, timeout=5)
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/tasks/{task_id}/stop")
def stop_task(task_id: str, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    host, key = get_emby_auth()
    try:
        url = f"{host}/emby/ScheduledTasks/Running/{task_id}/Delete?api_key={key}"
        requests.post(url, timeout=5)
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}