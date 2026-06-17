import json
import math
import os.path
import re
import shutil
from os import path
from typing import Callable

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams
from app.services import llm, material, subtitle, video, voice, upload_post
from app.services import state as sm
from app.utils import utils


def _emit_progress(
    progress_callback: Callable[[float, str], None] | None,
    progress: float,
    message: str,
):
    if not progress_callback:
        return
    try:
        progress_callback(max(0.0, min(float(progress), 1.0)), message)
    except Exception as exc:
        logger.warning(f"progress callback failed: {str(exc)}")


def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    video_script = params.video_script.strip()
    if not video_script:
        video_script = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number,
            video_script_prompt=params.video_script_prompt,
            custom_system_prompt=params.custom_system_prompt,
        )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video script.")
        return None

    return video_script


def _normalize_video_terms(video_terms):
    if not video_terms:
        return []
    if isinstance(video_terms, str):
        if video_terms.startswith("Error: "):
            return video_terms
        return [term.strip() for term in re.split(r"[,，]", video_terms) if term.strip()]
    if isinstance(video_terms, list):
        return [str(term).strip() for term in video_terms if str(term).strip()]
    raise ValueError("video_terms must be a string or a list of strings.")


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _terms_contain_cjk(video_terms) -> bool:
    if isinstance(video_terms, str):
        return _contains_cjk(video_terms)
    if isinstance(video_terms, list):
        return any(_contains_cjk(str(term)) for term in video_terms)
    return False


_CJK_STOCK_SEARCH_TRANSLATIONS = [
    ("常住人口", "city people"),
    ("黄浦江", "Huangpu River"),
    ("天安门", "Tiananmen"),
    ("陆家嘴", "Lujiazui"),
    ("小蛮腰", "Canton Tower"),
    ("珠江", "Pearl River"),
    ("长江", "Yangtze River"),
    ("广州", "Guangzhou"),
    ("重庆", "Chongqing"),
    ("深圳", "Shenzhen"),
    ("北京", "Beijing"),
    ("上海", "Shanghai"),
    ("中国", "China"),
    ("航拍", "aerial"),
    ("天际线", "skyline"),
    ("夜景", "night"),
    ("山城", "mountain city"),
    ("科技", "technology"),
    ("福田", "Futian"),
    ("城市", "city"),
    ("面积", "aerial city"),
    ("人口", "people"),
    ("商都", "commercial city"),
]


def _dedupe_adjacent_words(text: str) -> str:
    words = text.split()
    deduped = []
    for word in words:
        if not deduped or deduped[-1].lower() != word.lower():
            deduped.append(word)
    return " ".join(deduped)


def _translate_cjk_stock_search_term(term: str) -> str:
    if not _contains_cjk(term):
        return term.strip()

    translated = f" {term} "
    for source, replacement in _CJK_STOCK_SEARCH_TRANSLATIONS:
        translated = translated.replace(source, f" {replacement} ")

    translated = re.sub(r"[\u3400-\u9fff]+", " ", translated)
    translated = re.sub(r"[^\w\s.-]", " ", translated)
    translated = re.sub(r"\s+", " ", translated).strip()
    translated = _dedupe_adjacent_words(translated)
    return translated or "China city skyline"


def _translate_cjk_terms_for_stock_search(video_terms: list[str]) -> list[str]:
    return [_translate_cjk_stock_search_term(term) for term in video_terms]


def _uses_online_material_source(params) -> bool:
    return params.video_source in {"pexels", "pixabay", "coverr"}


def _resolve_voice_name_for_script(raw_voice_name: str, video_script: str) -> str:
    parsed_voice_name = voice.parse_voice_name(raw_voice_name or "")
    if voice.is_no_voice(raw_voice_name):
        return parsed_voice_name

    if _contains_cjk(video_script) and not parsed_voice_name.startswith(
        ("zh-", "yue-")
    ):
        fallback_voice = "zh-CN-XiaoxiaoNeural"
        logger.warning(
            "Chinese script detected but selected voice is not Chinese, "
            f"fallback to {fallback_voice}"
        )
        return fallback_voice

    return parsed_voice_name


