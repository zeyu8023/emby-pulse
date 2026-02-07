from fastapi import APIRouter, Response
from app.core.config import cfg
import requests
import logging
from functools import lru_cache

# 设置日志
logger = logging.getLogger("uvicorn")

router = APIRouter()

# 🔥 核心魔法：智能 ID 转换缓存
@lru_cache(maxsize=4096)
def get_real_image_id(item_id: str):
    """
    智能判断：如果是单集 (Episode)，尝试向上寻找剧集 ID (SeriesId)
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    if not key or not host: return item_id

    try:
        url = f"{host}/emby/Items/{item_id}?api_key={key}"
        res = requests.get(url, timeout=5) 
        
        if res.status_code == 200:
            data = res.json()
            type_raw = data.get("Type", "")
            series_id = data.get("SeriesId")
            
            # 只有明确是单集/季且有 SeriesId 时才替换
            if type_raw in ["Episode", "Season"] and series_id:
                return series_id
            
            return item_id
            
        elif res.status_code == 404:
            # 🔥 优化：404 说明 Emby 里已经没有这个物品了（可能是已删除的历史记录）
            # 这种情况下，直接返回原 ID，不再打印红色报错，让图片接口自己去尝试加载
            return item_id
            
        else:
            # 其他错误才打印
            print(f"⚠️ [Proxy] API Error: {res.status_code} for {item_id}")
            
    except Exception as e:
        print(f"❌ [Proxy] Smart Resolve Failed for {item_id}: {str(e)}")
        pass
    
    return item_id

@router.get("/api/proxy/image/{item_id}/{img_type}")
def proxy_image(item_id: str, img_type: str):
    """
    代理 Emby 图片资源 (智能版 + 兜底优化)
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    if not key or not host: return Response(status_code=404)

    try:
        # 1. 尝试获取智能 ID (可能是 SeriesId，也可能是原 ID)
        target_id = item_id
        if img_type.lower() == 'primary':
            target_id = get_real_image_id(item_id)

        # 2. 构造 URL
        url = f"{host}/emby/Items/{target_id}/Images/{img_type}?maxHeight=600&maxWidth=400&quality=90&api_key={key}"
        
        # 3. 请求图片
        resp = requests.get(url, timeout=10, stream=True)
        
        if resp.status_code == 200:
            return Response(
                content=resp.content, 
                media_type=resp.headers.get("Content-Type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"} # 恢复缓存
            )
        
        # 🔥 4. 兜底逻辑：如果智能替换后的 ID 拿不到图（比如 SeriesId 也404了），
        # 且 target_id 不等于 item_id，那我们尝试回退用原 item_id 再试一次！
        if resp.status_code == 404 and target_id != item_id:
            # print(f"⚠️ [Proxy] Retry with original ID for {item_id}")
            fallback_url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=600&maxWidth=400&quality=90&api_key={key}"
            fallback_resp = requests.get(fallback_url, timeout=10, stream=True)
            if fallback_resp.status_code == 200:
                 return Response(
                    content=fallback_resp.content, 
                    media_type=fallback_resp.headers.get("Content-Type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=86400"}
                )

    except Exception as e:
        print(f"❌ [Proxy] Image Error: {e}")
        pass
        
    # 真的找不到图，返回 404
    return Response(status_code=404)

@router.get("/api/proxy/user_image/{user_id}")
def proxy_user_image(user_id: str, tag: str = None):
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if not key: return Response(status_code=404)
    try:
        url = f"{host}/emby/Users/{user_id}/Images/Primary?width=200&height=200&mode=Crop&quality=90&api_key={key}"
        if tag: url += f"&tag={tag}"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"), headers={"Cache-Control": "public, max-age=86400"})
    except: pass
    return Response(status_code=404)