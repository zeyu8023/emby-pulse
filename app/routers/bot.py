from fastapi import APIRouter, Request
from app.schemas.models import BotSettingsModel
from app.core.config import cfg
from app.services.bot_service import bot
import requests
import threading

router = APIRouter()

@router.get("/api/bot/settings")
def api_get_bot_settings(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    return {"status": "success", "data": cfg.get_all()}

@router.post("/api/bot/settings")
def api_save_bot_settings(data: BotSettingsModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    cfg.set("tg_bot_token", data.tg_bot_token); cfg.set("tg_chat_id", data.tg_chat_id)
    cfg.set("enable_bot", data.enable_bot)
    cfg.set("enable_notify", data.enable_notify)
    cfg.set("enable_library_notify", data.enable_library_notify) # 🔥 新增
    
    bot.stop()
    if data.enable_bot: threading.Timer(1.0, bot.start).start()
    return {"status": "success", "message": "配置已保存"}

@router.post("/api/bot/test")
def api_test_bot(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    token = cfg.get("tg_bot_token"); chat_id = cfg.get("tg_chat_id"); proxy = cfg.get("proxy_url")
    if not token: return {"status": "error", "message": "请先保存配置"}
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        res = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "🎉 测试消息"}, proxies=proxies, timeout=10)
        return {"status": "success"} if res.status_code == 200 else {"status": "error", "message": f"API Error: {res.text}"}
    except Exception as e: return {"status": "error", "message": str(e)}