def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    script_lines = utils.split_script_to_visual_lines(video_script)
    expected_term_count = len(script_lines) if params.match_materials_to_script else 5
    video_terms = _normalize_video_terms(params.video_terms)
    if isinstance(video_terms, str):
        return video_terms
    if _uses_online_material_source(params) and _terms_contain_cjk(video_terms):
        if (
            params.match_materials_to_script
            and video_terms
            and len(video_terms) != expected_term_count
        ):
            logger.warning(
                "manual Chinese video terms count does not match script sentence "
                "count, expected: "
                f"{expected_term_count}, actual: {len(video_terms)}; regenerating"
            )
            video_terms = []
        else:
            logger.warning(
                "manual video terms contain Chinese text, translating them to "
                "English stock-video search terms"
            )
            video_terms = _translate_cjk_terms_for_stock_search(video_terms)

    if (
        params.match_materials_to_script
        and video_terms
        and len(video_terms) != expected_term_count
    ):
        logger.warning(
            "manual video terms count does not match script sentence count, "
            f"expected: {expected_term_count}, actual: {len(video_terms)}; regenerating"
        )
        video_terms = []

    if not video_terms:
        video_terms = llm.generate_terms(
            video_subject=params.video_subject,
            video_script=video_script,
            amount=expected_term_count,
            match_script_order=params.match_materials_to_script,
        )
        if isinstance(video_terms, str) and video_terms.startswith("Error: "):
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(f"failed to generate video terms: {video_terms}")
            return None
        video_terms = _normalize_video_terms(video_terms)

    logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if not video_terms:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video terms.")
        return None

    return video_terms


def _parse_srt_time(time_value: str) -> float | None:
    match = re.fullmatch(r"(\d+):(\d+):(\d+),(\d+)", (time_value or "").strip())
    if not match:
        return None
    hours, minutes, seconds, milliseconds = match.groups()
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(milliseconds.ljust(3, "0")[:3]) / 1000
    )


def _parse_srt_time_range(time_range: str) -> tuple[float, float] | None:
    if not time_range or "-->" not in time_range:
        return None
    start_text, end_text = [part.strip() for part in time_range.split("-->", 1)]
    start_time = _parse_srt_time(start_text)
    end_time = _parse_srt_time(end_text)
    if start_time is None or end_time is None or end_time <= start_time:
        return None
    return start_time, end_time


def build_matched_segments(video_script, video_terms, subtitle_path):
    if not subtitle_path:
        return []

    subtitle_items = subtitle.file_to_subtitles(subtitle_path)
    if not subtitle_items:
        return []

    script_lines = utils.split_script_to_visual_lines(video_script)
    terms = _normalize_video_terms(video_terms)
    if isinstance(terms, str):
        terms = []

    matched_segments = []
    for item_index, subtitle_item in enumerate(subtitle_items):
        parsed_range = _parse_srt_time_range(subtitle_item[1])
        if not parsed_range:
            logger.warning(f"skip invalid subtitle time range: {subtitle_item[1]}")
            continue

        start_time, end_time = parsed_range
        text = subtitle_item[2].strip()
        if not text and item_index < len(script_lines):
            text = script_lines[item_index]

        term = ""
        if item_index < len(terms):
            term = terms[item_index]
        elif terms:
            term = terms[-1]
        else:
            term = text

        matched_segments.append(
            {
                "index": len(matched_segments) + 1,
                "text": text,
                "term": term,
                "start": round(start_time, 3),
                "end": round(end_time, 3),
                "duration": round(end_time - start_time, 3),
                "material": "",
            }
        )

    logger.info(f"built {len(matched_segments)} sentence-matched segments")
    return matched_segments


def save_script_data(
    task_id,
    video_script,
    video_terms,
    params,
    matched_segments=None,
    extra=None,
):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
    }
    if matched_segments is not None:
        script_data["matched_segments"] = matched_segments
    if extra:
        script_data.update(extra)

    with open(script_file, "w", encoding="utf-8") as f:
        f.write(utils.to_json(script_data))


