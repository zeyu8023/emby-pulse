from fastapi import APIRouter, Request, BackgroundTasks, HTTPException
from app.services.bot_service import bot
from app.core.config import cfg
import json
import logging

# 初始化日志
logger = logging.getLogger("uvicorn")
router = APIRouter()

@router.post("/api/v1/webhook")
async def emby_webhook(request: Request, background_tasks: BackgroundTasks):
    # 1. 安全校验：验证 URL 中的 token 参数
    query_token = request.query_params.get("token")
    if query_token != cfg.get("webhook_token"):
        logger.warning(f"🚫 Webhook 鉴权失败: {query_token}")
        raise HTTPException(status_code=403, detail="Invalid Token")

    try:
        # 2. 增强型数据解析 (兼容 JSON, Form, Multipart)
        data = None
        content_type = request.headers.get("content-type", "")
        
        try:
            if "application/json" in content_type:
                data = await request.json()
            elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
                form = await request.form()
                # Emby 通常把 JSON 放在 'data' 字段里
                raw_data = form.get("data")
                if raw_data:
                    data = json.loads(raw_data)
        except Exception as parse_err:
            logger.error(f"❌ 数据解析失败: {parse_err}")
            return {"status": "error", "message": "Payload parse failed"}

        if not data:
            logger.warning("⚠️ 收到的 Webhook 数据为空")
            return {"status": "error", "message": "Empty payload"}

        # 3. 提取事件类型
        event_raw = data.get("Event", "")
        event = event_raw.lower().strip()
        
        if event:
            logger.info(f"🔔 Webhook 收到事件: {event_raw}")

        # 4. 事件分发处理
        
        # [场景A] 媒体入库 (支持 Movie, Episode, Series)
        if event in ["library.new", "item.added"]:
            item = data.get("Item", {})
            item_id = item.get("Id")
            item_type = item.get("Type")
            
            # 过滤不需要的类型，只处理视频类
            if item_id and item_type in ["Movie", "Episode", "Series"]:
                # 🔥 关键修改：把 item (原始数据) 也传过去，作为 404 时的兜底数据
                background_tasks.add_task(bot.push_new_media, item_id, item)

        # [场景B] 播放开始
        elif event == "playback.start":
            # 发送通知
            background_tasks.add_task(bot.push_playback_event, data, "start")

        # [场景C] 播放停止 (关键：既要发通知，又要记账！)
        elif event == "playback.stop":
            # 1. 发送停止通知
            background_tasks.add_task(bot.push_playback_event, data, "stop")
            # 2. 🔥 写入数据库 (修复日报无数据的问题)
            background_tasks.add_task(bot.save_playback_activity, data)

        return {"status": "success"}
    
    except Exception as e:
        logger.error(f"❌ Webhook 处理异常: {e}")
        return {"status": "error", "message": str(e)}