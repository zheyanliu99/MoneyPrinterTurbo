import math
import os.path
import re
from os import path

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams
from app.services import llm, material, subtitle, video, voice, upload_post
from app.services import state as sm
from app.utils import utils


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


def save_script_data(task_id, video_script, video_terms, params, matched_segments=None):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
    }
    if matched_segments is not None:
        script_data["matched_segments"] = matched_segments

    with open(script_file, "w", encoding="utf-8") as f:
        f.write(utils.to_json(script_data))


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


def start(task_id, params: VideoParams, stop_at: str = "video"):
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5)

    # 1. Generate script
    video_script = generate_script(task_id, params)
    if not video_script or "Error: " in video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)

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

    if stop_at == "subtitle":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
            matched_segments=matched_segments,
        )
        return {"subtitle_path": subtitle_path, "matched_segments": matched_segments}

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
        "combined_videos": combined_video_paths,
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": downloaded_videos,
        "cross_post_results": cross_post_results if cross_post_results else None,
    }
    if matched_segments:
        kwargs["matched_segments"] = matched_segments
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
