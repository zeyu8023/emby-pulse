from fastapi import APIRouter, Request, BackgroundTasks
from app.services.bot_service import bot
import json

router = APIRouter()

@router.post("/api/v1/webhook")
async def emby_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    统一处理 Emby Webhook 事件 (支持 播放开始/停止、新入库)
    """
    try:
        # 1. 解析数据 (兼容 JSON 和 Form)
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
        else:
            form = await request.form()
            raw_data = form.get("data", "{}")
            data = json.loads(raw_data)

        # 2. 获取事件类型 (转为小写以兼容不同 Emby 版本)
        event_raw = data.get("Event", "")
        event = event_raw.lower().strip()
        
        # 调试日志
        if event:
            print(f"🔔 Webhook Event: {event_raw}")

        # 3. 路由分发
        
        # [场景A] 新资源入库 (library.new)
        if event == "library.new":
            item = data.get("Item", {})
            item_id = item.get("Id")
            item_type = item.get("Type")
            
            # 只处理电影和剧集单集
            if item_id and item_type in ["Movie", "Episode"]:
                print(f"   -> 触发入库推送: {item.get('Name')}")
                background_tasks.add_task(bot.push_new_media, item_id)

        # [场景B] 播放开始 (playback.start)
        elif event == "playback.start":
            print(f"   -> 触发开始播放推送")
            background_tasks.add_task(bot.push_playback_event, data, "start")

        # [场景C] 播放停止 (playback.stop)
        elif event == "playback.stop":
            print(f"   -> 触发停止播放推送")
            background_tasks.add_task(bot.push_playback_event, data, "stop")

        return {"status": "success"}
    
    except Exception as e:
        print(f"❌ Webhook 处理错误: {e}")
        return {"status": "error", "message": str(e)}