def _read_script_data(task_id):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    if not path.exists(script_file):
        raise ValueError(f"script data not found for task: {task_id}")
    with open(script_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _coerce_video_params(raw_params):
    if isinstance(raw_params, VideoParams):
        return raw_params
    if not isinstance(raw_params, dict):
        raise ValueError("invalid saved video params")
    return VideoParams(**raw_params)


def _write_audio_segment_files(task_id, audio_file, matched_segments):
    from pydub import AudioSegment

    voice._configure_pydub_ffmpeg(AudioSegment)
    source_audio = AudioSegment.from_file(audio_file, format="mp3", codec="mp3")
    segments_dir = path.join(utils.task_dir(task_id), "audio_segments")
    os.makedirs(segments_dir, exist_ok=True)

    updated_segments = []
    previous_end_ms = 0
    for segment in matched_segments:
        segment_info = dict(segment)
        segment_index = int(segment_info.get("index") or len(updated_segments) + 1)
        start_ms = max(0, int(float(segment_info.get("start") or 0.0) * 1000))
        end_ms = max(start_ms + 1, int(float(segment_info.get("end") or 0.0) * 1000))
        start_ms = min(start_ms, len(source_audio))
        end_ms = min(max(end_ms, start_ms + 1), len(source_audio))

        segment_audio = source_audio[start_ms:end_ms]
        segment_audio_path = path.join(
            segments_dir, f"segment-{segment_index:03d}.mp3"
        )
        segment_audio.export(segment_audio_path, format="mp3")
        pause_before_ms = max(0, start_ms - previous_end_ms)
        previous_end_ms = max(previous_end_ms, end_ms)

        segment_info["audio_segment"] = {
            "file": segment_audio_path,
            "pause_before": round(pause_before_ms / 1000, 3),
            "original_text": segment_info.get("text", ""),
        }
        updated_segments.append(segment_info)

    audio_tail_pause = max(0, len(source_audio) - previous_end_ms) / 1000
    return updated_segments, round(audio_tail_pause, 3)


def _selection_to_dict(selection):
    if hasattr(selection, "model_dump"):
        return selection.model_dump()
    if isinstance(selection, dict):
        return dict(selection)
    raise ValueError("invalid selection item")


def _write_subtitle_file(subtitle_path, matched_segments):
    with open(subtitle_path, "w", encoding="utf-8") as f:
        for segment in matched_segments:
            f.write(
                utils.text_to_srt(
                    int(segment["index"]),
                    segment["text"],
                    float(segment["start"]),
                    float(segment["end"]),
                )
            )
            f.write("\n")


def generate_audio(task_id, params, video_script):
    '''
    Generate audio for the video script.
    If a custom audio file is provided, it will be used directly.
    There will be no subtitle maker object returned in this case.
    Otherwise, TTS will be used to generate the audio.
    Returns:
        - audio_file: path to the generated or provided audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object if TTS is used, None otherwise
    '''
    logger.info("\n\n## generating audio")
    # /audio 和 /subtitle 请求模型不包含 custom_audio_file，
    # 这里统一做兼容读取，避免直调接口时抛属性错误。
    custom_audio_file = getattr(params, "custom_audio_file", None)
    if not custom_audio_file or not os.path.exists(custom_audio_file):
        if custom_audio_file:
            logger.warning(
                f"custom audio file not found: {custom_audio_file}, using TTS to generate audio."
            )
        else:
            logger.info("no custom audio file provided, using TTS to generate audio.")
        audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
        sub_maker = voice.tts(
            text=video_script,
            voice_name=_resolve_voice_name_for_script(params.voice_name, video_script),
            voice_rate=params.voice_rate,
            voice_file=audio_file,
        )
        if sub_maker is None:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                """failed to generate audio:
1. check if the language of the voice matches the language of the video script.
2. check if the network is available. If you are in China, it is recommended to use a VPN and enable the global traffic mode.
            """.strip()
            )
            return None, None, None
        audio_duration = math.ceil(voice.get_audio_duration(sub_maker))
        if audio_duration == 0:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to get audio duration.")
            return None, None, None
        return audio_file, audio_duration, sub_maker
    else:
        logger.info(f"using custom audio file: {custom_audio_file}")
        audio_duration = voice.get_audio_duration(custom_audio_file)
        if audio_duration == 0:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to get audio duration from custom audio file.")
            return None, None, None
        return custom_audio_file, audio_duration, None

def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    '''
    Generate subtitle for the video script.
    If subtitle generation is disabled or no subtitle maker is provided, it will return an empty string.
    Otherwise, it will generate the subtitle using the specified provider.
    Returns:
        - subtitle_path: path to the generated subtitle file
    '''
    logger.info("\n\n## generating subtitle")
    if not params.subtitle_enabled or sub_maker is None:
        return ""

    subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    subtitle_fallback = False
    if subtitle_provider == "edge":
        voice.create_subtitle(
            text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path
        )
        if not os.path.exists(subtitle_path):
            subtitle_fallback = True
            logger.warning("subtitle file not found, fallback to whisper")

    if subtitle_provider == "whisper" or subtitle_fallback:
        subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path)
        logger.info("\n\n## correcting subtitle")
        subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

    subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
    if not subtitle_lines:
        logger.warning(f"subtitle file is invalid: {subtitle_path}")
        return ""

    return subtitle_path


def _assign_local_materials_to_segments(matched_segments, material_paths):
    if not matched_segments or not material_paths:
        return matched_segments

    updated_segments = []
    for index, segment in enumerate(matched_segments):
        segment_info = dict(segment)
        segment_info["material"] = material_paths[index % len(material_paths)]
        segment_info["provider"] = "local"
        segment_info["material_source_url"] = segment_info["material"]
        updated_segments.append(segment_info)
    return updated_segments


