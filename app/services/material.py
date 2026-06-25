import math
import os
import random
import re
import threading
from io import BytesIO
from typing import Any, Callable, List
from urllib.parse import unquote, urlencode

import numpy as np
import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip
from PIL import Image

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.services import x_material
from app.utils import utils

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()
_CANDIDATE_SEARCH_LIMIT = 10
_CANDIDATE_GROUP_SIZE = 3
_CANDIDATE_ORIENTATIONS = (
    ("portrait", VideoAspect.portrait),
    ("landscape", VideoAspect.landscape),
)
_ORIENTATION_LABELS = {"portrait": "竖屏", "landscape": "横屏"}
_CANDIDATE_PROVIDER_FALLBACK = ("pexels",)
_SUPPORTED_CANDIDATE_PROVIDERS = {"x", "pexels", "pixabay", "coverr", "url"}
_DEFAULT_THUMBNAIL_TIMEOUT = 1.0
_MAX_THUMBNAIL_TIMEOUT = 10.0
_STOPWORDS = {
    "and",
    "are",
    "for",
    "from",
    "into",
    "near",
    "over",
    "show",
    "that",
    "the",
    "this",
    "with",
}
_VISUAL_ATTRIBUTE_TOKENS = {
    "aerial",
    "architecture",
    "building",
    "buildings",
    "city",
    "cityscape",
    "downtown",
    "drone",
    "landmark",
    "night",
    "skylight",
    "skyline",
    "street",
    "timelapse",
    "tower",
    "travel",
    "urban",
    "view",
}
_PLACE_TOKEN_ALIASES = {
    "guangdong": {"guangdong", "guangzhou", "canton", "shenzhen", "pearl river"},
    "guangzhou": {"guangzhou", "canton", "canton tower", "pearl river"},
    "canton": {"canton", "guangzhou", "canton tower"},
    "beijing": {"beijing", "tiananmen", "tiananmen square", "forbidden city"},
    "tiananmen": {"tiananmen", "tiananmen square"},
    "shanghai": {"shanghai", "lujiazui", "huangpu river"},
    "lujiazui": {"lujiazui", "shanghai"},
    "chongqing": {"chongqing", "mountain city"},
    "shenzhen": {"shenzhen", "futian"},
    "futian": {"futian", "shenzhen"},
}


def _get_tls_verify() -> bool:
    # 默认开启 TLS 证书校验，防止素材搜索和下载过程被中间人篡改。
    # 仅在企业代理、自签证书等明确需要的场景下，允许用户通过
    # `config.toml` 显式设置 `tls_verify = false` 临时关闭。
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )

    return bool(tls_verify)


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\nPlease set it in the config.toml file: {config.config_file}\n\n"
            f"{utils.to_json(config.app)}"
        )

    # if only one key is provided, return it
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        _api_key_counter += 1
        return api_keys[_api_key_counter % len(api_keys)]


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_score(value: float) -> float:
    return max(0.0, min(float(value), 100.0))


def _candidate_thumbnail_timeout() -> float:
    timeout = _safe_float(
        config.app.get("candidate_thumbnail_timeout", _DEFAULT_THUMBNAIL_TIMEOUT),
        _DEFAULT_THUMBNAIL_TIMEOUT,
    )
    return max(0.1, min(timeout, _MAX_THUMBNAIL_TIMEOUT))


def _candidate_orientation_label(orientation: str) -> str:
    return _ORIENTATION_LABELS.get(str(orientation or ""), str(orientation or ""))


def _default_candidate_orientation(video_aspect: VideoAspect) -> str:
    aspect = VideoAspect(video_aspect)
    if aspect == VideoAspect.landscape:
        return "landscape"
    return "portrait"


def _orientation_from_dimensions(width: int, height: int, fallback: str = "") -> str:
    if width and height:
        return "landscape" if width > height else "portrait"
    return fallback


def _select_pexels_video_file(
    video_files: list[dict],
    target_width: int,
    target_height: int,
    exact_resolution: bool,
) -> dict | None:
    usable_files = [
        video for video in video_files or [] if isinstance(video, dict) and video.get("link")
    ]
    if exact_resolution:
        for video in usable_files:
            if (
                _safe_int(video.get("width")) == target_width
                and _safe_int(video.get("height")) == target_height
            ):
                return video
        return None

    def quality_key(video: dict):
        width = _safe_int(video.get("width"))
        height = _safe_int(video.get("height"))
        area = width * height
        fps = _safe_int(video.get("fps"))
        ratio = width / height if width and height else 0
        target_ratio = target_width / target_height if target_height else ratio
        aspect_gap = abs(math.log(ratio / target_ratio)) if ratio and target_ratio else 9
        return area, fps, -aspect_gap

    return max(usable_files, key=quality_key, default=None)


