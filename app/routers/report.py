from fastapi import APIRouter, Request
from app.schemas.models import PushRequestModel, ScheduleRequestModel
from app.core.config import cfg
from app.services.bot_service import bot
import uuid

router = APIRouter()

@router.post("/api/report/push_now")
def api_push_now(data: PushRequestModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    bot.push_now(data.user_id, data.period, data.theme)
    return {"status": "success", "message": "已发送"}

@router.get("/api/report/schedule")
def api_get_schedule(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    return {"status": "success", "data": cfg.get("scheduled_tasks") or []}

@router.post("/api/report/schedule")
def api_add_schedule(data: ScheduleRequestModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    tasks = cfg.get("scheduled_tasks") or []
    for t in tasks:
        if t['user_id'] == data.user_id and t['period'] == data.period: return {"status": "error", "message": "任务已存在"}
    new_task = {"id": str(uuid.uuid4())[:8], "user_id": data.user_id, "period": data.period, "theme": data.theme}
    tasks.append(new_task); cfg.set("scheduled_tasks", tasks)
    return {"status": "success", "message": "任务已添加"}

@router.delete("/api/report/schedule/{task_id}")
def api_delete_schedule(task_id: str, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    tasks = cfg.get("scheduled_tasks") or []; new_tasks = [t for t in tasks if t['id'] != task_id]
    cfg.set("scheduled_tasks", new_tasks); return {"status": "success", "message": "任务已删除"}