from fastapi import APIRouter, Request, BackgroundTasks
from app.services.bot_service import bot
import json

router = APIRouter()

@router.post("/api/v1/webhook")
async def emby_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    接收 Emby Webhook
    """
    # 验证 Token (可选，如果 Emby Webhook URL 填了 ?token=xxx)
    # token = request.query_params.get("token")
    
    try:
        # 解析数据
        # 有些 Emby 版本 Content-Type 不标准，使用 try 获取
        try:
            data = await request.json()
        except:
            form = await request.form()
            data = json.loads(form.get("data", "{}"))

        event = data.get("Event", "")

        # 1. 新资源入库通知
        if event == "Library.New":
            item = data.get("Item", {})
            item_id = item.get("Id")
            item_type = item.get("Type")
            
            # 过滤掉不需要的类型，只发电影和单集
            if item_id and item_type in ["Movie", "Episode"]:
                print(f"📥 New Media Detected: {item.get('Name')} ({item_type})")
                # 放入后台任务执行，不阻塞 Emby 请求
                background_tasks.add_task(bot.push_new_media, item_id)

        # 注意：播放通知目前是在 bot_service._monitor_loop 里轮询实现的，
        # 如果你想改用 Webhook 实时推送播放状态，也可以在这里加逻辑。
        # 目前保持原样即可。

        return {"status": "success"}
    
    except Exception as e:
        print(f"Webhook Error: {e}")
        return {"status": "error", "message": str(e)}