def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    per_page: int = _CANDIDATE_SEARCH_LIMIT,
    exact_resolution: bool = True,
    use_orientation_filter: bool = True,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    per_page = max(1, min(int(per_page or _CANDIDATE_SEARCH_LIMIT), 80))
    params = {"query": search_term, "per_page": per_page}
    if use_orientation_filter:
        params["orientation"] = video_orientation
    query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["videos"]
        # loop through each video in the result
        for v in videos:
            duration = float(v.get("duration") or 0)
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v.get("video_files") or []
            selected_video = _select_pexels_video_file(
                video_files=video_files,
                target_width=video_width,
                target_height=video_height,
                exact_resolution=exact_resolution,
            )
            if not selected_video:
                continue

            item = MaterialInfo()
            item.provider = "pexels"
            item.url = str(selected_video.get("link") or "")
            item.duration = duration
            item.width = _safe_int(selected_video.get("width"))
            item.height = _safe_int(selected_video.get("height"))
            item.thumbnail_url = str(v.get("image") or "")
            item.source_page_url = str(v.get("url") or "")
            item.orientation = (
                video_orientation
                if use_orientation_filter
                else _orientation_from_dimensions(
                    item.width, item.height, video_orientation
                )
            )
            item.orientation_label = _candidate_orientation_label(item.orientation)
            if item.url:
                video_items.append(item)
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 50,
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        response = r.json()
        video_items = []
        if "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["hits"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["videos"]
            # loop through each url to determine the best quality
            for video_type in video_files:
                video = video_files[video_type]
                w = int(video["width"])
                # h = int(video["height"])
                if w >= video_width:
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video["url"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_coverr(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    Coverr (https://coverr.co) - free HD/4K stock videos,
    subject to Coverr license terms (https://coverr.co/license).

    Coverr API notes (based on official docs at api.coverr.co/docs/):
      - 鉴权: Authorization: Bearer <api_key>
      - 搜索端点: GET /videos?query=...,响应结构 {"hits": [...], ...}
      - 加 ?urls=true 在搜索响应里直接返回 mp4 直链
      - URL 是 signed JWT(绑定 API key,无过期时间)
      - Coverr 库以 16:9 横屏为主,9:16 portrait 占比极低(约 1%)
        因此本函数不做 aspect_ratio 过滤,由下游 video.py 的
        resize + letterbox 逻辑统一处理
      - duration 字段同时存在 number 和 string 两种形态,本函数都接受

    本函数使用 urls.mp4_download 字段作为下载地址 —— 按 Coverr 官方文档
    (https://api.coverr.co/docs/videos/#download-a-video) 的说法,
    GET 这个 URL 本身就被 Coverr 当作一次合法的 download 事件计入统计,
    无需再调用 PATCH /videos/:id/stats/downloads。
    """
    api_key = get_api_key("coverr_api_keys")
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {
        "query": search_term,
        "page_size": 20,
        "urls": "true",
        "sort": "popular",
    }
    query_url = f"https://api.coverr.co/videos?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items: List[MaterialInfo] = []

        if not isinstance(response, dict) or "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items

        for v in response["hits"]:
            # duration 在不同响应里可能是 number(11.625) 或 string("10.500000")
            try:
                duration = int(float(v.get("duration") or 0))
            except (TypeError, ValueError):
                continue
            if duration < minimum_duration:
                continue

            video_id = v.get("id")
            mp4_download_url = (v.get("urls") or {}).get("mp4_download")
            if not video_id or not mp4_download_url:
                continue

            item = MaterialInfo()
            item.provider = "coverr"
            item.url = mp4_download_url
            item.duration = duration
            video_items.append(item)
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_x(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    source_urls: list[str] | None = None,
) -> List[MaterialInfo]:
    """
    Search Twitter/X video media through the external AgentReach CLI adapter.

    X does not expose a stable orientation filter, so orientation is inferred from
    returned metadata when available and otherwise left to the render pipeline.
    """
    del video_aspect
    try:
        return x_material.search_x_videos(
            search_term=search_term,
            minimum_duration=minimum_duration,
            source_urls=source_urls or [],
        )
    except Exception as exc:
        logger.warning(f"X video search failed for '{search_term}': {str(exc)}")
        return []


def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    # if video already exists, return the path
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"video already exists: {video_path}")
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    # if video does not exist, download it
    with open(video_path, "wb") as f:
        f.write(
            requests.get(
                video_url,
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(60, 240),
            ).content
        )

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        clip = None
        try:
            clip = VideoFileClip(video_path)
            duration = clip.duration
            fps = clip.fps
            if duration > 0 and fps > 0:
                return video_path
        except Exception as e:
            logger.warning(f"invalid video file: {video_path} => {str(e)}")
            try:
                os.remove(video_path)
            except Exception as remove_error:
                logger.warning(
                    f"failed to remove invalid video file: {video_path}, error: {str(remove_error)}"
                )
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception as close_error:
                    logger.warning(
                        f"failed to close video clip: {video_path}, error: {str(close_error)}"
                    )
    return ""


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    match_script_order: bool = False,
) -> List[str]:
    search_videos = search_videos_pexels
    if source == "pixabay":
        search_videos = search_videos_pixabay
    elif source == "coverr":
        search_videos = search_videos_coverr
    elif source == "x":
        search_videos = search_videos_x

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = os.path.join(utils.task_dir(task_id), "render_materials")
    elif not material_directory:
        material_directory = os.path.join(utils.task_dir(task_id), "render_materials")

    if match_script_order:
        return _download_videos_by_script_order(
            task_id=task_id,
            search_terms=search_terms,
            search_videos=search_videos,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )

    valid_video_items = []
    valid_video_urls = []
    found_duration = 0.0
    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        for item in video_items:
            if item.url not in valid_video_urls:
                valid_video_items.append(item)
                valid_video_urls.append(item.url)
                found_duration += item.duration

    logger.info(
        f"found total videos: {len(valid_video_items)}, required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )
    video_paths = []

    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    if concat_mode_value == VideoConcatMode.random.value:
        random.shuffle(valid_video_items)

    total_duration = 0.0
    for item in valid_video_items:
        try:
            logger.info(f"downloading video: {item.url}")
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
            if saved_video_path:
                logger.info(f"video saved: {saved_video_path}")
                video_paths.append(saved_video_path)
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds
                if total_duration > audio_duration:
                    logger.info(
                        f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            logger.error(f"failed to download video: {utils.to_json(item)} => {str(e)}")
    logger.success(f"downloaded {len(video_paths)} videos")
    return video_paths


def download_videos_for_segments(
    task_id: str,
    segments: List[dict],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    max_clip_duration: int = 5,
) -> tuple[List[str], List[dict]]:
    search_videos = search_videos_pexels
    if source == "pixabay":
        search_videos = search_videos_pixabay
    elif source == "coverr":
        search_videos = search_videos_coverr
    elif source == "x":
        search_videos = search_videos_x

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = os.path.join(utils.task_dir(task_id), "render_materials")
    elif not material_directory:
        material_directory = os.path.join(utils.task_dir(task_id), "render_materials")

    logger.info("downloading one video per script subtitle segment")
    used_video_urls = set()
    downloaded_paths = []
    matched_segments = []
    last_material_path = ""
    last_source_url = ""

    for segment in segments:
        segment_info = dict(segment)
        search_term = (segment_info.get("term") or segment_info.get("text") or "").strip()
        segment_duration = max(float(segment_info.get("duration") or 0.0), 1.0)
        minimum_duration = max(
            1,
            min(
                int(math.ceil(segment_duration)),
                int(max_clip_duration or math.ceil(segment_duration)),
            ),
        )

        video_items = []
        if search_term:
            video_items = search_videos(
                search_term=search_term,
                minimum_duration=minimum_duration,
                video_aspect=video_aspect,
            )
        logger.info(
            f"found {len(video_items)} segment videos for '{search_term}', "
            f"segment duration: {segment_duration:.2f}s"
        )

        unique_items = [item for item in video_items if item.url not in used_video_urls]
        candidate_items = unique_items or video_items
        saved_video_path = ""
        selected_source_url = ""

        for item in candidate_items:
            try:
                logger.info(
                    f"downloading segment video for '{search_term}': {item.url}"
                )
                saved_video_path = save_video(
                    video_url=item.url, save_dir=material_directory
                )
                if saved_video_path:
                    selected_source_url = item.url
                    used_video_urls.add(item.url)
                    logger.info(f"segment video saved: {saved_video_path}")
                    break
            except Exception as e:
                logger.error(
                    f"failed to download segment video: {utils.to_json(item)} => {str(e)}"
                )
                saved_video_path = ""
                selected_source_url = ""

        if not saved_video_path and last_material_path:
            logger.warning(
                f"no material found for segment '{search_term}', reusing previous material"
            )
            saved_video_path = last_material_path
            selected_source_url = last_source_url

        if saved_video_path:
            last_material_path = saved_video_path
            last_source_url = selected_source_url
            downloaded_paths.append(saved_video_path)
            segment_info["material"] = saved_video_path
            segment_info["material_source_url"] = selected_source_url
            segment_info["provider"] = source
        else:
            segment_info["material"] = ""
            segment_info["material_source_url"] = ""
            segment_info["provider"] = source

        matched_segments.append(segment_info)

    logger.success(f"downloaded {len(downloaded_paths)} segment videos")
    return downloaded_paths, matched_segments


def _metadata_text_for_candidate(item: MaterialInfo) -> str:
    text = " ".join(
        [
            item.source_page_url or "",
            item.thumbnail_url or "",
            item.url or "",
        ]
    )
    return re.sub(r"[^a-z0-9]+", " ", unquote(text).lower()).strip()


def _keyword_tokens(*parts: str) -> list[str]:
    tokens = []
    seen = set()
    for token in re.findall(r"[a-z0-9]+", " ".join(parts).lower()):
        if len(token) <= 2 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _metadata_contains_term(metadata_text: str, term: str) -> bool:
    term = re.sub(r"[^a-z0-9]+", " ", (term or "").lower()).strip()
    if not term:
        return False
    return bool(re.search(rf"(^|\s){re.escape(term)}($|\s)", metadata_text or ""))


def _matched_token_alias(token: str, metadata_text: str) -> str:
    aliases = _PLACE_TOKEN_ALIASES.get(token, {token})
    for alias in sorted(aliases, key=len, reverse=True):
        if _metadata_contains_term(metadata_text, alias):
            return alias
    return ""


def _search_token_weight(token: str) -> float:
    if token in _PLACE_TOKEN_ALIASES:
        return 2.8
    if token in _VISUAL_ATTRIBUTE_TOKENS:
        return 0.7
    return 1.0


def _metadata_relevance_score(
    item: MaterialInfo,
    search_term: str,
    segment_text: str,
    original_index: int,
) -> tuple[float, str]:
    metadata_text = _metadata_text_for_candidate(item)
    page_text = re.sub(
        r"[^a-z0-9]+",
        " ",
        unquote(item.source_page_url or "").lower(),
    ).strip()
    search_tokens = _keyword_tokens(search_term)
    context_tokens = _keyword_tokens(search_term, segment_text)
    phrase = " ".join(search_tokens)

    if not context_tokens:
        return _clamp_score(65.0 - (original_index * 0.5)), "ranked by source order"

    matched_search = []
    matched_context = []
    matched_page = []
    place_matches = []
    matched_search_weight = 0.0
    total_search_weight = 0.0
    for token in search_tokens:
        token_weight = _search_token_weight(token)
        total_search_weight += token_weight
        matched_alias = _matched_token_alias(token, metadata_text)
        if matched_alias:
            matched_search.append(matched_alias)
            matched_search_weight += token_weight
            if token in _PLACE_TOKEN_ALIASES:
                place_matches.append(matched_alias)
        page_alias = _matched_token_alias(token, page_text)
        if page_alias:
            matched_page.append(page_alias)

    matched_context = [
        token for token in context_tokens if _matched_token_alias(token, metadata_text)
    ]

    search_coverage = (
        matched_search_weight / total_search_weight if total_search_weight else 0.0
    )
    context_coverage = len(matched_context) / len(context_tokens)
    phrase_bonus = 14.0 if phrase and phrase in metadata_text else 0.0
    page_bonus = min(len(matched_page) * 4.0, 12.0)
    place_bonus = min(len(place_matches) * 18.0, 28.0)
    has_place_token = any(token in _PLACE_TOKEN_ALIASES for token in search_tokens)
    place_penalty = 22.0 if has_place_token and not place_matches else 0.0
    order_bonus = max(0.0, 8.0 - (original_index * 0.35))
    score = (
        24.0
        + (search_coverage * 44.0)
        + (context_coverage * 12.0)
        + phrase_bonus
        + page_bonus
        + place_bonus
        + order_bonus
        - place_penalty
    )

    if place_matches:
        reason = f"place match on {', '.join(place_matches[:3])}"
    elif matched_search:
        reason = f"matches {', '.join(matched_search[:3])} metadata"
    elif matched_context:
        reason = f"context match on {', '.join(matched_context[:3])}"
    else:
        reason = "no strong keyword match in URLs"
    if phrase_bonus:
        reason = f"{reason}; exact phrase"
    return _clamp_score(score), reason


def _score_thumbnail_image(image: Image.Image) -> tuple[float, str]:
    image = image.convert("RGB")
    image.thumbnail((192, 192), Image.Resampling.LANCZOS)
    arr = np.asarray(image, dtype=np.float32)
    if arr.size == 0 or arr.ndim != 3:
        return 50.0, "thumbnail unavailable"

    luminance = (
        (arr[:, :, 0] * 0.299) + (arr[:, :, 1] * 0.587) + (arr[:, :, 2] * 0.114)
    )
    brightness = float(np.mean(luminance) / 255.0)
    contrast = float(np.std(luminance))
    hist, _ = np.histogram(luminance, bins=64, range=(0, 255))
    if hist.sum() > 0:
        probabilities = hist.astype(np.float64) / hist.sum()
        probabilities = probabilities[probabilities > 0]
        entropy = float(-(probabilities * np.log2(probabilities)).sum())
    else:
        entropy = 0.0

    if luminance.shape[0] > 1 and luminance.shape[1] > 1:
        sharpness_metric = float(
            np.mean(np.abs(np.diff(luminance, axis=1)))
            + np.mean(np.abs(np.diff(luminance, axis=0)))
        )
    else:
        sharpness_metric = 0.0
    color_variance = float(np.mean(np.std(arr.reshape(-1, 3), axis=0)))

    brightness_score = _clamp_score(100.0 - (abs(brightness - 0.52) * 230.0))
    contrast_score = _clamp_score((contrast / 64.0) * 100.0)
    entropy_score = _clamp_score((entropy / 6.0) * 100.0)
    sharpness_score = _clamp_score((sharpness_metric / 28.0) * 100.0)
    color_score = _clamp_score((color_variance / 64.0) * 100.0)

    score = (
        brightness_score * 0.20
        + contrast_score * 0.22
        + entropy_score * 0.26
        + sharpness_score * 0.22
        + color_score * 0.10
    )
    if brightness < 0.08 and contrast < 14.0:
        return min(score, 18.0), "thumbnail appears too dark"
    if brightness > 0.93 and contrast < 16.0:
        return min(score, 22.0), "thumbnail appears over-bright"
    if sharpness_score < 25.0:
        return _clamp_score(score), "thumbnail has limited sharpness"
    return _clamp_score(score), "thumbnail has usable contrast and sharpness"


def _thumbnail_visual_score(item: MaterialInfo) -> tuple[float, str]:
    if not item.thumbnail_url:
        return 50.0, "no thumbnail available"

    timeout = _candidate_thumbnail_timeout()
    try:
        response = requests.get(
            item.thumbnail_url,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(min(2.0, timeout), timeout),
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        with Image.open(BytesIO(response.content)) as image:
            return _score_thumbnail_image(image)
    except Exception as e:
        logger.debug(
            f"thumbnail scoring skipped for {item.thumbnail_url}: {str(e)}"
        )
        return 50.0, "thumbnail unavailable"


def _quality_score_for_candidate(
    item: MaterialInfo,
    video_aspect: VideoAspect,
    minimum_duration: int,
    already_used: bool,
) -> tuple[float, str]:
    width = _safe_int(item.width)
    height = _safe_int(item.height)
    duration = _safe_float(item.duration)
    area = width * height
    target_width, target_height = VideoAspect(video_aspect).to_resolution()
    target_area = target_width * target_height
    target_ratio = target_width / target_height if target_height else 0
    ratio = width / height if width and height else 0

    resolution_score = min(area / target_area, 1.0) * 100.0 if area else 45.0
    duration_score = min(duration / max(minimum_duration, 1), 1.0) * 100.0
    if ratio and target_ratio:
        aspect_gap = abs(math.log(ratio / target_ratio))
        aspect_score = max(45.0, 100.0 - min(aspect_gap * 55.0, 55.0))
    else:
        aspect_score = 60.0
    source_score = 100.0 if item.source_page_url or item.thumbnail_url else 70.0

    score = (
        resolution_score * 0.40
        + duration_score * 0.25
        + aspect_score * 0.25
        + source_score * 0.10
    )
    if already_used:
        score -= 25.0

    reason_bits = []
    if width and height:
        reason_bits.append(f"{width}x{height}")
    if duration:
        reason_bits.append(f"{duration:.1f}s")
    if aspect_score < 75.0:
        reason_bits.append("resizes to fit")
    if already_used:
        reason_bits.append("duplicate lowered")

    return _clamp_score(score), ", ".join(reason_bits)


def _candidate_record_for_item(
    item: MaterialInfo,
    candidate_id: str,
    search_term: str,
    segment_text: str,
    video_aspect: VideoAspect,
    minimum_duration: int,
    used_video_urls: set,
    original_index: int,
) -> dict[str, Any]:
    already_used = bool(item.url and item.url in used_video_urls)
    quality_score, quality_reason = _quality_score_for_candidate(
        item=item,
        video_aspect=video_aspect,
        minimum_duration=minimum_duration,
        already_used=already_used,
    )
    relevance_value, relevance_reason = _metadata_relevance_score(
        item=item,
        search_term=search_term,
        segment_text=segment_text,
        original_index=original_index,
    )
    visual_score, visual_reason = _thumbnail_visual_score(item)
    reason_bits = [relevance_reason]
    if quality_reason:
        reason_bits.append(quality_reason)
    if visual_reason and visual_reason not in {
        "thumbnail unavailable",
        "no thumbnail available",
    }:
        reason_bits.append(visual_reason)

    return {
        "candidate_id": candidate_id,
        "item": item,
        "original_index": original_index,
        "quality_score": quality_score,
        "relevance_score": relevance_value,
        "keyword_score": relevance_value,
        "visual_score": visual_score,
        "score": _clamp_score(
            (relevance_value * 0.50)
            + (quality_score * 0.30)
            + (visual_score * 0.20)
        ),
        "reason": "; ".join(bit for bit in reason_bits if bit),
    }


def _dedupe_video_items(video_items: List[MaterialInfo]) -> List[MaterialInfo]:
    deduped_items = []
    seen_urls = set()
    for item in video_items:
        if not item.url or item.url in seen_urls:
            continue
        seen_urls.add(item.url)
        deduped_items.append(item)
    return deduped_items


def _candidate_provider_list(segment: dict, source: str) -> list[str]:
    raw_providers = segment.get("providers")
    if isinstance(raw_providers, str):
        provider_values = re.split(r"[,，;；|/]+", raw_providers)
    elif isinstance(raw_providers, list):
        provider_values = raw_providers
    else:
        provider_values = [source]

    providers = []
    for provider in provider_values:
        provider_name = str(provider or "").strip().lower()
        if provider_name == "twitter":
            provider_name = "x"
        if provider_name == "mixed":
            provider_name = "pexels"
        if provider_name in _SUPPORTED_CANDIDATE_PROVIDERS and provider_name not in providers:
            providers.append(provider_name)
    return providers or list(_CANDIDATE_PROVIDER_FALLBACK)


def _source_urls_for_segment(segment: dict) -> list[str]:
    raw_urls = segment.get("source_urls") or []
    if isinstance(raw_urls, str):
        raw_urls = re.split(r"[\n,，;；]+", raw_urls)
    urls = []
    for raw_url in raw_urls:
        url = str(raw_url or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _looks_like_direct_video_url(url: str) -> bool:
    return bool(
        re.search(r"\.(mp4|mov|m4v|webm|m3u8)(?:$|[?#])", url or "", re.I)
        or "video.twimg.com" in (url or "").lower()
    )


def _looks_like_x_page_url(url: str) -> bool:
    return bool(re.search(r"https?://(?:www\.)?(?:x|twitter)\.com/", url or "", re.I))


def _direct_source_video_items(
    source_urls: list[str],
    minimum_duration: int,
) -> list[MaterialInfo]:
    items = []
    for url in source_urls:
        if not _looks_like_direct_video_url(url):
            continue
        item = MaterialInfo()
        item.provider = "url"
        item.url = url
        item.duration = float(max(minimum_duration, 1))
        item.source_page_url = url
        item.reason = "provided by preproduction source URL"
        items.append(item)
    return items


def _rank_candidate_items(
    search_term: str,
    segment_text: str,
    video_items: List[MaterialInfo],
    video_aspect: VideoAspect,
    minimum_duration: int,
    used_video_urls: set,
    segment_index: int,
    candidates_per_segment: int,
) -> List[dict[str, Any]]:
    ordered_items = _dedupe_video_items(video_items)[:_CANDIDATE_SEARCH_LIMIT]
    ordered_items = [item for item in ordered_items if item.url not in used_video_urls] + [
        item for item in ordered_items if item.url in used_video_urls
    ]

    records = [
        _candidate_record_for_item(
            item=item,
            candidate_id=f"seg-{segment_index}-raw-{index + 1}",
            search_term=search_term,
            segment_text=segment_text,
            video_aspect=video_aspect,
            minimum_duration=minimum_duration,
            used_video_urls=used_video_urls,
            original_index=index,
        )
        for index, item in enumerate(ordered_items)
    ]
    if not records:
        return []

    for record in records:
        record["score"] = _clamp_score(
            (record["relevance_score"] * 0.50)
            + (record["quality_score"] * 0.30)
            + (record["visual_score"] * 0.20)
        )
        if not record["reason"]:
            record["reason"] = "matched by search metadata and quality heuristics"

    return sorted(
        records,
        key=lambda record: (
            -record["score"],
            -record["relevance_score"],
            -record["quality_score"],
            -record["visual_score"],
            record["original_index"],
        ),
    )[:candidates_per_segment]


def _candidate_payload_from_record(
    record: dict[str, Any],
    segment_index: int,
    rank: int,
    orientation: str,
    group_rank: int,
    is_default_group: bool,
    fallback: bool = False,
) -> dict:
    item: MaterialInfo = record["item"]
    orientation = orientation or item.orientation or ""
    cached_path = item.cached_path or ""
    preview_url = cached_path or item.url
    return {
        "candidate_id": f"seg-{segment_index}-cand-{rank}",
        "rank": rank,
        "provider": item.provider or "pexels",
        "material": cached_path,
        "source_url": item.url,
        "preview_url": preview_url,
        "duration": float(item.duration or 0),
        "width": int(item.width or 0),
        "height": int(item.height or 0),
        "thumbnail_url": item.thumbnail_url or "",
        "source_page_url": item.source_page_url or "",
        "author": item.author or "",
        "tweet_id": item.tweet_id or "",
        "media_type": item.media_type or "",
        "attribution": item.attribution or "",
        "cached_path": cached_path,
        "score": round(float(record.get("score") or 0.0), 2),
        "relevance_score": round(float(record.get("relevance_score") or 0.0), 2),
        "keyword_score": round(float(record.get("keyword_score") or 0.0), 2),
        "quality_score": round(float(record.get("quality_score") or 0.0), 2),
        "visual_score": round(float(record.get("visual_score") or 0.0), 2),
        "orientation": orientation,
        "orientation_label": _candidate_orientation_label(orientation),
        "group_rank": int(group_rank or 0),
        "is_default_group": bool(is_default_group),
        "reason": str(record.get("reason") or "").strip(),
        "fallback": fallback,
    }


def _cache_x_candidate_records(
    task_id: str,
    records: list[dict[str, Any]],
    segment_index: int,
):
    cache_policy = str(config.app.get("x_cache_policy", "task") or "task").strip().lower()
    if cache_policy not in {"task", "true", "1", "yes"}:
        return

    cache_dir = os.path.join(utils.task_dir(task_id), "x_materials", f"segment-{segment_index:03d}")
    for record in records:
        item = record.get("item")
        if not isinstance(item, MaterialInfo) or item.provider != "x":
            continue
        if item.media_type and item.media_type != "video":
            continue
        if item.cached_path and os.path.exists(item.cached_path):
            continue
        try:
            saved_path = save_video(video_url=item.url, save_dir=cache_dir)
        except Exception as exc:
            logger.warning(f"failed to cache X candidate video: {item.url}, error: {str(exc)}")
            saved_path = ""
        if saved_path:
            item.cached_path = saved_path


def download_candidate_videos_for_segments(
    task_id: str,
    segments: List[dict],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    max_clip_duration: int = 5,
    candidates_per_segment: int = 3,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[List[str], List[dict]]:
    """
    Prepare multiple remote preview candidates for each subtitle/script segment.

    Pexels keeps the portrait/landscape dual-search behavior. CSV manifest rows
    may override providers per segment, including X via the AgentReach adapter.
    """
    logger.info("preparing editable remote candidates for script segments")

    updated_segments: List[dict] = []
    used_video_urls = set()
    last_candidates: List[dict] = []
    total_segments = len(segments)
    default_orientation = _default_candidate_orientation(video_aspect)

    for segment_number, segment in enumerate(segments, start=1):
        segment_info = dict(segment)
        segment_index = int(segment_info.get("index") or len(updated_segments) + 1)
        search_term = (segment_info.get("term") or segment_info.get("text") or "").strip()
        source_urls = _source_urls_for_segment(segment_info)
        providers = _candidate_provider_list(segment_info, source)
        if source_urls and "url" not in providers:
            providers.insert(0, "url")
        if any(_looks_like_x_page_url(url) for url in source_urls) and "x" not in providers:
            providers.insert(0, "x")
        if progress_callback:
            progress_callback(
                segment_number - 1,
                total_segments,
                f"正在搜索第 {segment_number}/{total_segments} 句：{search_term or '未命名片段'}",
            )
        segment_duration = max(float(segment_info.get("duration") or 0.0), 1.0)
        minimum_duration = max(
            1,
            min(
                int(math.ceil(segment_duration)),
                int(max_clip_duration or math.ceil(segment_duration)),
            ),
        )
        ranked_groups: dict[str, List[dict[str, Any]]] = {}
        for orientation, _ in _CANDIDATE_ORIENTATIONS:
            ranked_groups[orientation] = []

        for provider in providers:
            if provider == "pexels":
                for orientation, orientation_aspect in _CANDIDATE_ORIENTATIONS:
                    video_items = []
                    if search_term:
                        video_items = search_videos_pexels(
                            search_term=search_term,
                            minimum_duration=minimum_duration,
                            video_aspect=orientation_aspect,
                            per_page=_CANDIDATE_SEARCH_LIMIT,
                            exact_resolution=False,
                            use_orientation_filter=True,
                        )
                    logger.info(
                        f"found {len(video_items)} {orientation} {provider} candidate videos for "
                        f"'{search_term}', segment duration: {segment_duration:.2f}s"
                    )
                    ranked_groups[orientation].extend(
                        _rank_candidate_items(
                            search_term=search_term,
                            segment_text=str(segment_info.get("text") or ""),
                            video_items=video_items,
                            video_aspect=orientation_aspect,
                            minimum_duration=minimum_duration,
                            used_video_urls=used_video_urls,
                            segment_index=segment_index,
                            candidates_per_segment=candidates_per_segment,
                        )
                    )
                continue

            if provider == "x":
                video_items = []
                if search_term or source_urls:
                    video_items = search_videos_x(
                        search_term=search_term,
                        minimum_duration=minimum_duration,
                        video_aspect=video_aspect,
                        source_urls=source_urls,
                    )
                logger.info(
                    f"found {len(video_items)} X candidate videos for "
                    f"'{search_term}', segment duration: {segment_duration:.2f}s"
                )
            elif provider == "url":
                video_items = _direct_source_video_items(
                    source_urls=source_urls,
                    minimum_duration=minimum_duration,
                )
                logger.info(
                    f"found {len(video_items)} direct URL candidate videos for "
                    f"'{search_term}', segment duration: {segment_duration:.2f}s"
                )
            elif provider == "pixabay":
                video_items = (
                    search_videos_pixabay(
                        search_term=search_term,
                        minimum_duration=minimum_duration,
                        video_aspect=video_aspect,
                    )
                    if search_term
                    else []
                )
                logger.info(f"found {len(video_items)} pixabay candidate videos for '{search_term}'")
            elif provider == "coverr":
                video_items = (
                    search_videos_coverr(
                        search_term=search_term,
                        minimum_duration=minimum_duration,
                        video_aspect=video_aspect,
                    )
                    if search_term
                    else []
                )
                logger.info(f"found {len(video_items)} coverr candidate videos for '{search_term}'")
            else:
                continue

            provider_groups: dict[str, list[MaterialInfo]] = {
                orientation: [] for orientation, _ in _CANDIDATE_ORIENTATIONS
            }
            for item in video_items:
                item.orientation = (
                    item.orientation
                    or _orientation_from_dimensions(
                        _safe_int(item.width),
                        _safe_int(item.height),
                        default_orientation,
                    )
                )
                if item.orientation not in provider_groups:
                    item.orientation = default_orientation
                item.orientation_label = _candidate_orientation_label(item.orientation)
                provider_groups[item.orientation].append(item)

            for orientation, orientation_aspect in _CANDIDATE_ORIENTATIONS:
                records = _rank_candidate_items(
                    search_term=search_term,
                    segment_text=str(segment_info.get("text") or ""),
                    video_items=provider_groups.get(orientation, []),
                    video_aspect=orientation_aspect,
                    minimum_duration=minimum_duration,
                    used_video_urls=used_video_urls,
                    segment_index=segment_index,
                    candidates_per_segment=candidates_per_segment,
                )
                _cache_x_candidate_records(
                    task_id=task_id,
                    records=records,
                    segment_index=segment_index,
                )
                ranked_groups[orientation].extend(records)

        candidates = []
        ordered_orientations = [default_orientation] + [
            orientation
            for orientation, _ in _CANDIDATE_ORIENTATIONS
            if orientation != default_orientation
        ]
        for orientation in ordered_orientations:
            for group_rank, record in enumerate(
                sorted(
                    ranked_groups.get(orientation, []),
                    key=lambda record: (
                        -record["score"],
                        -record["relevance_score"],
                        -record["quality_score"],
                        record["original_index"],
                    ),
                ),
                start=1,
            ):
                candidate = _candidate_payload_from_record(
                    record=record,
                    segment_index=segment_index,
                    rank=len(candidates) + 1,
                    orientation=orientation,
                    group_rank=group_rank,
                    is_default_group=orientation == default_orientation,
                    fallback=False,
                )
                candidates.append(candidate)

        for candidate in candidates:
            used_video_urls.add(candidate["source_url"])

        if not candidates and last_candidates:
            logger.warning(
                f"no candidates found for segment '{search_term}', reusing previous candidates"
            )
            for previous in last_candidates:
                fallback_candidate = dict(previous)
                fallback_candidate["candidate_id"] = (
                    f"seg-{segment_index}-cand-{len(candidates) + 1}"
                )
                fallback_candidate["rank"] = len(candidates) + 1
                fallback_candidate["fallback"] = True
                fallback_candidate["reason"] = (
                    "reused previous segment candidate because no new candidates were found"
                )
                candidates.append(fallback_candidate)
                if len(candidates) >= candidates_per_segment * len(_CANDIDATE_ORIENTATIONS):
                    break

        if candidates:
            last_candidates = [dict(candidate) for candidate in candidates]
            segment_info["material"] = ""
            segment_info["material_source_url"] = candidates[0]["source_url"]
            segment_info["preview_url"] = candidates[0]["preview_url"]
            segment_info["provider"] = ",".join(providers)
        else:
            segment_info["material"] = ""
            segment_info["material_source_url"] = ""
            segment_info["preview_url"] = ""
            segment_info["provider"] = ",".join(providers)

        segment_info["candidates"] = candidates
        updated_segments.append(segment_info)
        if progress_callback:
            progress_callback(
                segment_number,
                total_segments,
                f"已准备第 {segment_number}/{total_segments} 句候选素材",
            )

    candidate_count = sum(
        len(segment.get("candidates") or []) for segment in updated_segments
    )
    logger.success(
        f"prepared {candidate_count} editable remote candidate videos for "
        f"{len(updated_segments)} segments"
    )
    return [], updated_segments


def _download_videos_by_script_order(
    task_id: str,
    search_terms: List[str],
    search_videos,
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """
    按脚本文案顺序下载素材。

    默认下载逻辑会把所有关键词的候选素材合并成一个大列表；如果第一个
    关键词返回很多结果，最终下载时可能一直消耗这个关键词的素材，后续
    脚本主题就排不上时间线。这里按关键词分组后轮询下载：
    第 1 轮取每个关键词的第 1 个候选，第 2 轮取每个关键词的第 2 个候选。
    这样在不重写视频合成引擎的前提下，尽量保证素材顺序贴近文案顺序。
    """
    logger.info("downloading videos with script-order material matching")
    candidate_groups = []
    valid_video_urls = set()
    found_duration = 0.0

    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        term_items = []
        for item in video_items:
            if item.url in valid_video_urls:
                continue
            term_items.append(item)
            valid_video_urls.add(item.url)
            found_duration += item.duration

        if term_items:
            candidate_groups.append((search_term, term_items))

    logger.info(
        f"found total ordered video candidates: {sum(len(items) for _, items in candidate_groups)}, "
        f"required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )

    video_paths = []
    total_duration = 0.0
    candidate_index = 0
    while candidate_groups and total_duration <= audio_duration:
        has_candidate = False
        for search_term, term_items in candidate_groups:
            if candidate_index >= len(term_items):
                continue

            has_candidate = True
            item = term_items[candidate_index]
            try:
                logger.info(
                    f"downloading ordered video for '{search_term}': {item.url}"
                )
                saved_video_path = save_video(
                    video_url=item.url, save_dir=material_directory
                )
                if saved_video_path:
                    logger.info(f"video saved: {saved_video_path}")
                    video_paths.append(saved_video_path)
                    total_duration += min(max_clip_duration, item.duration)
                    if total_duration > audio_duration:
                        logger.info(
                            f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                        )
                        break
            except Exception as e:
                logger.error(
                    f"failed to download ordered video: {utils.to_json(item)} => {str(e)}"
                )

        if not has_candidate:
            break
        candidate_index += 1

    logger.success(f"downloaded {len(video_paths)} ordered videos")
    return video_paths


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
