from fastapi import APIRouter, Request, BackgroundTasks, HTTPException
from app.services.bot_service import bot
from app.core.config import cfg
import json
import logging

logger = logging.getLogger("uvicorn")
router = APIRouter()

@router.post("/api/v1/webhook")
async def emby_webhook(request: Request, background_tasks: BackgroundTasks):
    query_token = request.query_params.get("token")
    if query_token != cfg.get("webhook_token"):
        raise HTTPException(status_code=403, detail="Invalid Token")

    try:
        data = None
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
        elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            raw_data = form.get("data")
            if raw_data: data = json.loads(raw_data)

        if not data: return {"status": "error", "message": "Empty"}

        event = data.get("Event", "").lower().strip()
        if event: logger.info(f"🔔 Webhook: {event}")

        # 1. 入库通知 (传递原始数据兜底)
        if event in ["library.new", "item.added"]:
            item = data.get("Item", {})
            if item.get("Id") and item.get("Type") in ["Movie", "Episode", "Series"]:
                background_tasks.add_task(bot.push_new_media, item.get("Id"), item)

        # 2. 播放状态
        elif event == "playback.start":
            background_tasks.add_task(bot.push_playback_event, data, "start")
        elif event == "playback.stop":
            background_tasks.add_task(bot.push_playback_event, data, "stop")
            # 🔥 已移除 save_playback_activity，只发通知，不写库

        return {"status": "success"}
    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return {"status": "error", "message": str(e)}