def get_video_materials(task_id, params, video_terms, audio_duration, matched_segments=None):
    if params.video_source == "local":
        logger.info("\n\n## preprocess local materials")
        materials = video.preprocess_video(
            materials=params.video_materials, clip_duration=params.video_clip_duration
        )
        if not materials:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "no valid materials found, please check the materials and try again."
            )
            return None
        material_paths = [material_info.url for material_info in materials]
        if params.match_materials_to_script and matched_segments:
            matched_segments = _assign_local_materials_to_segments(
                matched_segments, material_paths
            )
            return [segment["material"] for segment in matched_segments], matched_segments
        return material_paths, matched_segments
    else:
        logger.info(f"\n\n## downloading videos from {params.video_source}")
        if params.match_materials_to_script and matched_segments:
            downloaded_videos, matched_segments = material.download_videos_for_segments(
                task_id=task_id,
                segments=matched_segments,
                source=params.video_source,
                video_aspect=params.video_aspect,
                max_clip_duration=params.video_clip_duration,
            )
            if downloaded_videos:
                return downloaded_videos, matched_segments
            logger.warning(
                "sentence-level material matching found no videos, fallback to ordered download"
            )

        downloaded_videos = material.download_videos(
            task_id=task_id,
            search_terms=video_terms,
            source=params.video_source,
            video_aspect=params.video_aspect,
            video_concat_mode=(
                VideoConcatMode.sequential
                if params.match_materials_to_script
                else params.video_concat_mode
            ),
            audio_duration=audio_duration * params.video_count,
            max_clip_duration=params.video_clip_duration,
            match_script_order=params.match_materials_to_script,
        )
        if not downloaded_videos:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "failed to download videos, maybe the network is not available. if you are in China, please use a VPN."
            )
            return None
        return downloaded_videos, matched_segments


def generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path, matched_segments=None
):
    final_video_paths = []
    combined_video_paths = []
    use_segment_matching = bool(
        params.match_materials_to_script
        and matched_segments
        and all(segment.get("material") for segment in matched_segments)
    )
    # 多视频生成默认会打散素材以增加差异；但“按文案顺序匹配素材”追求的是
    # 时间线稳定性和可解释性，所以开启后所有输出都使用顺序拼接。
    if params.match_materials_to_script:
        video_concat_mode = VideoConcatMode.sequential
    elif params.video_count == 1:
        video_concat_mode = params.video_concat_mode
    else:
        video_concat_mode = VideoConcatMode.random
    video_transition_mode = params.video_transition_mode

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        if use_segment_matching:
            video.combine_videos_by_segments(
                combined_video_path=combined_video_path,
                segments=matched_segments,
                audio_file=audio_file,
                video_aspect=params.video_aspect,
                video_transition_mode=video_transition_mode,
                threads=params.n_threads,
            )
        else:
            video.combine_videos(
                combined_video_path=combined_video_path,
                video_paths=downloaded_videos,
                audio_file=audio_file,
                video_aspect=params.video_aspect,
                video_concat_mode=video_concat_mode,
                video_transition_mode=video_transition_mode,
                max_clip_duration=params.video_clip_duration,
                threads=params.n_threads,
            )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        video.generate_video(
            video_path=combined_video_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=final_video_path,
            params=params,
        )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)

    return final_video_paths, combined_video_paths


def _remove_path_quietly(file_or_dir: str):
    if not file_or_dir:
        return
    try:
        if path.isdir(file_or_dir):
            shutil.rmtree(file_or_dir, ignore_errors=True)
        elif path.exists(file_or_dir):
            os.remove(file_or_dir)
    except Exception as exc:
        logger.warning(f"failed to remove temporary artifact: {file_or_dir}, error: {str(exc)}")


def _cleanup_final_only_artifacts(
    task_id: str,
    material_paths=None,
    combined_video_paths=None,
    extra_paths=None,
):
    task_root = utils.task_dir(task_id)
    protected_paths = {
        path.realpath(file_path)
        for file_path in (combined_video_paths or [])
        if file_path
    }

    for file_path in material_paths or []:
        if not file_path or file_path.startswith(("http://", "https://")):
            continue
        real_file_path = path.realpath(file_path)
        if real_file_path in protected_paths:
            continue
        if real_file_path.startswith(path.realpath(task_root) + os.sep):
            _remove_path_quietly(real_file_path)

    for file_path in combined_video_paths or []:
        _remove_path_quietly(file_path)

    for file_path in extra_paths or []:
        _remove_path_quietly(file_path)

    for temp_dir in (
        path.join(task_root, "render_materials"),
        path.join(task_root, "selected_materials"),
        path.join(task_root, "candidates"),
    ):
        _remove_path_quietly(temp_dir)


def _materialize_remote_candidate(candidate: dict, task_id: str, url_to_path: dict) -> str:
    material_path = candidate.get("material") or ""
    if material_path and not material_path.startswith(("http://", "https://")):
        if path.exists(material_path):
            return material_path

    source_url = candidate.get("source_url") or candidate.get("preview_url") or ""
    if not source_url:
        raise ValueError("selected candidate is missing source_url")
    if source_url in url_to_path:
        return url_to_path[source_url]

    material_dir = path.join(utils.task_dir(task_id), "selected_materials")
    saved_path = material.save_video(video_url=source_url, save_dir=material_dir)
    if not saved_path:
        raise ValueError(f"failed to download selected candidate: {source_url}")
    url_to_path[source_url] = saved_path
    return saved_path


