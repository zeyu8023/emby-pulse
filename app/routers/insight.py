from fastapi import APIRouter, Request
from app.core.config import cfg
import requests
import time
import logging

# 配置日志
logger = logging.getLogger("uvicorn")

router = APIRouter()

def get_emby_auth():
    """获取 Emby 配置信息"""
    return cfg.get("emby_host"), cfg.get("emby_api_key")

def fetch_with_retry(url, headers, retries=3):
    """
    带重试机制的请求函数
    """
    for i in range(retries):
        try:
            # 超时时间设为 60 秒
            response = requests.get(url, headers=headers, timeout=60)
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 500:
                logger.warning(f"Emby 服务端报错 500，可能是查询数据量过大 (尝试 {i+1}/{retries})")
            else:
                logger.warning(f"Emby API 返回错误: {response.status_code} (尝试 {i+1}/{retries})")
        except requests.exceptions.Timeout:
            logger.warning(f"连接 Emby 超时 (尝试 {i+1}/{retries})")
        except requests.exceptions.RequestException as e:
            logger.error(f"连接 Emby 网络错误: {e}")
        
        if i == retries - 1: break
        time.sleep(1)
    return None

@router.get("/api/insight/quality")
def scan_library_quality(request: Request):
    """
    质量盘点核心接口 - 分批查询版
    """
    # 1. 鉴权
    user = request.session.get("user")
    if not user:
        return {"status": "error", "message": "Unauthorized: 请先登录"}
    
    host, key = get_emby_auth()
    if not host or not key:
        return {"status": "error", "message": "Emby 未配置，请前往[系统设置]填写 API Key"}

    headers = {"X-Emby-Token": key, "Accept": "application/json"}

    # 2. 定义分批查询函数
    def fetch_items_by_type(item_type):
        """
        拆分查询：单独查 Movie 或 Episode，减轻 Emby 压力
        减少 Fields 字段，只查必要的 MediaSources 和 Path
        """
        # 注意：不再请求 ProviderIds 和 MediaStreams(通常包含在MediaSources里)，减少数据量
        query = f"Recursive=true&IncludeItemTypes={item_type}&Fields=MediaSources,Path"
        url = f"{host}/emby/Items?{query}"
        logger.info(f"正在扫描 {item_type}: {url}")
        
        data = fetch_with_retry(url, headers)
        if data and "Items" in data:
            return data["Items"]
        return []

    try:
        # 3. 分别获取电影和剧集 (避免一次性请求导致 500 错误)
        movies = fetch_items_by_type("Movie")
        episodes = fetch_items_by_type("Episode")
        
        # 合并结果
        items = movies + episodes
        
        if not items:
            return {"status": "error", "message": "未获取到任何媒体数据，请检查 Emby 是否有媒体库或 API 是否正常"}

        # 4. 初始化统计
        stats = {
            "total_count": len(items),
            "resolution": {"4k": 0, "1080p": 0, "720p": 0, "sd": 0},
            "video_codec": {"hevc": 0, "h264": 0, "av1": 0, "other": 0},
            "hdr_type": {"sdr": 0, "hdr10": 0, "dolby_vision": 0},
            "bad_quality_list": []
        }

        # 5. 遍历统计
        for item in items:
            # 兼容性判断
            media_sources = item.get("MediaSources")
            if not media_sources or not isinstance(media_sources, list):
                continue
            
            source = media_sources[0]
            media_streams = source.get("MediaStreams")
            if not media_streams:
                continue
            
            # 找到视频流
            video_stream = next((s for s in media_streams if s.get("Type") == "Video"), None)
            if not video_stream:
                continue

            # --- 分辨率 ---
            width = video_stream.get("Width", 0)
            if width >= 3800: stats["resolution"]["4k"] += 1
            elif width >= 1900: stats["resolution"]["1080p"] += 1
            elif width >= 1200: stats["resolution"]["720p"] += 1
            else: 
                stats["resolution"]["sd"] += 1
                if len(stats["bad_quality_list"]) < 100:
                    stats["bad_quality_list"].append({
                        "Name": item.get("Name"),
                        "SeriesName": item.get("SeriesName", ""),
                        "Year": item.get("ProductionYear"),
                        "Resolution": f"{width}x{video_stream.get('Height')}",
                        "Path": item.get("Path", "未知路径")
                    })

            # --- 编码 ---
            codec = video_stream.get("Codec", "").lower()
            if "hevc" in codec or "h265" in codec: stats["video_codec"]["hevc"] += 1
            elif "h264" in codec or "avc" in codec: stats["video_codec"]["h264"] += 1
            elif "av1" in codec: stats["video_codec"]["av1"] += 1
            else: stats["video_codec"]["other"] += 1

            # --- HDR ---
            video_range = video_stream.get("VideoRange", "").lower()
            display_title = video_stream.get("DisplayTitle", "").lower()
            
            if "dolby" in display_title or "dv" in display_title or "dolby" in video_range:
                stats["hdr_type"]["dolby_vision"] += 1
            elif "hdr" in video_range or "hdr" in display_title or "pq" in video_range:
                stats["hdr_type"]["hdr10"] += 1
            else:
                stats["hdr_type"]["sdr"] += 1

        return {"status": "success", "data": stats}

    except Exception as e:
        logger.error(f"质量盘点处理错误: {str(e)}")
        return {"status": "error", "message": f"处理失败: {str(e)}"}