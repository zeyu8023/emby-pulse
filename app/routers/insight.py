from fastapi import APIRouter, Request
from app.core.config import cfg
import requests
import time
import logging

# 配置日志
logger = logging.getLogger("uvicorn")

router = APIRouter()

def get_emby_auth():
    return cfg.get("emby_host"), cfg.get("emby_api_key")

def fetch_with_retry(url, headers, retries=2):
    """基础重试请求"""
    for i in range(retries):
        try:
            # 30秒超时
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            pass # 忽略网络错误，重试
        time.sleep(0.5)
    return None

@router.get("/api/insight/quality")
def scan_library_quality(request: Request):
    """
    质量盘点 - ID-First 策略 (专治 Emby 4.10+)
    """
    # 1. 鉴权
    user = request.session.get("user")
    if not user: return {"status": "error", "message": "Unauthorized"}
    
    host, key = get_emby_auth()
    if not host or not key: return {"status": "error", "message": "Emby 未配置"}

    headers = {"X-Emby-Token": key, "Accept": "application/json"}
    
    # 2. 初始化统计
    stats = {
        "total_count": 0,
        "resolution": {"4k": 0, "1080p": 0, "720p": 0, "sd": 0},
        "video_codec": {"hevc": 0, "h264": 0, "av1": 0, "other": 0},
        "hdr_type": {"sdr": 0, "hdr10": 0, "dolby_vision": 0},
        "bad_quality_list": []
    }
    
    try:
        # 3. 第一步：只获取所有 ID (轻量级，不会崩)
        # 不请求 Fields，只请求 Id，速度极快
        logger.info("正在获取全量媒体 ID 索引...")
        id_url = f"{host}/emby/Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=Id"
        
        id_data = fetch_with_retry(id_url, headers)
        if not id_data or "Items" not in id_data:
            return {"status": "error", "message": "无法获取媒体索引，Emby 可能未就绪"}
            
        all_ids = [item["Id"] for item in id_data["Items"]]
        total_items = len(all_ids)
        logger.info(f"获取索引成功，共 {total_items} 个条目，准备分批拉取详情...")

        if total_items == 0:
             return {"status": "success", "data": stats}

        # 4. 第二步：分批次精确查询详情 (Batch Size = 50)
        # 使用 Ids=1,2,3 参数，避开数据库递归 Bug
        BATCH_SIZE = 50
        processed_count = 0

        for i in range(0, total_items, BATCH_SIZE):
            batch_ids = all_ids[i : i + BATCH_SIZE]
            ids_string = ",".join(batch_ids)
            
            # 精确查询这 50 个 ID 的详情
            detail_url = f"{host}/emby/Items?Ids={ids_string}&Fields=MediaSources,Path,MediaStreams"
            
            batch_data = fetch_with_retry(detail_url, headers)
            
            if batch_data and "Items" in batch_data:
                items = batch_data["Items"]
                processed_count += len(items)
                
                # --- 统计逻辑 (保持不变) ---
                for item in items:
                    media_sources = item.get("MediaSources")
                    if not media_sources or not isinstance(media_sources, list): continue
                    
                    # 取第一个源
                    source = media_sources[0]
                    media_streams = source.get("MediaStreams")
                    if not media_streams: continue
                    
                    # 取视频流
                    video_stream = next((s for s in media_streams if s.get("Type") == "Video"), None)
                    if not video_stream: continue

                    # 分辨率
                    width = video_stream.get("Width", 0)
                    if width >= 3800: stats["resolution"]["4k"] += 1
                    elif width >= 1900: stats["resolution"]["1080p"] += 1
                    elif width >= 1200: stats["resolution"]["720p"] += 1
                    else: 
                        stats["resolution"]["sd"] += 1
                        if len(stats["bad_quality_list"]) < 50:
                            stats["bad_quality_list"].append({
                                "Name": item.get("Name"),
                                "SeriesName": item.get("SeriesName", ""),
                                "Year": item.get("ProductionYear"),
                                "Resolution": f"{width}x{video_stream.get('Height')}",
                                "Path": item.get("Path", "")
                            })

                    # 编码
                    codec = video_stream.get("Codec", "").lower()
                    if "hevc" in codec or "h265" in codec: stats["video_codec"]["hevc"] += 1
                    elif "h264" in codec or "avc" in codec: stats["video_codec"]["h264"] += 1
                    elif "av1" in codec: stats["video_codec"]["av1"] += 1
                    else: stats["video_codec"]["other"] += 1

                    # HDR
                    video_range = video_stream.get("VideoRange", "").lower()
                    display_title = video_stream.get("DisplayTitle", "").lower()
                    if "dolby" in display_title or "dv" in display_title: stats["hdr_type"]["dolby_vision"] += 1
                    elif "hdr" in video_range or "hdr" in display_title: stats["hdr_type"]["hdr10"] += 1
                    else: stats["hdr_type"]["sdr"] += 1

            # 打印进度日志，方便排查
            if i % 500 == 0:
                logger.info(f"进度: {processed_count}/{total_items}...")

        stats["total_count"] = processed_count
        logger.info(f"扫描完成，有效数据: {processed_count}")
        return {"status": "success", "data": stats}

    except Exception as e:
        logger.error(f"严重错误: {str(e)}")
        # 即使报错，也尝试返回已统计的数据，避免前端 undefined
        return {"status": "success", "data": stats}