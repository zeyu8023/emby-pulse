from fastapi import APIRouter, Request
from app.schemas.models import UserUpdateModel, NewUserModel
from app.core.config import cfg
from app.core.database import query_db
import requests
import datetime
import json
import time

router = APIRouter()

# 默认认证提供商
DEFAULT_AUTH_PROVIDER = "Emby.Server.Implementations.Library.DefaultAuthenticationProvider"

@router.get("/api/manage/users")
def api_manage_users(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code != 200: return {"status": "error", "message": "Emby API Error"}
        emby_users = res.json()
        meta_rows = query_db("SELECT * FROM users_meta")
        meta_map = {r['user_id']: dict(r) for r in meta_rows} if meta_rows else {}
        final_list = []
        for u in emby_users:
            uid = u['Id']; meta = meta_map.get(uid, {}); policy = u.get('Policy', {})
            final_list.append({
                "Id": uid, "Name": u['Name'], "LastLoginDate": u.get('LastLoginDate'),
                "IsDisabled": policy.get('IsDisabled', False), "IsAdmin": policy.get('IsAdministrator', False),
                "ExpireDate": meta.get('expire_date'), "Note": meta.get('note'), "PrimaryImageTag": u.get('PrimaryImageTag')
            })
        return {"status": "success", "data": final_list}
    except Exception as e: return {"status": "error", "message": str(e)}

def force_set_password_logic(host, key, user_id, password):
    """
    封装后的改密逻辑：同时尝试两种路径，确保写入
    """
    print(f"🔑 Setting Password for {user_id}...")
    
    # 路径 1: 直接在 UserDto 中注入密码 (适用于 Emby 新版本)
    # 这是一个"更新用户属性"的操作，往往比 /Password 接口更有效
    try:
        user_res = requests.get(f"{host}/emby/Users/{user_id}?api_key={key}")
        if user_res.status_code == 200:
            user_dto = user_res.json()
            
            # 1. 注入密码到 DTO
            user_dto["Password"] = password
            
            # 2. 顺手修正认证方式 (防止还是云端状态)
            user_dto["AuthenticationProviderId"] = DEFAULT_AUTH_PROVIDER
            user_dto["ConnectUserId"] = None
            
            print(f"   -> Method 1: Injecting 'Password' into UserDto...")
            r1 = requests.post(f"{host}/emby/Users/{user_id}?api_key={key}", json=user_dto)
            print(f"   -> Status: {r1.status_code}")
    except Exception as e:
        print(f"   -> Method 1 Failed: {e}")

    # 路径 2: 使用 /Password 接口，但严禁使用 ResetPassword=True
    try:
        time.sleep(0.2)
        print(f"   -> Method 2: Calling /Password Endpoint (ResetPassword=False)...")
        
        # 这里的关键是 ResetPassword: False
        # 这告诉 Emby: "我是来设置值的，不是来清空状态的"
        payload = {
            "Id": user_id,
            "NewPassword": password,
            "ResetPassword": False  # 🔥 关键修改：绝对不能是 True
        }
        
        # Emby 有时需要 CurrentPassword 字段存在(哪怕是空)才能通过校验
        payload["CurrentPassword"] = "" 
        
        r2 = requests.post(f"{host}/emby/Users/{user_id}/Password?api_key={key}", json=payload)
        print(f"   -> Status: {r2.status_code} | Response: {r2.text}")
        
        if r2.status_code not in [200, 204]:
            return False, r2.text
            
    except Exception as e:
        print(f"   -> Method 2 Failed: {e}")
        return False, str(e)
        
    return True, "Success"

@router.post("/api/manage/user/update")
def api_manage_user_update(data: UserUpdateModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    print(f"📝 Update User Request: {data.user_id}")
    
    try:
        # 1. 更新数据库有效期
        if data.expire_date is not None:
            exist = query_db("SELECT 1 FROM users_meta WHERE user_id = ?", (data.user_id,), one=True)
            if exist: query_db("UPDATE users_meta SET expire_date = ? WHERE user_id = ?", (data.expire_date, data.user_id))
            else: query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (data.user_id, data.expire_date, datetime.datetime.now().isoformat()))
        
        # 2. 刷新策略 (解禁)
        if data.is_disabled is not None:
            print(f"🔧 Updating Policy...")
            p_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if p_res.status_code == 200:
                policy = p_res.json().get('Policy', {})
                policy['IsDisabled'] = data.is_disabled
                if not data.is_disabled:
                    policy['LoginAttemptsBeforeLockout'] = -1 
                requests.post(f"{host}/emby/Users/{data.user_id}/Policy?api_key={key}", json=policy)

        # 3. 设置密码 (使用新逻辑)
        if data.password:
            success, msg = force_set_password_logic(host, key, data.user_id, data.password)
            if not success:
                return {"status": "error", "message": f"改密失败: {msg}"}

        return {"status": "success", "message": "更新成功"}
    except Exception as e: 
        print(f"❌ Error: {e}")
        return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/new")
def api_manage_user_new(data: NewUserModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    print(f"📝 New User: {data.name}")
    try:
        # 1. 创建用户
        res = requests.post(f"{host}/emby/Users/New?api_key={key}", json={"Name": data.name})
        if res.status_code != 200: return {"status": "error", "message": f"创建失败: {res.text}"}
        new_id = res.json()['Id']
        
        # 2. 立即初始化策略
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json={"IsDisabled": False, "LoginAttemptsBeforeLockout": -1})
        
        # 3. 设置初始密码 (直接使用新逻辑)
        if data.password:
            success, msg = force_set_password_logic(host, key, new_id, data.password)
            if not success:
                print(f"⚠️ Initial password set failed: {msg}")

        # 4. 记录有效期
        if data.expire_date:
            query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (new_id, data.expire_date, datetime.datetime.now().isoformat()))
            
        return {"status": "success", "message": "用户创建成功"}
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