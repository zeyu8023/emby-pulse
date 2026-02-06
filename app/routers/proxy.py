from fastapi import APIRouter, Response
from app.core.config import cfg, FALLBACK_IMAGE_URL
import requests

router = APIRouter()

@router.get("/api/proxy/image/{item_id}/{img_type}")
def proxy_image(item_id: str, img_type: str):
    """
    代理 Emby 的图片资源，解决内网/混合内容问题
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    if not key or not host:
        return Response(status_code=404)

    try:
        # 构造 Emby 图片 URL
        # MaxHeight/MaxWidth 限制图片大小，提高加载速度
        url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=600&maxWidth=400&quality=90&api_key={key}"
        
        # 发起请求
        resp = requests.get(url, timeout=5, stream=True)
        
        if resp.status_code == 200:
            # 透传图片内容和 Content-Type
            return Response(
                content=resp.content, 
                media_type=resp.headers.get("Content-Type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"} # 缓存1天
            )
    except Exception as e:
        print(f"Proxy Image Error: {e}")
        pass
        
    # 失败则重定向到默认图
    return Response(status_code=404)

@router.get("/api/proxy/user_image/{user_id}")
def proxy_user_image(user_id: str, tag: str = None):
    """
    代理用户头像
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    if not key: 
        return Response(status_code=404)
        
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