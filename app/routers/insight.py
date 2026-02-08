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
    解决 Emby 4.10 可能出现的瞬时连接中断问题
    """
    for i in range(retries):
        try:
            # 🔥 重点修复：将超时时间设置为 60 秒
            response = requests.get(url, headers=headers, timeout=60)
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"Emby API 返回错误代码: {response.status_code} (尝试 {i+1}/{retries})")
        except requests.exceptions.Timeout:
            logger.warning(f"连接 Emby 超时 (尝试 {i+1}/{retries})")
            if i == retries - 1: raise # 最后一次尝试也超时，则抛出异常
        except requests.exceptions.RequestException as e:
            logger.error(f"连接 Emby 发生网络错误: {e}")
            if i == retries - 1: raise
        
        # 失败后等待 1 秒再重试
        time.sleep(1)
    return None

@router.get("/api/insight/scan")
def scan_library_quality(request: Request):
    """
    质量盘点核心接口
    """
    # 1. 鉴权检查
    user = request.session.get("user")
    if not user:
        return {"status": "error", "message": "Unauthorized: 请先登录"}
    
    # 2. 获取配置
    host, key = get_emby_auth()
    if not host or not key:
        return {"status": "error", "message": "Emby 未配置，请前往[系统设置]填写 API Key"}

    try:
        # 3. 构造请求头
        headers = {
            "X-Emby-Token": key,
            "Accept": "application/json"
        }
        
        # 4. 构造查询 URL (适配 Emby 4.10+)
        # Recursive=true: 递归查询所有子项
        # IncludeItemTypes: 只查电影和剧集
        # Fields: 显式要求返回媒体源信息(MediaSources)、路径(Path)和提供商ID(ProviderIds)
        query_params = "Recursive=true&IncludeItemTypes=Movie,Episode&Fields=MediaSources,ProviderIds,Path,MediaStreams"
        url = f"{host}/emby/Items?{query_params}"
        
        logger.info(f"开始扫描媒体库质量: {url}")
        
        # 5. 发起请求 (带重试)
        data = fetch_with_retry(url, headers)
        
        if not data:
            return {"status": "error", "message": "无法获取媒体数据，请检查 Emby 连接或 API Key 是否正确"}

        items = data.get("Items", [])
        
        # 6. 初始化统计数据结构
        stats = {
            "total_count": len(items),
            "resolution": {
                "4k": 0,      # 宽度 >= 3800
                "1080p": 0,   # 宽度 >= 1900
                "720p": 0,    # 宽度 >= 1200
                "sd": 0       # 其他
            },
            "video_codec": {
                "hevc": 0,    # H.265 / HEVC
                "h264": 0,    # H.264 / AVC
                "av1": 0,     # AV1
                "other": 0
            },
            "hdr_type": {
                "sdr": 0,
                "hdr10": 0,
                "dolby_vision": 0
            },
            "bad_quality_list": [] # 低画质洗版建议列表
        }

        # 7. 遍历数据进行统计
        for item in items:
            # 安全检查：确保 item 包含 MediaSources
            media_sources = item.get("MediaSources")
            if not media_sources or not isinstance(media_sources, list):
                continue
            
            source = media_sources[0]
            media_streams = source.get("MediaStreams")
            if not media_streams:
                continue
            
            # 找到视频流 (Type=Video)
            video_stream = next((s for s in media_streams if s.get("Type") == "Video"), None)
            if not video_stream:
                continue

            # --- A. 分辨率统计 ---
            width = video_stream.get("Width", 0)
            if width >= 3800:
                stats["resolution"]["4k"] += 1
            elif width >= 1900:
                stats["resolution"]["1080p"] += 1
            elif width >= 1200:
                stats["resolution"]["720p"] += 1
            else: 
                stats["resolution"]["sd"] += 1
                # 记录低画质 (SD/480P) 用于前端展示洗版建议
                # 限制列表长度防止 JSON 过大，只记录前 100 个
                if len(stats["bad_quality_list"]) < 100:
                    stats["bad_quality_list"].append({
                        "Name": item.get("Name"),
                        "SeriesName": item.get("SeriesName", ""),
                        "Year": item.get("ProductionYear"),
                        "Resolution": f"{width}x{video_stream.get('Height')}",
                        "Path": item.get("Path", "未知路径")
                    })

            # --- B. 编码格式统计 ---
            codec = video_stream.get("Codec", "").lower()
            if "hevc" in codec or "h265" in codec:
                stats["video_codec"]["hevc"] += 1
            elif "h264" in codec or "avc" in codec:
                stats["video_codec"]["h264"] += 1
            elif "av1" in codec:
                stats["video_codec"]["av1"] += 1
            else:
                stats["video_codec"]["other"] += 1

            # --- C. HDR/杜比视界统计 ---
            # Emby 4.10 可能在 DisplayTitle 或 VideoRange 中标识 HDR
            video_range = video_stream.get("VideoRange", "").lower()
            display_title = video_stream.get("DisplayTitle", "").lower()
            
            if "dolby" in display_title or "dv" in display_title or "dolby" in video_range:
                stats["hdr_type"]["dolby_vision"] += 1
            elif "hdr" in video_range or "hdr" in display_title or "pq" in video_range:
                stats["hdr_type"]["hdr10"] += 1
            else:
                stats["hdr_type"]["sdr"] += 1

        return {"status": "success", "data": stats}

    except requests.exceptions.Timeout:
        logger.error("Emby API 请求超时 (60s)")
        return {"status": "error", "message": "连接 Emby 超时 (60s)，您的媒体库可能正在扫描中，请稍后再试"}
        
    except requests.exceptions.ConnectionError:
        logger.error("Emby API 连接失败")
        return {"status": "error", "message": "无法连接到 Emby 服务器，请检查 IP 和端口是否正确"}
        
    except Exception as e:
        logger.error(f"质量盘点未知错误: {str(e)}")
        return {"status": "error", "message": f"扫描失败: {str(e)}"}