def _strip_task_local_materials(task_id: str, segments):
    if not segments:
        return segments
    task_root = path.realpath(utils.task_dir(task_id))
    sanitized_segments = []

    def is_task_local_file(file_path: str) -> bool:
        if not file_path or file_path.startswith(("http://", "https://")):
            return False
        return path.realpath(file_path).startswith(task_root + os.sep)

    for segment in segments:
        if not isinstance(segment, dict):
            sanitized_segments.append(segment)
            continue
        sanitized_segment = dict(segment)
        source_url = (
            sanitized_segment.get("material_source_url")
            or sanitized_segment.get("preview_url")
            or ""
        )
        if is_task_local_file(sanitized_segment.get("material") or ""):
            sanitized_segment["material"] = ""
        if source_url and not sanitized_segment.get("preview_url"):
            sanitized_segment["preview_url"] = source_url

        candidates = sanitized_segment.get("candidates")
        if isinstance(candidates, list):
            sanitized_candidates = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    sanitized_candidates.append(candidate)
                    continue
                sanitized_candidate = dict(candidate)
                candidate_url = (
                    sanitized_candidate.get("source_url")
                    or sanitized_candidate.get("preview_url")
                    or ""
                )
                if is_task_local_file(sanitized_candidate.get("material") or ""):
                    sanitized_candidate["material"] = ""
                if candidate_url and not sanitized_candidate.get("preview_url"):
                    sanitized_candidate["preview_url"] = candidate_url
                sanitized_candidates.append(sanitized_candidate)
            sanitized_segment["candidates"] = sanitized_candidates

        sanitized_segments.append(sanitized_segment)
    return sanitized_segments


def _prepare_candidates_from_timeline(
    task_id,
    params,
    video_script,
    video_terms,
    audio_file,
    audio_duration,
    subtitle_path,
    matched_segments,
    progress_callback=None,
):
    if params.video_source != "pexels":
        raise ValueError("candidate editor currently supports Pexels only.")
    if not matched_segments:
        raise ValueError(
            "candidate editor needs a valid subtitle timeline. Enable subtitles and TTS."
        )

    _emit_progress(progress_callback, 0.35, "正在切分每句音频...")
    matched_segments, audio_tail_pause = _write_audio_segment_files(
        task_id, audio_file, matched_segments
    )

    def _candidate_progress(current: int, total: int, message: str):
        total = max(total, 1)
        _emit_progress(
            progress_callback,
            0.40 + (max(current, 0) / total) * 0.55,
            message,
        )

    _, matched_segments = material.download_candidate_videos_for_segments(
        task_id=task_id,
        segments=matched_segments,
        source="pexels",
        video_aspect=params.video_aspect,
        max_clip_duration=params.video_clip_duration,
        candidates_per_segment=3,
        progress_callback=_candidate_progress,
    )
    first_segment = matched_segments[0] if matched_segments else {}
    if not first_segment.get("candidates"):
        raise ValueError(
            "Pexels returned no candidate videos for the first sentence. "
            "Try a clearer first keyword."
        )

    extra = {
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "requires_selection": True,
        "audio_tail_pause": audio_tail_pause,
    }
    save_script_data(
        task_id,
        video_script,
        video_terms,
        params,
        matched_segments=matched_segments,
        extra=extra,
    )
    remote_candidate_urls = []
    for segment in matched_segments:
        for candidate in segment.get("candidates") or []:
            source_url = candidate.get("source_url") or candidate.get("preview_url")
            if source_url and source_url not in remote_candidate_urls:
                remote_candidate_urls.append(source_url)

    kwargs = {
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": remote_candidate_urls,
        "matched_segments": matched_segments,
        "requires_selection": True,
    }
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )
    _emit_progress(progress_callback, 1.0, "候选素材准备完成")
    return kwargs


