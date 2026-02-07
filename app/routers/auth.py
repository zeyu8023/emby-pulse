from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from app.core.config import cfg
from app.schemas.models import LoginModel
import requests

router = APIRouter()

@router.post("/api/login")
async def api_login(data: LoginModel, request: Request):
    try:
        host = cfg.get("emby_host")
        if not host: 
            return JSONResponse(content={"status": "error", "message": "请先在 config.yaml 配置 EMBY_HOST"})
            
        # 构造 Emby 认证请求
        url = f"{host}/emby/Users/AuthenticateByName"
        payload = {
            "Username": data.username, 
            "Pw": data.password
        }
        # 伪装成 Web 客户端
        headers = {
            "X-Emby-Authorization": 'MediaBrowser Client="EmbyPulse", Device="Web", DeviceId="EmbyPulse", Version="1.0.0"'
        }
        
        # 发送请求给 Emby Server
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        
        if res.status_code == 200:
            user_info = res.json().get("User", {})
            
            # 关键：检查是否为 Emby 管理员
            if not user_info.get("Policy", {}).get("IsAdministrator", False):
                return JSONResponse(content={"status": "error", "message": "权限不足：仅限 Emby 管理员登录"})
            
            # 登录成功：写入 Session
            request.session["user"] = {
                "id": user_info.get("Id"),
                "name": user_info.get("Name"),
                "is_admin": True,
                "server_id": res.json().get("ServerId") # 存一下 ServerId 备用
            }
            return JSONResponse(content={"status": "success"})
        
        elif res.status_code == 401:
            return JSONResponse(content={"status": "error", "message": "账号或密码错误"})
        else:
            return JSONResponse(content={"status": "error", "message": f"Emby 连接失败: {res.status_code}"})
            
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": f"登录异常: {str(e)}"})

@router.get("/logout")
async def api_logout(request: Request):
    # 彻底清空 Session
    request.session.clear()
    # 跳转回登录页
    return RedirectResponse("/login", status_code=302)