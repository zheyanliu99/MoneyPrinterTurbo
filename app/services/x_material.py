import json
import os
import re
import shlex
import shutil
import subprocess
from typing import Any

import yaml
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo


_DEFAULT_X_SEARCH_LIMIT = 10
_DEFAULT_X_TIMEOUT = 20


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _enabled() -> bool:
    value = config.app.get("enable_x_materials", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _configured_timeout() -> int:
    return max(3, _safe_int(config.app.get("x_timeout_seconds"), _DEFAULT_X_TIMEOUT))


def _configured_limit() -> int:
    return max(1, min(_safe_int(config.app.get("x_search_limit"), _DEFAULT_X_SEARCH_LIMIT), 50))


def _agent_reach_command() -> tuple[list[str], str | None]:
    command = str(config.app.get("agent_reach_command") or "agent-reach").strip()
    parts = shlex.split(command) if command else ["agent-reach"]
    if parts and shutil.which(parts[0]):
        return parts, None

    sibling_repo = os.path.abspath(
        os.path.join(config.root_dir, os.pardir, "Agent-Reach")
    )
    if os.path.isdir(sibling_repo) and shutil.which("uv"):
        return ["uv", "run", "agent-reach"], sibling_repo

    return parts, None


def run_command(
    args: list[str],
    timeout: int | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout or _configured_timeout(),
    )


def _load_structured_output(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def agent_reach_doctor() -> dict[str, Any]:
    command, cwd = _agent_reach_command()
    try:
        result = run_command(
            [*command, "doctor", "--json"],
            timeout=_configured_timeout(),
            cwd=cwd,
        )
    except Exception as exc:
        logger.warning(f"AgentReach doctor failed for X provider: {str(exc)}")
        return {}

    if result.returncode != 0:
        logger.warning(
            "AgentReach doctor returned non-zero status for X provider: "
            f"{result.stderr or result.stdout}"
        )
        return {}
    payload = _load_structured_output(result.stdout)
    return payload if isinstance(payload, dict) else {}


def active_twitter_backend(doctor_data: dict[str, Any] | None = None) -> str:
    doctor_data = doctor_data if doctor_data is not None else agent_reach_doctor()
    twitter_status = (doctor_data or {}).get("twitter") or {}
    if not isinstance(twitter_status, dict):
        return ""
    if str(twitter_status.get("status") or "").lower() != "ok":
        return ""
    return str(twitter_status.get("active_backend") or "").strip()


def _twitter_search_commands(backend: str, query: str, limit: int) -> list[list[str]]:
    backend_normalized = backend.lower()
    if "opencli" in backend_normalized:
        return [["opencli", "twitter", "search", query, "-f", "yaml"]]
    if "bird" in backend_normalized:
        return [
            ["bird", "search", query, "--json"],
            ["birdx", "search", query, "--json"],
            ["bird", "search", query],
        ]
    return [
        ["twitter", "search", query, "-n", str(limit), "--json"],
        ["twitter", "search", query, "-n", str(limit), "--yaml"],
        ["twitter", "search", query, "-n", str(limit)],
    ]


def _twitter_tweet_commands(backend: str, tweet_url: str) -> list[list[str]]:
    backend_normalized = backend.lower()
    if "opencli" in backend_normalized:
        return [["opencli", "twitter", "tweet", tweet_url, "-f", "yaml"]]
    if "bird" in backend_normalized:
        return [
            ["bird", "tweet", tweet_url, "--json"],
            ["birdx", "tweet", tweet_url, "--json"],
            ["bird", "tweet", tweet_url],
        ]
    return [
        ["twitter", "tweet", tweet_url, "--json"],
        ["twitter", "tweet", tweet_url, "--yaml"],
        ["twitter", "tweet", tweet_url],
    ]


def _run_first_success(commands: list[list[str]]) -> Any:
    for command in commands:
        try:
            result = run_command(command, timeout=_configured_timeout())
        except Exception as exc:
            logger.debug(f"X command failed before output: {command}, error: {str(exc)}")
            continue
        if result.returncode != 0:
            logger.debug(
                f"X command returned non-zero status: {command}, "
                f"stderr: {result.stderr}"
            )
            continue
        payload = _load_structured_output(result.stdout)
        if payload:
            return payload
    return None


def _iter_nodes(payload: Any):
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _iter_nodes(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_nodes(item)


def _iter_nodes_with_ancestors(payload: Any, ancestors: tuple[dict[str, Any], ...] = ()):
    if isinstance(payload, dict):
        yield payload, ancestors
        next_ancestors = (*ancestors, payload)
        for value in payload.values():
            yield from _iter_nodes_with_ancestors(value, next_ancestors)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_nodes_with_ancestors(item, ancestors)


def _first_text(node: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _looks_like_x_url(url: str) -> bool:
    return bool(re.search(r"https?://(?:www\.)?(?:x|twitter)\.com/", url or "", re.I))


def _looks_like_video_url(url: str) -> bool:
    return bool(
        re.search(r"\.(mp4|mov|m4v|webm|m3u8)(?:$|[?#])", url or "", re.I)
        or "video.twimg.com" in (url or "").lower()
    )


def _source_page_from_node(node: dict[str, Any]) -> str:
    for key in ("tweet_url", "source_page_url", "permalink", "link", "url"):
        value = _first_text(node, (key,))
        if _looks_like_x_url(value):
            return value
    tweet_id = _first_text(node, ("tweet_id", "id_str", "id", "status_id"))
    author = _author_from_node(node).lstrip("@")
    if tweet_id and author:
        return f"https://x.com/{author}/status/{tweet_id}"
    if tweet_id:
        return f"https://x.com/i/status/{tweet_id}"
    return ""


def _author_from_node(node: dict[str, Any]) -> str:
    for key in ("username", "screen_name", "handle", "author"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _author_from_node(value)
            if nested:
                return nested
    user = node.get("user")
    if isinstance(user, dict):
        return _author_from_node(user)
    return ""


def _best_variant_url(node: dict[str, Any]) -> str:
    variants = node.get("variants")
    if not isinstance(variants, list):
        video_info = node.get("video_info")
        if isinstance(video_info, dict):
            variants = video_info.get("variants")
    if not isinstance(variants, list):
        return ""

    best_url = ""
    best_bitrate = -1
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        url = _first_text(variant, ("url", "src"))
        content_type = _first_text(variant, ("content_type", "type")).lower()
        if not url or ("mp4" not in content_type and not _looks_like_video_url(url)):
            continue
        bitrate = _safe_int(variant.get("bitrate"), 0)
        if bitrate >= best_bitrate:
            best_url = url
            best_bitrate = bitrate
    return best_url


def _duration_from_node(node: dict[str, Any]) -> float:
    for key in ("duration", "duration_sec", "duration_seconds"):
        value = _safe_float(node.get(key), 0.0)
        if value > 0:
            return value
    for key in ("duration_ms", "duration_millis"):
        value = _safe_float(node.get(key), 0.0)
        if value > 0:
            return value / 1000
    video_info = node.get("video_info")
    if isinstance(video_info, dict):
        return _duration_from_node(video_info)
    return 0.0


def _dimensions_from_node(node: dict[str, Any]) -> tuple[int, int]:
    width = _safe_int(node.get("width") or node.get("w"))
    height = _safe_int(node.get("height") or node.get("h"))
    original_info = node.get("original_info")
    if (not width or not height) and isinstance(original_info, dict):
        width = width or _safe_int(original_info.get("width"))
        height = height or _safe_int(original_info.get("height"))
    sizes = node.get("sizes")
    if (not width or not height) and isinstance(sizes, dict):
        for size_info in sizes.values():
            if isinstance(size_info, dict):
                width = width or _safe_int(size_info.get("w"))
                height = height or _safe_int(size_info.get("h"))
                if width and height:
                    break
    return width, height


def _material_from_media_node(node: dict[str, Any]) -> MaterialInfo | None:
    if (
        "content_type" in node
        and "url" in node
        and not any(
            key in node
            for key in (
                "media_type",
                "type",
                "kind",
                "video_url",
                "media_url_https",
                "media_url",
                "thumbnail_url",
                "preview_image_url",
            )
        )
    ):
        return None

    media_url = _best_variant_url(node)
    if not media_url:
        media_url = _first_text(
            node,
            (
                "video_url",
                "videoUrl",
                "mp4",
                "mp4_url",
                "playback_url",
                "media_url_https",
                "media_url",
                "download_url",
                "src",
                "url",
            ),
        )
    if _looks_like_x_url(media_url):
        media_url = ""
    if not media_url:
        return None

    media_type = _first_text(node, ("media_type", "type", "kind")).lower()
    if not media_type:
        media_type = "video" if _looks_like_video_url(media_url) else "image"
    if media_type not in {"video", "animated_gif", "gif", "photo", "image"}:
        if _looks_like_video_url(media_url):
            media_type = "video"
        else:
            return None

    width, height = _dimensions_from_node(node)
    source_page_url = _source_page_from_node(node)
    author = _author_from_node(node)
    tweet_id = _first_text(node, ("tweet_id", "id_str", "id", "status_id"))
    thumbnail_url = _first_text(
        node,
        (
            "thumbnail_url",
            "thumb_url",
            "preview_image_url",
            "poster",
            "media_url_https",
        ),
    )
    if thumbnail_url == media_url and media_type in {"video", "animated_gif", "gif"}:
        thumbnail_url = _first_text(node, ("preview_image_url", "poster"))

    item = MaterialInfo()
    item.provider = "x"
    item.url = media_url
    item.duration = _duration_from_node(node)
    item.width = width
    item.height = height
    item.thumbnail_url = thumbnail_url or ""
    item.source_page_url = source_page_url
    item.author = author
    item.tweet_id = tweet_id
    item.media_type = "video" if media_type in {"video", "animated_gif", "gif"} else "image"
    item.attribution = f"X {author}".strip() if author else "X"
    return item


def parse_x_media_payload(payload: Any) -> list[MaterialInfo]:
    items: list[MaterialInfo] = []
    seen_urls = set()

    for node, ancestors in _iter_nodes_with_ancestors(payload):
        if not isinstance(node, dict):
            continue
        item = _material_from_media_node(node)
        if not item or item.url in seen_urls:
            continue
        if not item.source_page_url or not item.author or not item.tweet_id:
            for context in reversed(ancestors):
                if not item.source_page_url:
                    item.source_page_url = _source_page_from_node(context)
                if not item.author:
                    item.author = _author_from_node(context)
                if not item.tweet_id:
                    item.tweet_id = _first_text(
                        context, ("tweet_id", "id_str", "id", "status_id")
                    )
                if item.source_page_url and item.author and item.tweet_id:
                    break
        if item.author and item.attribution == "X":
            item.attribution = f"X {item.author}"
        seen_urls.add(item.url)
        items.append(item)
    return items


def _x_urls(source_urls: list[str] | None) -> list[str]:
    return [url for url in source_urls or [] if _looks_like_x_url(url)]


def search_x_media(
    search_term: str,
    minimum_duration: int = 1,
    limit: int | None = None,
    source_urls: list[str] | None = None,
) -> list[MaterialInfo]:
    if not _enabled():
        return []

    doctor = agent_reach_doctor()
    backend = active_twitter_backend(doctor)
    if not backend:
        twitter_status = (doctor or {}).get("twitter") or {}
        logger.warning(
            "X material provider is unavailable; AgentReach twitter status: "
            f"{twitter_status.get('status') or 'missing'}"
        )
        return []

    limit = max(1, min(int(limit or _configured_limit()), 50))
    payloads = []
    for tweet_url in _x_urls(source_urls):
        payload = _run_first_success(_twitter_tweet_commands(backend, tweet_url))
        if payload:
            payloads.append(payload)

    query = str(search_term or "").strip()
    if query:
        payload = _run_first_success(_twitter_search_commands(backend, query, limit))
        if payload:
            payloads.append(payload)

    media_items = []
    seen_urls = set()
    for payload in payloads:
        for item in parse_x_media_payload(payload):
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            media_items.append(item)

    minimum_duration = max(0, int(minimum_duration or 0))
    return [
        item
        for item in media_items
        if item.media_type != "video" or not minimum_duration or item.duration <= 0 or item.duration >= minimum_duration
    ][:limit]


def search_x_videos(
    search_term: str,
    minimum_duration: int = 1,
    limit: int | None = None,
    source_urls: list[str] | None = None,
) -> list[MaterialInfo]:
    return [
        item
        for item in search_x_media(
            search_term=search_term,
            minimum_duration=minimum_duration,
            limit=limit,
            source_urls=source_urls,
        )
        if item.media_type == "video"
    ]
