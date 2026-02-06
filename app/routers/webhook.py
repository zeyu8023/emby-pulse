from fastapi import APIRouter, Request, BackgroundTasks
from app.services.bot_service import bot
from app.core.config import cfg
import json

router = APIRouter()

@router.post("/api/v1/webhook")
async def emby_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    统一处理 Emby Webhook 事件
    """
    try:
        # 兼容性处理：Emby 有时发 Form 表单，有时发 JSON
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
        else:
            form = await request.form()
            data = json.loads(form.get("data", "{}"))

        event = data.get("Event", "")
        
        # 调试日志：看看收到了什么
        # print(f"🔔 Webhook Event: {event}")

        # 1. 新资源入库 (Library.New)
        if event == "Library.New":
            item = data.get("Item", {})
            item_id = item.get("Id")
            item_type = item.get("Type")
            
            # 只处理电影和剧集单集
            if item_id and item_type in ["Movie", "Episode"]:
                # 放入后台任务，避免卡住 Emby
                background_tasks.add_task(bot.push_new_media, item_id)

        # 2. 播放开始 (Playback.Start)
        elif event == "Playback.Start":
            # 放入后台任务
            background_tasks.add_task(bot.push_playback_start, data)

        return {"status": "success"}
    
    except Exception as e:
        print(f"❌ Webhook Error: {e}")
        return {"status": "error", "message": str(e)}