def _render_selection_impl(task_id, selections, progress_callback=None):
    logger.info(f"rendering selected candidates for task: {task_id}")
    selections = [_selection_to_dict(selection) for selection in selections]
    if not selections:
        raise ValueError("render selection requires at least one selected segment.")
    _emit_progress(progress_callback, 0.03, "正在检查候选选择...")

    script_data = _read_script_data(task_id)
    params = _coerce_video_params(script_data.get("params") or {})
    params.match_materials_to_script = True
    params.video_concat_mode = VideoConcatMode.sequential.value
    params.video_transition_mode = None
    params.video_count = 1

    original_segments = script_data.get("matched_segments") or []
    if not original_segments:
        raise ValueError("no candidate segments found for this task.")

    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=10,
        matched_segments=original_segments,
        requires_selection=True,
    )

    segment_by_index = {
        int(segment.get("index") or 0): segment for segment in original_segments
    }
    selections_by_index = {
        int(selection.get("segment_index") or 0): selection for selection in selections
    }
    missing_indexes = [
        int(segment.get("index") or 0)
        for segment in original_segments
        if int(segment.get("index") or 0) not in selections_by_index
    ]
    if missing_indexes:
        raise ValueError(f"missing selections for segments: {missing_indexes}")

    selected_segments = []
    selected_material_paths = []
    url_to_path = {}
    ordered_segment_indexes = sorted(segment_by_index)
    total_segments = max(len(ordered_segment_indexes), 1)
    for render_index, segment_index in enumerate(ordered_segment_indexes, start=1):
        segment = segment_by_index[segment_index]
        selection = selections_by_index[segment_index]
        _emit_progress(
            progress_callback,
            0.08 + ((render_index - 1) / total_segments) * 0.32,
            f"正在下载第 {render_index}/{total_segments} 句选中素材...",
        )
        candidates = segment.get("candidates") or []
        candidate_map = {
            str(candidate.get("candidate_id")): candidate for candidate in candidates
        }
        candidate_id = str(selection.get("candidate_id") or "")
        if candidate_id not in candidate_map:
            raise ValueError(
                f"invalid candidate_id for segment {segment_index}: {candidate_id}"
            )

        candidate = candidate_map[candidate_id]
        trim_start = float(selection.get("trim_start") or 0.0)
        trim_end = selection.get("trim_end")
        trim_end = float(trim_end) if trim_end not in (None, "") else None
        candidate_duration = float(candidate.get("duration") or 0.0)
        if trim_start < 0:
            raise ValueError(f"trim_start must be >= 0 for segment {segment_index}")
        if trim_end is not None and trim_end <= trim_start:
            raise ValueError(
                f"trim_end must be greater than trim_start for segment {segment_index}"
            )
        if candidate_duration > 0:
            if trim_start >= candidate_duration:
                raise ValueError(
                    f"trim_start exceeds candidate duration for segment {segment_index}"
                )
            if trim_end is not None and trim_end > candidate_duration + 0.1:
                raise ValueError(
                    f"trim_end exceeds candidate duration for segment {segment_index}"
                )

        selected_text = (selection.get("text") or segment.get("text") or "").strip()
        local_material_path = _materialize_remote_candidate(
            candidate=candidate,
            task_id=task_id,
            url_to_path=url_to_path,
        )
        segment_info = dict(segment)
        segment_info["text"] = selected_text
        segment_info["candidate_id"] = candidate_id
        segment_info["material"] = local_material_path
        segment_info["material_source_url"] = (
            candidate.get("source_url") or candidate.get("preview_url") or ""
        )
        segment_info["preview_url"] = (
            candidate.get("preview_url") or candidate.get("source_url") or ""
        )
        segment_info["provider"] = candidate.get("provider", "pexels")
        segment_info["trim_start"] = round(trim_start, 3)
        segment_info["trim_end"] = (
            round(trim_end, 3)
            if trim_end is not None
            else round(candidate_duration, 3)
        )
        selected_segments.append(segment_info)
        selected_material_paths.append(local_material_path)
        _emit_progress(
            progress_callback,
            0.08 + (render_index / total_segments) * 0.32,
            f"已准备第 {render_index}/{total_segments} 句视频素材",
        )

    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=25,
        matched_segments=selected_segments,
        requires_selection=True,
    )

    from pydub import AudioSegment

    voice._configure_pydub_ffmpeg(AudioSegment)
    edited_audio = AudioSegment.empty()
    edited_segments = []
    edited_audio_dir = path.join(utils.task_dir(task_id), "edited_audio_segments")
    os.makedirs(edited_audio_dir, exist_ok=True)

    for audio_index, segment in enumerate(selected_segments, start=1):
        segment_index = int(segment.get("index") or len(edited_segments) + 1)
        _emit_progress(
            progress_callback,
            0.42 + ((audio_index - 1) / total_segments) * 0.22,
            f"正在重建第 {audio_index}/{total_segments} 句音频...",
        )
        audio_segment_info = segment.get("audio_segment") or {}
        pause_before = max(float(audio_segment_info.get("pause_before") or 0.0), 0.0)
        if pause_before > 0:
            edited_audio += AudioSegment.silent(duration=int(pause_before * 1000))

        original_text = (
            audio_segment_info.get("original_text") or segment.get("text") or ""
        ).strip()
        selected_text = (segment.get("text") or "").strip()
        original_segment_file = audio_segment_info.get("file", "")
        segment_audio_file = original_segment_file

        if selected_text != original_text:
            segment_audio_file = path.join(
                edited_audio_dir, f"segment-{segment_index:03d}.mp3"
            )
            sub_maker = voice.tts(
                text=selected_text,
                voice_name=_resolve_voice_name_for_script(
                    params.voice_name, selected_text
                ),
                voice_rate=params.voice_rate,
                voice_file=segment_audio_file,
                voice_volume=params.voice_volume,
            )
            if sub_maker is None or not path.exists(segment_audio_file):
                raise ValueError(
                    f"failed to regenerate TTS for segment {segment_index}"
                )
        elif not original_segment_file or not path.exists(original_segment_file):
            raise ValueError(
                f"original audio slice missing for segment {segment_index}"
            )

        segment_audio = AudioSegment.from_file(
            segment_audio_file, format="mp3", codec="mp3"
        )
        start_time = len(edited_audio) / 1000
        edited_audio += segment_audio
        end_time = len(edited_audio) / 1000

        edited_segment = dict(segment)
        edited_segment["start"] = round(start_time, 3)
        edited_segment["end"] = round(end_time, 3)
        edited_segment["duration"] = round(end_time - start_time, 3)
        edited_segment["audio_segment"] = {
            **audio_segment_info,
            "file": segment_audio_file,
            "pause_before": round(pause_before, 3),
            "original_text": selected_text,
        }
        edited_segments.append(edited_segment)
        _emit_progress(
            progress_callback,
            0.42 + (audio_index / total_segments) * 0.22,
            f"已重建第 {audio_index}/{total_segments} 句音频",
        )

    tail_pause = max(float(script_data.get("audio_tail_pause") or 0.0), 0.0)
    if tail_pause > 0:
        edited_audio += AudioSegment.silent(duration=int(tail_pause * 1000))

    edited_audio_file = path.join(utils.task_dir(task_id), "audio-edited.mp3")
    edited_audio.export(edited_audio_file, format="mp3")
    edited_subtitle_path = path.join(utils.task_dir(task_id), "subtitle-edited.srt")
    _write_subtitle_file(edited_subtitle_path, edited_segments)
    audio_duration = voice.get_audio_duration(edited_audio_file)

    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=45,
        matched_segments=edited_segments,
        requires_selection=False,
    )
    _emit_progress(progress_callback, 0.70, "正在合成最终视频...")

    downloaded_videos = [segment["material"] for segment in edited_segments]
    final_video_paths, combined_video_paths = generate_final_videos(
        task_id,
        params,
        downloaded_videos,
        edited_audio_file,
        edited_subtitle_path,
        edited_segments,
    )
    if not final_video_paths:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        _cleanup_final_only_artifacts(
            task_id,
            material_paths=selected_material_paths,
            combined_video_paths=combined_video_paths,
            extra_paths=[edited_audio_dir],
        )
        return
    _emit_progress(progress_callback, 0.95, "正在整理输出文件...")

    script_file_extra = {
        "audio_file": edited_audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": edited_subtitle_path,
        "requires_selection": False,
        "selected_segments": _strip_task_local_materials(task_id, edited_segments),
        "audio_tail_pause": tail_pause,
    }
    save_script_data(
        task_id,
        script_data.get("script", ""),
        script_data.get("search_terms", []),
        params,
        matched_segments=_strip_task_local_materials(task_id, edited_segments),
        extra=script_file_extra,
    )
    metadata_segments = _strip_task_local_materials(task_id, edited_segments)

    kwargs = {
        "videos": final_video_paths,
        "combined_videos": [],
        "script": script_data.get("script", ""),
        "terms": script_data.get("search_terms", []),
        "audio_file": edited_audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": edited_subtitle_path,
        "materials": [],
        "matched_segments": metadata_segments,
        "requires_selection": False,
    }
    _cleanup_final_only_artifacts(
        task_id,
        material_paths=selected_material_paths,
        combined_video_paths=combined_video_paths,
        extra_paths=[edited_audio_dir],
    )
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )
    _emit_progress(progress_callback, 1.0, "最终视频已生成")
    return kwargs


