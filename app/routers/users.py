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

def set_password_via_impersonation(host, admin_key, user_id, username, new_password):
    """
    🔥 终极方案：替身攻击
    先作为管理员把密码置空，然后模拟用户用空密码登录，最后自己修改密码。
    """
    print(f"🥷 Impersonation Attack: Setting password for {username}...")

    # 1. 管理员：强制清洗账号并置空密码 (确保一定是空密码状态)
    try:
        # 强制本地化 + 置空密码
        user_res = requests.get(f"{host}/emby/Users/{user_id}?api_key={admin_key}")
        if user_res.status_code == 200:
            user_dto = user_res.json()
            user_dto["AuthenticationProviderId"] = DEFAULT_AUTH_PROVIDER
            user_dto["ConnectUserId"] = None
            requests.post(f"{host}/emby/Users/{user_id}?api_key={admin_key}", json=user_dto)
        
        # 强制重置为空
        requests.post(f"{host}/emby/Users/{user_id}/Password?api_key={admin_key}", 
                      json={"Id": user_id, "NewPassword": "", "ResetPassword": True})
        time.sleep(0.2)
    except Exception as e:
        print(f"   -> Step 1 Error: {e}")

    # 2. 替身：模拟用户登录 (用空密码)
    # 注意：这里不需要 admin_key，而是像普通客户端一样登录
    headers = {
        "X-Emby-Client": "EmbyPulse Bot",
        "X-Emby-Device-Name": "Server",
        "X-Emby-Device-Id": "embypulse-script",
        "X-Emby-Version": "4.8.0.0",
        "Content-Type": "application/json"
    }
    
    auth_data = {
        "Username": username,
        "Pw": "" # 🔥 关键：利用空密码漏洞登录
    }
    
    print(f"   -> Step 2: Logging in as '{username}' with empty password...")
    auth_res = requests.post(f"{host}/emby/Users/AuthenticateByName", json=auth_data, headers=headers)
    
    if auth_res.status_code != 200:
        print(f"   ❌ Login Failed: {auth_res.text}")
        return False, f"无法模拟登录: {auth_res.text}"
    
    # 拿到用户的 Token
    user_token = auth_res.json().get("AccessToken")
    print(f"   -> Got User Token: {user_token[:5]}***")

    # 3. 本尊：修改密码
    # 使用用户的 Token，而不是 API Key
    user_headers = headers.copy()
    user_headers["X-Emby-Token"] = user_token
    
    pwd_data = {
        "Id": user_id,
        "CurrentPassword": "", # 旧密码为空
        "NewPassword": new_password
    }
    
    print(f"   -> Step 3: Self-updating password...")
    pwd_res = requests.post(f"{host}/emby/Users/{user_id}/Password", json=pwd_data, headers=user_headers)
    
    if pwd_res.status_code in [200, 204]:
        print("   ✅ Password Set Successfully!")
        return True, "Success"
    else:
        print(f"   ❌ Self-Update Failed: {pwd_res.text}")
        return False, pwd_res.text

@router.post("/api/manage/user/update")
def api_manage_user_update(data: UserUpdateModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    print(f"📝 Update User Request: {data.user_id}")
    
    try:
        # 获取用户名 (替身登录需要)
        user_name = "Unknown"
        u_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
        if u_res.status_code == 200:
            user_name = u_res.json()['Name']

        # 1. 更新数据库有效期
        if data.expire_date is not None:
            exist = query_db("SELECT 1 FROM users_meta WHERE user_id = ?", (data.user_id,), one=True)
            if exist: query_db("UPDATE users_meta SET expire_date = ? WHERE user_id = ?", (data.expire_date, data.user_id))
            else: query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (data.user_id, data.expire_date, datetime.datetime.now().isoformat()))
        
        # 2. 刷新策略 (必须先启用用户，否则无法模拟登录)
        if data.is_disabled is not None:
            print(f"🔧 Updating Policy...")
            p_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if p_res.status_code == 200:
                policy = p_res.json().get('Policy', {})
                policy['IsDisabled'] = data.is_disabled
                if not data.is_disabled:
                    policy['LoginAttemptsBeforeLockout'] = -1 
                requests.post(f"{host}/emby/Users/{data.user_id}/Policy?api_key={key}", json=policy)

        # 3. 设置密码 (替身攻击)
        if data.password:
            # 确保用户已启用，否则登不进去
            if data.is_disabled is None: # 如果没显式传，强制检查并启用
                requests.post(f"{host}/emby/Users/{data.user_id}/Policy?api_key={key}", 
                              json={"IsDisabled": False, "LoginAttemptsBeforeLockout": -1})

            success, msg = set_password_via_impersonation(host, key, data.user_id, user_name, data.password)
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
        
        # 2. 立即启用 (否则无法登录)
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json={"IsDisabled": False, "LoginAttemptsBeforeLockout": -1})
        
        # 3. 设置初始密码 (替身攻击)
        if data.password:
            success, msg = set_password_via_impersonation(host, key, new_id, data.name, data.password)
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