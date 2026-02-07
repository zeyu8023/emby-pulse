from fastapi import APIRouter, Response
from app.core.config import cfg
import requests
import logging

# 初始化日志
logger = logging.getLogger("uvicorn")
router = APIRouter()

# 🔥 注意：调试模式下禁用了 @lru_cache，以便每次刷新都能看到日志
# 生产环境可以把 @lru_cache(maxsize=4096) 加回来
def get_real_image_id_debug(item_id: str):
    """
    智能 ID 转换（调试版）
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    # 基础配置检查
    if not key or not host: 
        print(f"❌ [Debug] Missing Config: Host or Key is empty.")
        return item_id

    try:
        # 🔥 强制要求 Emby 返回 SeriesId 和 ParentId
        url = f"{host}/emby/Items/{item_id}?api_key={key}&Fields=SeriesId,ParentId,PrimaryImageAspectRatio"
        
        # 发起查询
        res = requests.get(url, timeout=5)
        
        if res.status_code == 200:
            data = res.json()
            
            # 提取关键字段
            type_raw = data.get("Type", "Unknown")
            series_id = data.get("SeriesId")
            parent_id = data.get("ParentId")
            name = data.get("Name", "Unknown")
            series_name = data.get("SeriesName", "Unknown")

            # 打印详细判断过程
            # print(f"🔍 [Check] ID={item_id} | Type={type_raw} | Name={name} | SeriesId={series_id}")

            # 逻辑 1: 如果有 SeriesId (通常是 Episode 或 Season)，直接用 SeriesId
            if series_id:
                print(f"✅ [Swap] ID {item_id} ({name}) -> SeriesId {series_id} ({series_name})")
                return series_id
            
            # 逻辑 2: 如果是单集但没有 SeriesId (可能是 API 数据不全)，尝试用 ParentId (可能是季 ID)
            if type_raw == "Episode" and parent_id:
                print(f"🔄 [Fallback] ID {item_id} has no SeriesId, using ParentId {parent_id}")
                return parent_id
                
            # 逻辑 3: 如果本身就是 Series 或 Movie，保持原样
            if type_raw in ["Series", "Movie"]:
                # print(f"⏹️ [Keep] ID {item_id} is already {type_raw}")
                return item_id

            # 其他情况
            # print(f"⚠️ [Skip] No parent info for {item_id} ({type_raw}), keeping original.")
            return item_id
            
        elif res.status_code == 404:
            # 这是一个关键点：如果返回 404，说明数据库里的这个 ID 已经是死记录了
            print(f"❌ [404] Item {item_id} not found in Emby. Cannot find Series poster.")
            return item_id
        else:
            print(f"❌ [Error] API returned {res.status_code} for {item_id}")
            return item_id
            
    except Exception as e:
        print(f"❌ [Exception] Failed to resolve {item_id}: {str(e)}")
        return item_id

@router.get("/api/proxy/image/{item_id}/{img_type}")
def proxy_image(item_id: str, img_type: str):
    """
    图片代理路由
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    if not key or not host:
        return Response(status_code=404)

    try:
        target_id = item_id
        
        # 🟢 仅对 Primary (封面) 启用智能替换逻辑
        if img_type.lower() == 'primary':
            target_id = get_real_image_id_debug(item_id)

        # 构造目标 URL
        # 限制尺寸以提高加载速度
        url = f"{host}/emby/Items/{target_id}/Images/{img_type}?maxHeight=600&maxWidth=400&quality=90&api_key={key}"
        
        # 下载图片
        resp = requests.get(url, timeout=10, stream=True)
        
        # 🟢 成功情况
        if resp.status_code == 200:
            return Response(
                content=resp.content, 
                media_type=resp.headers.get("Content-Type", "image/jpeg"),
                # 🔥 强制禁用浏览器缓存 (调试期间)
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"} 
            )
        
        # 🟡 失败情况 (如果 SeriesId 的图下载失败，比如该剧集确实没封面)
        # 尝试回退到原始 ID 下载截图
        if resp.status_code == 404 and target_id != item_id:
            print(f"⚠️ [Retry] Target {target_id} image missing, falling back to original {item_id}")
            fallback_url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=600&maxWidth=400&quality=90&api_key={key}"
            fallback_resp = requests.get(fallback_url, timeout=10, stream=True)
            
            if fallback_resp.status_code == 200:
                 return Response(
                    content=fallback_resp.content, 
                    media_type=fallback_resp.headers.get("Content-Type", "image/jpeg"),
                    headers={"Cache-Control": "no-cache"}
                )

    except Exception as e:
        print(f"❌ [Proxy Error] {e}")
        pass
        
    # 彻底失败，返回 404
    return Response(status_code=404)

@router.get("/api/proxy/user_image/{user_id}")
def proxy_user_image(user_id: str, tag: str = None):
    """
    用户头像代理
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    if not key: return Response(status_code=404)
        
    try:
        url = f"{host}/emby/Users/{user_id}/Images/Primary?width=200&height=200&mode=Crop&quality=90&api_key={key}"
        if tag: 
            url += f"&tag={tag}"
            
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            return Response(
                content=resp.content, 
                media_type=resp.headers.get("Content-Type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"}
            )
    except: 
        pass
        
    return Response(status_code=404)