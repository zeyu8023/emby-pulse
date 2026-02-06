from fastapi import APIRouter, Request
from app.schemas.models import UserUpdateModel, NewUserModel
from app.core.config import cfg
from app.core.database import query_db
import requests
import datetime

router = APIRouter()

@router.get("/api/manage/users")
def api_manage_users(request: Request):
    """
    获取用户列表及元数据
    """
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code != 200: return {"status": "error", "message": "Emby API Error"}
        emby_users = res.json()
        
        # 获取本地数据库中的扩展信息（过期时间、备注）
        meta_rows = query_db("SELECT * FROM users_meta")
        meta_map = {r['user_id']: dict(r) for r in meta_rows} if meta_rows else {}
        
        final_list = []
        for u in emby_users:
            uid = u['Id']
            meta = meta_map.get(uid, {})
            policy = u.get('Policy', {})
            final_list.append({
                "Id": uid, 
                "Name": u['Name'], 
                "LastLoginDate": u.get('LastLoginDate'),
                "IsDisabled": policy.get('IsDisabled', False), 
                "IsAdmin": policy.get('IsAdministrator', False),
                "ExpireDate": meta.get('expire_date'), 
                "Note": meta.get('note'), 
                "PrimaryImageTag": u.get('PrimaryImageTag')
            })
        return {"status": "success", "data": final_list}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/update")
def api_manage_user_update(data: UserUpdateModel, request: Request):
    """
    更新用户：只处理 停用/启用 和 过期时间
    已移除密码修改功能
    """
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    print(f"📝 Update User Request: {data.user_id}")
    
    try:
        # 1. 更新数据库有效期 (本地业务)
        if data.expire_date is not None:
            exist = query_db("SELECT 1 FROM users_meta WHERE user_id = ?", (data.user_id,), one=True)
            if exist: query_db("UPDATE users_meta SET expire_date = ? WHERE user_id = ?", (data.expire_date, data.user_id))
            else: query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (data.user_id, data.expire_date, datetime.datetime.now().isoformat()))
        
        # 2. 刷新策略 (仅处理 停用/启用)
        if data.is_disabled is not None:
            print(f"🔧 Updating Policy (IsDisabled={data.is_disabled})...")
            p_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if p_res.status_code == 200:
                policy = p_res.json().get('Policy', {})
                policy['IsDisabled'] = data.is_disabled
                # 如果是启用，重置错误次数，防止因为之前的尝试被锁
                if not data.is_disabled:
                    policy['LoginAttemptsBeforeLockout'] = -1 
                
                r = requests.post(f"{host}/emby/Users/{data.user_id}/Policy?api_key={key}", json=policy)
                if r.status_code != 204:
                    print(f"⚠️ Policy Update Warning: {r.status_code}")

        return {"status": "success", "message": "设置已更新 (密码修改功能已禁用)"}
    except Exception as e: 
        print(f"❌ Error: {e}")
        return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/new")
def api_manage_user_new(data: NewUserModel, request: Request):
    """
    新建用户：创建用户 + 初始化策略 + 设置过期时间
    不设置密码，返回提示信息
    """
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    print(f"📝 New User: {data.name}")
    try:
        # 1. 创建用户
        res = requests.post(f"{host}/emby/Users/New?api_key={key}", json={"Name": data.name})
        if res.status_code != 200: return {"status": "error", "message": f"创建失败: {res.text}"}
        new_id = res.json()['Id']
        
        # 2. 立即初始化策略 (防止默认被禁用)
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json={"IsDisabled": False, "LoginAttemptsBeforeLockout": -1})
        
        # 3. 记录有效期
        if data.expire_date:
            query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (new_id, data.expire_date, datetime.datetime.now().isoformat()))
            
        # 🔥 修改提示语：明确告知密码为空
        msg = "用户创建成功！当前账号密码为空，如需修改密码，请通知用户前往 Emby 客户端自行设置。"
        return {"status": "success", "message": msg}

    except Exception as e: return {"status": "error", "message": str(e)}

@router.delete("/api/manage/user/{user_id}")
def api_manage_user_delete(user_id: str, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        res = requests.delete(f"{host}/emby/Users/{user_id}?api_key={key}")
        if res.status_code in [200, 204]:
            query_db("DELETE FROM users_meta WHERE user_id = ?", (user_id,))
            return {"status": "success", "message": "用户已删除"}
        return {"status": "error", "message": "删除失败"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.get("/api/users")
def api_get_users():
    """
    简易用户列表 (用于下拉框等)
    """
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if not key: return {"status": "error"}
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code == 200:
            users = res.json(); hidden = cfg.get("hidden_users") or []; data = []
            for u in users: data.append({"UserId": u['Id'], "UserName": u['Name'], "IsHidden": u['Id'] in hidden})
            data.sort(key=lambda x: x['UserName'])
            return {"status": "success", "data": data}
        return {"status": "success", "data": []}
    except Exception as e: return {"status": "error", "message": str(e)}