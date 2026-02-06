from fastapi import APIRouter, Request
from app.schemas.models import UserUpdateModel, NewUserModel
from app.core.config import cfg
from app.core.database import query_db
import requests
import datetime
import json
import time

router = APIRouter()

# Emby 本地默认认证提供商
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
        
        # 🔥 Step 1: 注入 PIN 码 (强制激活本地认证)
        if data.password or data.is_disabled is not None:
            user_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if user_res.status_code == 200:
                user_dto = user_res.json()
                original_name = user_dto["Name"]
                
                print(f"🧹 [Step 1] Injecting PIN Code to Force Local Auth...")
                
                # --- 动作: 强制设置本地 PIN 码 + 清除云端 ---
                user_dto["AuthenticationProviderId"] = DEFAULT_AUTH_PROVIDER
                user_dto["EasyPassword"] = "1234" # 设置一个临时 PIN，强迫 Emby 认领此账号为本地
                
                # 🔥 修正：必须用 None (JSON null)，不能用空字符串
                user_dto["ConnectUserId"] = None
                user_dto["ConnectUserName"] = None
                user_dto["ConnectLinkType"] = None
                
                # 改个名确保写库
                user_dto["Name"] = f"{original_name}_fix"
                
                if "Password" in user_dto: del user_dto["Password"]
                if "Configuration" in user_dto and "Password" in user_dto["Configuration"]: 
                    del user_dto["Configuration"]["Password"]
                
                r1 = requests.post(f"{host}/emby/Users/{data.user_id}?api_key={key}", json=user_dto)
                print(f"   -> Inject Status: {r1.status_code}")

        # 2. 刷新策略
        if data.is_disabled is not None:
            print(f"🔧 [Step 2] Updating Policy...")
            p_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if p_res.status_code == 200:
                policy = p_res.json().get('Policy', {})
                policy['IsDisabled'] = data.is_disabled
                if not data.is_disabled:
                    policy['LoginAttemptsBeforeLockout'] = -1 
                requests.post(f"{host}/emby/Users/{data.user_id}/Policy?api_key={key}", json=policy)

        # 3. 🔥 Step 3: 管理员强制改密 (此时账号有 PIN，必然是本地账号)
        if data.password:
            print(f"🔑 [Step 3] Force Admin Password Reset...")
            time.sleep(0.5) 
            
            # 先归零
            payload_zero = { "Id": data.user_id, "NewPassword": "", "ResetPassword": True }
            requests.post(f"{host}/emby/Users/{data.user_id}/Password?api_key={key}", json=payload_zero)
            
            time.sleep(0.2)
            
            # 再设置
            payload = { 
                "Id": data.user_id, 
                "NewPassword": data.password, 
                "ResetPassword": False 
            }
            r = requests.post(f"{host}/emby/Users/{data.user_id}/Password?api_key={key}", json=payload)
            print(f"   -> Emby Final Response: {r.status_code}")
            
            # 🔥 Step 4: 清理现场 (移除 PIN 码，恢复名字)
            print(f"🧹 [Step 4] Cleaning up PIN and Name...")
            restore_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if restore_res.status_code == 200:
                restore_dto = restore_res.json()
                if restore_dto["Name"].endswith("_fix"):
                    restore_dto["Name"] = restore_dto["Name"].replace("_fix", "") # 恢复原名
                
                restore_dto["EasyPassword"] = None # 🔥 删除 PIN 码
                restore_dto["AuthenticationProviderId"] = DEFAULT_AUTH_PROVIDER
                restore_dto["ConnectUserId"] = None
                
                requests.post(f"{host}/emby/Users/{data.user_id}?api_key={key}", json=restore_dto)

            if r.status_code not in [200, 204]:
                return {"status": "error", "message": f"改密失败: {r.text}"}

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
        # 1. 创建
        res = requests.post(f"{host}/emby/Users/New?api_key={key}", json={"Name": data.name})
        if res.status_code != 200: return {"status": "error", "message": f"创建失败: {res.text}"}
        new_id = res.json()['Id']
        
        # 2. 强制本地化
        user_res = requests.get(f"{host}/emby/Users/{new_id}?api_key={key}")
        if user_res.status_code == 200:
            user_dto = user_res.json()
            user_dto["AuthenticationProviderId"] = DEFAULT_AUTH_PROVIDER
            user_dto["ConnectUserId"] = None
            user_dto["ConnectUserName"] = None
            requests.post(f"{host}/emby/Users/{new_id}?api_key={key}", json=user_dto)

        # 3. 策略
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json={"IsDisabled": False, "LoginAttemptsBeforeLockout": -1})
        
        # 4. 设置初始密码
        if data.password:
            requests.post(f"{host}/emby/Users/{new_id}/Password?api_key={key}", json={"NewPassword": "", "ResetPassword": True})
            time.sleep(0.1)
            requests.post(f"{host}/emby/Users/{new_id}/Password?api_key={key}", json={"CurrentPassword": "", "NewPassword": data.password, "ResetPassword": False})

        # 5. 记录
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