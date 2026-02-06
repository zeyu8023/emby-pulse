from pydantic import BaseModel
from typing import Optional, List

class LoginModel(BaseModel):
    username: str
    password: str

class SettingsModel(BaseModel):
    emby_host: str
    emby_api_key: str
    tmdb_api_key: Optional[str] = ""
    proxy_url: Optional[str] = ""
    hidden_users: List[str] = []

class BotSettingsModel(BaseModel):
    tg_bot_token: str
    tg_chat_id: str
    enable_bot: bool
    enable_notify: bool
    enable_library_notify: Optional[bool] = False # 🔥 新增

class PushRequestModel(BaseModel):
    user_id: str
    period: str
    theme: str

class ScheduleRequestModel(BaseModel):
    user_id: str
    period: str
    theme: str

class UserUpdateModel(BaseModel):
    user_id: str
    password: Optional[str] = None
    is_disabled: Optional[bool] = None
    expire_date: Optional[str] = None 

class NewUserModel(BaseModel):
    name: str
    # 🔥 关键修复：设为 Optional，解决前端不传密码导致的 422 错误
    password: Optional[str] = None 
    expire_date: Optional[str] = None