def render_selection(task_id, selections, progress_callback=None):
    try:
        return _render_selection_impl(task_id, selections, progress_callback)
    except Exception as exc:
        logger.exception(f"failed to render selected candidates: {str(exc)}")
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            progress=100,
            error=str(exc),
        )
        return


def start(task_id, params: VideoParams, stop_at: str = "video", progress_callback=None):
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5)
    _emit_progress(progress_callback, 0.03, "正在生成脚本...")

    # 1. Generate script
    video_script = generate_script(task_id, params)
    if not video_script or "Error: " in video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)
    _emit_progress(progress_callback, 0.10, "脚本已完成，正在生成关键词...")

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, script=video_script
        )
        return {"script": video_script}

    # 2. Generate terms
    video_terms = ""
    if params.video_source != "local":
        video_terms = generate_terms(task_id, params, video_script)
        if not video_terms:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            return
    elif params.match_materials_to_script and params.video_terms:
        video_terms = _normalize_video_terms(params.video_terms)
        if isinstance(video_terms, str):
            logger.warning(
                f"ignore invalid local video terms for segment matching: {video_terms}"
            )
            video_terms = []

    save_script_data(task_id, video_script, video_terms, params)
    _emit_progress(progress_callback, 0.20, "关键词已完成，正在生成配音...")

    if stop_at == "terms":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, terms=video_terms
        )
        return {"script": video_script, "terms": video_terms}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20)

    # 3. Generate audio
    audio_file, audio_duration, sub_maker = generate_audio(
        task_id, params, video_script
    )
    if not audio_file:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)
    _emit_progress(progress_callback, 0.30, "配音已完成，正在生成字幕...")

    if stop_at == "audio":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            audio_file=audio_file,
        )
        return {"audio_file": audio_file, "audio_duration": audio_duration}

    # 4. Generate subtitle
    subtitle_path = generate_subtitle(
        task_id, params, video_script, sub_maker, audio_file
    )
    matched_segments = []
    if params.match_materials_to_script:
        matched_segments = build_matched_segments(
            video_script=video_script,
            video_terms=video_terms,
            subtitle_path=subtitle_path,
        )
        if not matched_segments:
            logger.warning(
                "no valid subtitle timeline for sentence-level material matching, "
                "fallback to ordered fixed-duration material matching"
            )
    _emit_progress(progress_callback, 0.34, "字幕已完成，正在准备候选素材...")

    if stop_at == "subtitle":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
            matched_segments=matched_segments,
        )
        return {"subtitle_path": subtitle_path, "matched_segments": matched_segments}

    if stop_at == "candidates":
        try:
            params.video_source = "pexels"
            params.match_materials_to_script = True
            params.video_concat_mode = VideoConcatMode.sequential.value
            params.video_transition_mode = None
            params.bgm_type = ""
            params.video_count = 1
            return _prepare_candidates_from_timeline(
                task_id=task_id,
                params=params,
                video_script=video_script,
                video_terms=video_terms,
                audio_file=audio_file,
                audio_duration=audio_duration,
                subtitle_path=subtitle_path,
                matched_segments=matched_segments,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            logger.exception(f"failed to prepare candidate materials: {str(exc)}")
            sm.state.update_task(
                task_id,
                state=const.TASK_STATE_FAILED,
                progress=100,
                error=str(exc),
            )
            return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=40)

    # 5. Get video materials
    materials_result = get_video_materials(
        task_id, params, video_terms, audio_duration, matched_segments
    )
    downloaded_videos = None
    if materials_result:
        downloaded_videos, matched_segments = materials_result
    if not downloaded_videos:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    if matched_segments:
        save_script_data(
            task_id,
            video_script,
            video_terms,
            params,
            matched_segments=matched_segments,
        )

    if stop_at == "materials":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            materials=downloaded_videos,
            matched_segments=matched_segments,
        )
        return {"materials": downloaded_videos, "matched_segments": matched_segments}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50)

    # 仅完整视频生成流程才需要处理视频拼接模式；
    # 这样可以避免 /subtitle 和 /audio 这类请求访问不存在的字段。
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    # 6. Generate final videos
    final_video_paths, combined_video_paths = generate_final_videos(
        task_id,
        params,
        downloaded_videos,
        audio_file,
        subtitle_path,
        matched_segments,
    )

    if not final_video_paths:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        _cleanup_final_only_artifacts(
            task_id,
            material_paths=downloaded_videos,
            combined_video_paths=combined_video_paths,
        )
        return

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos."
    )

    # 7. Cross-post to TikTok/Instagram (if enabled)
    cross_post_results = []
    if upload_post.upload_post_service.is_configured() and upload_post.upload_post_service.auto_upload:
        logger.info("\n\n## cross-posting videos to TikTok/Instagram")
        for video_path in final_video_paths:
            result = upload_post.cross_post_video(
                video_path=video_path,
                title=params.video_subject or "Check out this video! #shorts #viral"
            )
            cross_post_results.append(result)
            if result.get('success'):
                logger.info(f"✅ Cross-posted: {video_path}")
            else:
                logger.warning(f"⚠️ Failed to cross-post: {video_path} - {result.get('error', 'Unknown error')}")

    kwargs = {
        "videos": final_video_paths,
        "combined_videos": [],
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": [],
        "cross_post_results": cross_post_results if cross_post_results else None,
    }
    if matched_segments:
        metadata_segments = _strip_task_local_materials(task_id, matched_segments)
        kwargs["matched_segments"] = metadata_segments
        save_script_data(
            task_id,
            video_script,
            video_terms,
            params,
            matched_segments=metadata_segments,
        )
    _cleanup_final_only_artifacts(
        task_id,
        material_paths=downloaded_videos,
        combined_video_paths=combined_video_paths,
    )
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )
    return kwargs


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
