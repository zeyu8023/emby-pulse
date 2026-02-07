from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.core.config import cfg
import os

# 初始化模版引擎
templates = Jinja2Templates(directory="templates")

router = APIRouter()

# 依赖项：检查是否登录
def check_login(request: Request):
    token = request.cookies.get("access_token")
    if not token or token != cfg.get("web_password"):
        # 如果是 API 请求返回 401，如果是页面请求跳转登录
        if request.url.path.startswith("/api"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return False
    return True

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("index.html", {"request": request})

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@router.get("/content", response_class=HTMLResponse)
async def content_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("content.html", {"request": request})

@router.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("report.html", {"request": request})

@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("users.html", {"request": request})

# 🔥 修正点：这里必须用 @router.get，不能用 @app.get
@router.get("/insight", response_class=HTMLResponse)
async def insight_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("insight.html", {"request": request})