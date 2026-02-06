from fastapi import APIRouter, Request
from app.schemas.models import UserUpdateModel, NewUserModel
from app.core.config import cfg
from app.core.database import query_db
import requests
import datetime
import json
import time

router = APIRouter()

# Emby 本地默认认证提供商的类名
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
        
        # 🔥 Step 1: 暴力净化 (针对 2ms 假成功问题)
        # 只要涉及改密或改状态，就强制清洗，确保环境纯净
        if data.password or data.is_disabled is not None:
            user_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if user_res.status_code == 200:
                user_dto = user_res.json()
                
                # 检查是否有点“云端”的味道
                has_cloud_taint = (user_dto.get("AuthenticationProviderId") != DEFAULT_AUTH_PROVIDER) or \
                                  user_dto.get("ConnectUserId") or \
                                  user_dto.get("ConnectUserName") # 🔥 关键新增：检查用户名
                
                # 为了保险，即使看起来没问题，也强制刷一遍（如果提供了密码）
                if has_cloud_taint or data.password:
                    print(f"🧹 [Step 1] Force Purging ALL Cloud Fields...")
                    
                    # 1. 强制指定本地认证
                    user_dto["AuthenticationProviderId"] = DEFAULT_AUTH_PROVIDER
                    
                    # 2. 🔥 关键新增：使用空字符串 "" 强制覆盖，防止 null 被忽略
                    user_dto["ConnectUserId"] = ""  
                    user_dto["ConnectUserName"] = "" 
                    user_dto["ConnectLinkType"] = "LinkedUser" # 有时候设为 LinkedUser + 空ID 才能触发重置
                    
                    # 3. 移除干扰项
                    if "Password" in user_dto: del user_dto["Password"]
                    if "Configuration" in user_dto:
                        if "Password" in user_dto["Configuration"]: del user_dto["Configuration"]["Password"]

                    # 4. 🔥 技巧：修改 SortName 强制触发数据库 Dirty Flag (确保写入)
                    # 赋予一个临时 SortName，或者重置为 Name
                    user_dto["SortName"] = user_dto["Name"]

                    clean_res = requests.post(f"{host}/emby/Users/{data.user_id}?api_key={key}", json=user_dto)
                    print(f"   -> Cleanse Status: {clean_res.status_code}")

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

        # 3. 🔥 Step 3: 归零重启法
        if data.password:
            print(f"🔑 [Step 3] Resetting Password...")
            
            # (A) 归零
            print(f"   -> (A) Zeroing out password...")
            payload_zero = { "Id": data.user_id, "NewPassword": "", "ResetPassword": True }
            requests.post(f"{host}/emby/Users/{data.user_id}/Password?api_key={key}", json=payload_zero)
            
            # 这里的 Sleep 很重要，给数据库一点喘息时间
            time.sleep(0.2)
            
            # (B) 填入
            print(f"   -> (B) Setting new password...")
            payload_set = { 
                "Id": data.user_id, 
                "CurrentPassword": "", 
                "NewPassword": data.password, 
                "ResetPassword": False 
            }
            r_final = requests.post(f"{host}/emby/Users/{data.user_id}/Password?api_key={key}", json=payload_set)
            
            print(f"   -> Emby Final Response: {r_final.status_code}")
            if r_final.status_code not in [200, 204]:
                return {"status": "error", "message": f"改密失败: {r_final.text}"}

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
        
        # 2. 强制本地化 (使用同样的强力清洗)
        user_res = requests.get(f"{host}/emby/Users/{new_id}?api_key={key}")
        if user_res.status_code == 200:
            user_dto = user_res.json()
            user_dto["AuthenticationProviderId"] = DEFAULT_AUTH_PROVIDER
            user_dto["ConnectUserId"] = ""
            user_dto["ConnectUserName"] = ""
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