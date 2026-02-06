from fastapi import APIRouter, Request
from app.schemas.models import UserUpdateModel, NewUserModel
from app.core.config import cfg
from app.core.database import query_db
import requests
import datetime
import json
import time
import uuid  # 引入 UUID 生成随机数

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
        
        # 🔥 Step 1: 制造“脏数据”强制清洗
        if data.password or data.is_disabled is not None:
            user_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if user_res.status_code == 200:
                user_dto = user_res.json()
                
                # 生成一个随机标记，确保数据与数据库中不同，强制触发写入
                random_tag = str(uuid.uuid4())[:8]
                print(f"🧹 [Step 1] Force Dirty Write (Tag: {random_tag})...")
                
                # 1. 强制本地认证
                user_dto["AuthenticationProviderId"] = DEFAULT_AUTH_PROVIDER
                
                # 2. 清除云端字段
                user_dto["ConnectUserId"] = ""  
                user_dto["ConnectUserName"] = "" 
                user_dto["ConnectLinkType"] = ""
                
                # 3. 🔥 核心：修改 SortName 为随机值，迫使 Emby 认为数据变了，必须写库
                # 如果不改这个，Emby 可能会因为其他字段没变而跳过写入（导致 3ms 耗时）
                user_dto["SortName"] = f"FIX_{random_tag}" 
                
                # 4. 移除干扰
                if "Password" in user_dto: del user_dto["Password"]

                # 提交更新
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

        # 3. 🔥 Step 3: 管理员强制改密
        # 此时账号已经是本地的了（因为 Step 1 强制写库了）
        if data.password:
            print(f"🔑 [Step 3] Force Admin Password Reset...")
            
            # 给数据库一点时间同步
            time.sleep(0.3)
            
            # 直接使用标准的管理员强制重置
            # Id: 用户ID
            # NewPassword: 新密码
            # ResetPassword: True (告诉 Emby 这是一个强制覆盖操作)
            payload = { 
                "Id": data.user_id, 
                "NewPassword": data.password, 
                "ResetPassword": True 
            }
            r = requests.post(f"{host}/emby/Users/{data.user_id}/Password?api_key={key}", json=payload)
            
            print(f"   -> Emby Final Response: {r.status_code}")
            if r.status_code not in [200, 204]:
                return {"status": "error", "message": f"改密失败: {r.text}"}
            
            # (可选) 恢复 SortName，为了美观
            # 虽然不恢复也不影响使用，用户平时看不到 SortName
            try:
                restore_dto = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}").json()
                if restore_dto.get("SortName", "").startswith("FIX_"):
                    restore_dto["SortName"] = restore_dto["Name"]
                    requests.post(f"{host}/emby/Users/{data.user_id}?api_key={key}", json=restore_dto)
            except: pass

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
            user_dto["ConnectUserId"] = ""
            user_dto["ConnectUserName"] = ""
            user_dto["ConnectLinkType"] = ""
            requests.post(f"{host}/emby/Users/{new_id}?api_key={key}", json=user_dto)

        # 3. 策略
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json={"IsDisabled": False, "LoginAttemptsBeforeLockout": -1})
        
        # 4. 设置初始密码
        if data.password:
            requests.post(f"{host}/emby/Users/{new_id}/Password?api_key={key}", json={"NewPassword": data.password, "ResetPassword": True})

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