from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from app.core.config import templates, cfg

router = APIRouter()

@router.get("/")
async def page_dashboard(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("index.html", {"request": request, "active_page": "dashboard", "user": request.session.get("user")})

@router.get("/content")
async def page_content(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("content.html", {"request": request, "active_page": "content", "user": request.session.get("user")})

@router.get("/details")
async def page_details(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("details.html", {"request": request, "active_page": "details", "user": request.session.get("user")})

@router.get("/report")
async def page_report(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("report.html", {"request": request, "active_page": "report", "user": request.session.get("user")})

@router.get("/bot")
async def page_bot(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    context = {"request": request, "active_page": "bot", "user": request.session.get("user")}
    context.update(cfg.get_all()) 
    return templates.TemplateResponse("bot.html", context)

@router.get("/users_manage")
async def page_users_manage(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("users.html", {"request": request, "active_page": "users_manage", "user": request.session.get("user")})

@router.get("/settings")
async def page_settings(request: Request):
    if not request.session.get("user"): return RedirectResponse("/login")
    return templates.TemplateResponse("settings.html", {"request": request, "active_page": "settings", "user": request.session.get("user")})