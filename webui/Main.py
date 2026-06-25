import os
import sys
import webbrowser
import html
import csv
import io
from uuid import UUID, uuid4

import requests
import streamlit as st
from loguru import logger

# Add the root directory of the project to the system path to allow importing modules from the project
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)
    print("******** sys.path ********")
    print(sys.path)
    print("")

from app.config import config
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import llm, preproduction, voice
from app.services import state as sm
from app.services import task as tm
from app.utils import utils

st.set_page_config(
    page_title="MoneyPrinterTurbo",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": "# MoneyPrinterTurbo\nSimply provide a topic or keyword for a video, and it will "
        "automatically generate the video copy, video materials, video subtitles, "
        "and video background music before synthesizing a high-definition short "
        "video.\n\nhttps://github.com/harry0703/MoneyPrinterTurbo",
    },
)


streamlit_style = """
<style>
h1 {
    padding-top: 0 !important;
}
</style>
"""
st.markdown(streamlit_style, unsafe_allow_html=True)

# 定义资源目录
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
system_locale = utils.get_system_locale()

DEFAULT_TEST_VIDEO_SUBJECT = "中国五大城市硬核盘点"
DEFAULT_TEST_VIDEO_SCRIPT = """第五 广州 常住人口约1898万人。
面积约7434平方公里。
2024年GDP约3.10万亿元。
千年商都广州把烟火气炼成硬实力。

第四 重庆 常住人口约3190万人。
面积约8.24万平方公里。
2024年GDP约3.21万亿元。
山城重庆把江河桥梁和万家灯火写成中国速度。

第三 深圳 常住人口约1779万人。
面积约1997平方公里。
2024年GDP约3.68万亿元。
年轻的深圳用科技资本和效率把奇迹变成日常。

第二 北京 常住人口约2186万人。
面积约1.64万平方公里。
2024年GDP约4.98万亿元。
首都北京把历史权力和创新压成一座世界级引擎。

第一 上海 常住人口约2487万人。
面积约6340平方公里。
2024年GDP约5.39万亿元。
东方之巅上海用金融航运和天际线定义中国高度。"""
DEFAULT_TEST_VIDEO_TERMS = (
    "广州城市天际线，广州城市航拍，广州CBD天际线，广州小蛮腰夜景，"
    "重庆城市天际线，重庆山城航拍，重庆长江大桥，重庆城市夜景，"
    "深圳城市天际线，深圳城市航拍，深圳科技城市，深圳福田天际线，"
    "北京城市天际线，北京城市航拍，北京CBD天际线，北京天安门城市，"
    "上海城市天际线，上海城市航拍，上海陆家嘴天际线，上海黄浦江天际线"
)
SIMPLE_CANDIDATE_PROVIDER_OPTIONS = ("pexels", "x")
SIMPLE_CANDIDATE_PROVIDER_LABELS = {
    "pexels": "Pexels",
    "x": "X",
}
STOCK_CANDIDATE_PROVIDERS = {"pexels", "pixabay", "coverr"}


def _default_preproduction_csv() -> str:
    script_lines = utils.split_script_to_visual_lines(DEFAULT_TEST_VIDEO_SCRIPT)
    display_terms = [
        term.strip()
        for term in DEFAULT_TEST_VIDEO_TERMS.split("，")
        if term.strip()
    ]
    rows = []
    for index, script_line in enumerate(script_lines, start=1):
        keyword = display_terms[index - 1] if index - 1 < len(display_terms) else ""
        rows.append(
            {
                "segment_index": index,
                "title": DEFAULT_TEST_VIDEO_SUBJECT,
                "script": script_line,
                "keyword_cn": keyword,
                "material_query": tm._translate_cjk_stock_search_term(keyword),
                "providers": "pexels",
                "source_urls": "",
                "duration_sec": "",
                "preferred_orientation": "",
                "notes": "",
            }
        )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=preproduction.DEFAULT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


if "video_subject" not in st.session_state:
    st.session_state["video_subject"] = DEFAULT_TEST_VIDEO_SUBJECT
if "video_script" not in st.session_state:
    st.session_state["video_script"] = DEFAULT_TEST_VIDEO_SCRIPT
if "video_terms" not in st.session_state:
    st.session_state["video_terms"] = DEFAULT_TEST_VIDEO_TERMS
if "video_script_prompt" not in st.session_state:
    st.session_state["video_script_prompt"] = ""
if "custom_system_prompt" not in st.session_state:
    st.session_state["custom_system_prompt"] = llm.DEFAULT_SCRIPT_SYSTEM_PROMPT
if "use_custom_system_prompt" not in st.session_state:
    st.session_state["use_custom_system_prompt"] = False
if "match_materials_to_script" not in st.session_state:
    st.session_state["match_materials_to_script"] = bool(
        config.app.get("match_materials_to_script", False)
    )
if "ui_language" not in st.session_state:
    st.session_state["ui_language"] = config.ui.get("language", system_locale)
if "local_video_materials" not in st.session_state:
    # 记住用户最近一次已经落盘的本地素材，避免仅修改文案后二次生成时丢失素材列表。
    st.session_state["local_video_materials"] = []
if "preproduction_csv_text" not in st.session_state:
    st.session_state["preproduction_csv_text"] = _default_preproduction_csv()
if "preproduction_rows" not in st.session_state:
    st.session_state["preproduction_rows"] = []
if "simple_candidate_providers" not in st.session_state:
    configured_providers = preproduction.normalize_provider_list(
        config.app.get("candidate_preview_providers", ["pexels"])
    )
    st.session_state["simple_candidate_providers"] = [
        provider
        for provider in configured_providers
        if provider in SIMPLE_CANDIDATE_PROVIDER_OPTIONS
    ] or ["pexels"]

# 加载语言文件
locales = utils.load_locales(i18n_dir)

# 创建一个顶部栏，包含标题和语言选择
title_col, lang_col = st.columns([3, 1])

with title_col:
    st.title(f"MoneyPrinterTurbo v{config.project_version}")

with lang_col:
    display_languages = []
    selected_index = 0
    for i, code in enumerate(locales.keys()):
        display_languages.append(f"{code} - {locales[code].get('Language')}")
        if code == st.session_state.get("ui_language", ""):
            selected_index = i

    selected_language = st.selectbox(
        "Language / 语言",
        options=display_languages,
        index=selected_index,
        key="top_language_selector",
        label_visibility="collapsed",
    )
    if selected_language:
        code = selected_language.split(" - ")[0].strip()
        st.session_state["ui_language"] = code
        config.ui["language"] = code

support_locales = [
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "fr-FR",
    "ru-RU",
    "vi-VN",
    "th-TH",
    "tr-TR",
]


def get_all_fonts():
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                fonts.append(file)
    fonts.sort()
    return fonts


def get_all_songs():
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        # task_id 应始终是服务端生成的 UUID。这里先做格式校验，避免异常值
        # 通过路径拼接访问任务目录之外的位置，也避免后续打开目录时触发
        # 平台 shell 对特殊字符的解释。
        normalized_task_id = str(UUID(str(task_id)))
        tasks_root = os.path.abspath(os.path.join(root_dir, "storage", "tasks"))
        path = os.path.abspath(os.path.join(tasks_root, normalized_task_id))

        # 即使 UUID 校验通过，也再次确认最终路径仍在任务根目录内，避免
        # 未来调用方调整 task_id 来源时引入路径穿越风险。
        if not path.startswith(tasks_root + os.sep):
            logger.warning(f"invalid task folder path: {path}")
            return

        if os.path.isdir(path):
            webbrowser.open(f"file://{path}")
    except Exception as e:
        logger.error(e)


def scroll_to_bottom():
    js = """
    <script>
        console.log("scroll_to_bottom");
        function scroll(dummy_var_to_force_repeat_execution){
            var sections = parent.document.querySelectorAll('section.main');
            console.log(sections);
            for(let index = 0; index<sections.length; index++) {
                sections[index].scrollTop = sections[index].scrollHeight;
            }
        }
        scroll(1);
    </script>
    """
    st.components.v1.html(js, height=0, width=0)


def init_log():
    logger.remove()
    _lvl = "DEBUG"

    def format_record(record):
        # 获取日志记录中的文件全路径
        file_path = record["file"].path
        # 将绝对路径转换为相对于项目根目录的路径
        relative_path = os.path.relpath(file_path, root_dir)
        # 更新记录中的文件路径
        record["file"].path = f"./{relative_path}"
        # 返回修改后的格式字符串
        # 您可以根据需要调整这里的格式
        record["message"] = record["message"].replace(root_dir, ".")

        _format = (
            "<green>{time:%Y-%m-%d %H:%M:%S}</> | "
            + "<level>{level}</> | "
            + '"{file.path}:{line}":<blue> {function}</> '
            + "- <level>{message}</>"
            + "\n"
        )
        return _format

    logger.add(
        sys.stdout,
        level=_lvl,
        format=format_record,
        colorize=True,
    )


init_log()

locales = utils.load_locales(i18n_dir)


def tr(key):
    loc = locales.get(st.session_state["ui_language"], {})
    return loc.get("Translation", {}).get(key, key)

@st.cache_data(ttl=300, show_spinner=False)
def get_groq_model_ids(api_key: str, base_url: str) -> list[str]:
    if not api_key:
        return []

    normalized_base_url = (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
    models_url = f"{normalized_base_url}/models"

    try:
        response = requests.get(
            models_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", [])

        model_ids = []
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    model_ids.append(model_id.strip())

        return sorted(set(model_ids))
    except Exception as e:
        logger.warning(f"failed to fetch groq models: {e}")
        return []


def _as_list_config_value(key: str) -> list:
    value = config.app.get(key, [])
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _voice_rate_from_percent(percent: int | float) -> float:
    try:
        numeric_percent = float(percent)
    except (TypeError, ValueError):
        numeric_percent = 0.0
    numeric_percent = min(max(numeric_percent, -50.0), 50.0)
    return round(1.0 + numeric_percent / 100.0, 2)


def _voice_rate_percent_from_rate(rate: int | float | str) -> int:
    try:
        numeric_rate = float(rate)
    except (TypeError, ValueError):
        numeric_rate = 1.0
    numeric_rate = min(max(numeric_rate, 0.5), 1.5)
    return int(round((numeric_rate - 1.0) * 100))


def _simple_voice_rate_value() -> float:
    if "simple_voice_rate_percent" in st.session_state:
        return _voice_rate_from_percent(st.session_state["simple_voice_rate_percent"])
    return _voice_rate_from_percent(
        _voice_rate_percent_from_rate(st.session_state.get("simple_voice_rate", 1.0))
    )


def _format_voice_rate_percent(percent: int | float) -> str:
    try:
        numeric_percent = int(round(float(percent)))
    except (TypeError, ValueError):
        numeric_percent = 0
    return f"{numeric_percent:+d}%"


def _render_voice_rate_control():
    if "simple_voice_rate_percent" not in st.session_state:
        st.session_state["simple_voice_rate_percent"] = _voice_rate_percent_from_rate(
            st.session_state.get("simple_voice_rate", 1.0)
        )

    voice_rate_percent = st.slider(
        "配音语速",
        min_value=-50,
        max_value=50,
        step=5,
        format="%d%%",
        key="simple_voice_rate_percent",
        help="负数会放慢配音，正数会加快配音。每句 clip 时长会跟随配音重新计算，但视频素材不会变速。",
    )
    voice_rate = _voice_rate_from_percent(voice_rate_percent)
    st.session_state["simple_voice_rate"] = voice_rate
    st.caption(
        f"当前语速：{_format_voice_rate_percent(voice_rate_percent)} "
        f"({voice_rate:.2f}x)。候选时长和最终字幕会按配音重算，视频素材不变速。"
    )


def _preproduction_editor_rows(rows: list[dict]) -> list[dict]:
    editor_rows = []
    for row in preproduction.normalize_preproduction_rows(rows):
        editor_rows.append(
            {
                "segment_index": row["segment_index"],
                "script": row["script"],
                "keyword_cn": row["keyword_cn"],
                "material_query": row["material_query"],
                "providers": ", ".join(row["providers"]),
                "source_urls": "\n".join(row["source_urls"]),
                "duration_sec": row["duration_sec"] or "",
                "preferred_orientation": row["preferred_orientation"],
                "notes": row["notes"],
            }
        )
    return editor_rows


def _preproduction_rows_from_editor(rows) -> list[dict]:
    normalized = []
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    if not isinstance(rows, list):
        return normalized
    for row in rows:
        row = dict(row)
        row["providers"] = preproduction.normalize_provider_list(row.get("providers"))
        row["source_urls"] = preproduction.normalize_source_urls(row.get("source_urls"))
        normalized.append(row)
    return preproduction.normalize_preproduction_rows(normalized)


def _sync_preproduction_rows_to_inputs(rows: list[dict]):
    rows = preproduction.normalize_preproduction_rows(rows)
    if not rows:
        return
    plan = preproduction.build_preproduction_plan(
        rows,
        title=st.session_state.get("video_subject", ""),
    )
    if plan.get("title"):
        st.session_state["video_subject"] = plan["title"]
    st.session_state["video_script"] = plan.get("script", "")
    st.session_state["video_terms"] = "，".join(plan.get("display_terms") or [])
    st.session_state["script_keyword_matches"] = [
        {
            "index": row["segment_index"],
            "text": row["script"],
            "term": row["keyword_cn"],
            "score": "",
            "reason": "CSV 预生产计划",
        }
        for row in rows
    ]
    st.session_state.pop("script_keyword_match_signature", None)


def _preproduction_plan_from_session() -> dict | None:
    rows = preproduction.normalize_preproduction_rows(
        st.session_state.get("preproduction_rows") or []
    )
    if not rows:
        return None
    return preproduction.build_preproduction_plan(
        rows,
        title=st.session_state.get("video_subject", ""),
    )


def _candidate_provider_label(provider: str) -> str:
    provider_name = str(provider or "").strip().lower()
    return SIMPLE_CANDIDATE_PROVIDER_LABELS.get(provider_name, provider_name.upper())


def _selected_simple_candidate_providers() -> list[str]:
    providers = preproduction.normalize_provider_list(
        st.session_state.get("simple_candidate_providers") or ["pexels"]
    )
    providers = [
        provider
        for provider in providers
        if provider in SIMPLE_CANDIDATE_PROVIDER_OPTIONS
    ]
    return providers or ["pexels"]


def _simple_material_query_for_keyword(keyword: str, providers: list[str]) -> str:
    keyword = str(keyword or "").strip()
    if any(provider in STOCK_CANDIDATE_PROVIDERS for provider in providers):
        return tm._translate_cjk_stock_search_term(keyword)
    return keyword


def _implicit_preproduction_plan_from_keyword_matches() -> dict | None:
    explicit_plan = _preproduction_plan_from_session()
    if explicit_plan:
        return explicit_plan

    providers = _selected_simple_candidate_providers()
    if providers == ["pexels"]:
        return None

    script = st.session_state.get("video_script", "").strip()
    script_lines = utils.split_script_to_visual_lines(script)
    if not script_lines:
        return None

    rows = _normalize_keyword_match_rows(
        st.session_state.get("script_keyword_matches") or []
    )
    if len(rows) != len(script_lines):
        rows = tm.build_script_keyword_matches(
            video_script=script,
            video_terms=st.session_state.get("video_terms", ""),
            video_subject=st.session_state.get("video_subject", ""),
        )

    rows = _normalize_keyword_match_rows(rows)
    if len(rows) != len(script_lines):
        return None

    plan_rows = []
    for index, script_line in enumerate(script_lines, start=1):
        match_row = rows[index - 1]
        keyword = str(match_row.get("term") or script_line).strip()
        plan_rows.append(
            {
                "segment_index": index,
                "title": st.session_state.get("video_subject", ""),
                "script": script_line,
                "keyword_cn": keyword,
                "material_query": _simple_material_query_for_keyword(
                    keyword, providers
                ),
                "providers": providers,
                "source_urls": [],
                "duration_sec": 0.0,
                "preferred_orientation": "",
                "notes": "普通流程素材库选择",
            }
        )

    return preproduction.build_preproduction_plan(
        plan_rows,
        title=st.session_state.get("video_subject", ""),
    )


def _providers_required_for_prepare() -> set[str]:
    plan = _preproduction_plan_from_session()
    if not plan:
        return set(_selected_simple_candidate_providers()) or {"pexels"}
    providers = set()
    for row in plan.get("segments") or []:
        providers.update(preproduction.normalize_provider_list(row.get("providers")))
        if preproduction.normalize_source_urls(row.get("source_urls")):
            providers.add("url")
    return providers or {"pexels"}


def _build_editor_params() -> VideoParams:
    matched_terms = _matched_terms_from_session()
    preproduction_plan = _implicit_preproduction_plan_from_keyword_matches()
    if preproduction_plan:
        video_terms = preproduction_plan.get("display_terms") or matched_terms
    else:
        video_terms = matched_terms or st.session_state.get("video_terms", "").strip()
    params = VideoParams(
        video_subject=st.session_state.get("video_subject", "").strip(),
        video_script=st.session_state.get("video_script", "").strip(),
        video_terms=video_terms,
        video_source="pexels",
        video_aspect=st.session_state.get(
            "simple_video_aspect", VideoAspect.portrait.value
        ),
        video_concat_mode=VideoConcatMode.sequential.value,
        video_transition_mode=None,
        video_clip_duration=int(st.session_state.get("simple_clip_duration", 5)),
        match_materials_to_script=True,
        video_count=1,
        voice_name=st.session_state.get(
            "simple_voice_name",
            config.ui.get("voice_name", "en-AU-NatashaNeural-Female"),
        ),
        voice_rate=_simple_voice_rate_value(),
        voice_volume=1.0,
        bgm_type="",
        bgm_file="",
        bgm_volume=0.0,
        subtitle_enabled=True,
        subtitle_position=st.session_state.get("simple_subtitle_position", "bottom"),
        font_name=st.session_state.get("simple_font_name", "STHeitiMedium.ttc"),
        text_fore_color=st.session_state.get("simple_text_fore_color", "#FFFFFF"),
        text_background_color=st.session_state.get(
            "simple_text_background_color", True
        ),
        rounded_subtitle_background=bool(
            st.session_state.get("simple_rounded_subtitle_background", False)
        ),
        font_size=int(st.session_state.get("simple_font_size", 60)),
        stroke_color=st.session_state.get("simple_stroke_color", "#000000"),
        stroke_width=float(st.session_state.get("simple_stroke_width", 1.5)),
        n_threads=int(st.session_state.get("simple_n_threads", 2)),
        paragraph_number=1,
        video_script_prompt=st.session_state.get("video_script_prompt", ""),
        custom_system_prompt=st.session_state.get("custom_system_prompt", ""),
        preproduction_plan=preproduction_plan,
    )
    return params


def _keyword_match_signature(script: str, terms: str, subject: str) -> str:
    return utils.md5(f"{subject}\n---script---\n{script}\n---terms---\n{terms}")


def _normalize_keyword_match_rows(rows) -> list[dict]:
    normalized_rows = []
    if not isinstance(rows, list):
        return normalized_rows
    for row in rows:
        if not isinstance(row, dict):
            continue
        index = int(row.get("index") or len(normalized_rows) + 1)
        text = str(row.get("text") or "").strip()
        term = str(row.get("term") or row.get("keyword") or "").strip()
        normalized_rows.append(
            {
                "index": index,
                "text": text,
                "term": term,
                "score": row.get("score", ""),
                "reason": str(row.get("reason") or ""),
            }
        )
    return normalized_rows


def _matched_terms_from_session() -> list[str]:
    rows = _normalize_keyword_match_rows(
        st.session_state.get("script_keyword_matches") or []
    )
    script_lines = utils.split_script_to_visual_lines(
        st.session_state.get("video_script", "")
    )
    if not rows or len(rows) != len(script_lines):
        return []
    terms = [row["term"] for row in rows if row.get("term")]
    return terms if len(terms) == len(script_lines) else []


def _render_script_keyword_match_editor(force_refresh: bool = False):
    script = st.session_state.get("video_script", "").strip()
    if not script:
        return

    script_lines = utils.split_script_to_visual_lines(script)
    if not script_lines:
        return

    terms_text = st.session_state.get("video_terms", "").strip()
    subject = st.session_state.get("video_subject", "").strip()
    signature = _keyword_match_signature(script, terms_text, subject)
    if force_refresh or st.session_state.get("script_keyword_match_signature") != signature:
        st.session_state["script_keyword_matches"] = tm.build_script_keyword_matches(
            video_script=script,
            video_terms=terms_text,
            video_subject=subject,
        )
        st.session_state["script_keyword_match_signature"] = signature

    rows = _normalize_keyword_match_rows(
        st.session_state.get("script_keyword_matches") or []
    )
    st.subheader("脚本关键词匹配")
    st.caption("先确认每句脚本对应的搜索关键词；这里改完后，再准备候选素材。")
    edited_rows = st.data_editor(
        rows,
        hide_index=True,
        width="stretch",
        num_rows="fixed",
        column_order=("index", "text", "term", "reason"),
        disabled=("index", "text", "reason"),
        key=f"keyword_match_editor_{signature}",
        column_config={
            "index": st.column_config.NumberColumn("句", width="small"),
            "text": st.column_config.TextColumn("脚本句子", width="large"),
            "term": st.column_config.TextColumn("匹配关键词", width="medium"),
            "reason": st.column_config.TextColumn("匹配方式", width="medium"),
        },
    )
    st.session_state["script_keyword_matches"] = _normalize_keyword_match_rows(
        edited_rows
    )
    matched_terms = _matched_terms_from_session()
    if matched_terms:
        st.caption(f"将按 {len(matched_terms)} 个逐句关键词搜索候选素材。")
    else:
        st.warning("请为每一句脚本填写一个关键词，之后再准备候选素材。")


def _show_video_preview(video_path: str, caption: str = ""):
    if not video_path:
        st.caption(caption or "No preview")
        return
    try:
        if os.path.exists(video_path):
            st.video(video_path)
        else:
            st.video(video_path)
        if caption:
            st.caption(caption)
    except Exception as e:
        st.caption(caption or video_path)
        logger.warning(f"failed to render video preview: {video_path}, error: {e}")


def _build_progress_updater(initial_message: str):
    progress_bar = st.progress(0)
    progress_text = st.empty()

    def update(progress: float, message: str):
        progress_value = max(0.0, min(float(progress or 0.0), 1.0))
        progress_bar.progress(progress_value)
        progress_text.caption(f"{int(progress_value * 100):02d}% · {message}")

    update(0.0, initial_message)
    return update


def _candidate_preview_caption(candidate: dict) -> str:
    orientation_label = candidate.get("orientation_label") or ""
    group_rank = candidate.get("group_rank") or candidate.get("rank")
    provider = _candidate_provider_label(candidate.get("provider") or "")
    parts = [f"{orientation_label}候选 {group_rank}".strip()]
    if provider:
        parts.append(provider)
    score = candidate.get("score")
    if score is not None:
        try:
            parts.append(f"评分 {float(score):.0f}")
        except (TypeError, ValueError):
            pass
    width = int(candidate.get("width") or 0)
    height = int(candidate.get("height") or 0)
    if width and height:
        parts.append(f"{width}x{height}")
    if candidate.get("fallback"):
        parts.append("fallback")
    attribution = candidate.get("attribution") or candidate.get("author") or ""
    if attribution:
        parts.append(str(attribution))
    return " · ".join(parts)


def _render_candidate_preview_grid(candidates: list[dict]):
    if not candidates:
        return
    for start in range(0, len(candidates), 3):
        row_candidates = candidates[start:start + 3]
        preview_cols = st.columns(min(3, len(row_candidates)))
        for preview_col, candidate in zip(preview_cols, row_candidates):
            with preview_col:
                _show_video_preview(
                    candidate.get("preview_url")
                    or candidate.get("source_url")
                    or candidate.get("material", ""),
                    caption=_candidate_preview_caption(candidate),
                )
                if candidate.get("reason"):
                    st.caption(candidate["reason"])


def _render_candidate_preview_group(candidates: list[dict]):
    if not candidates:
        return
    provider_order = []
    for candidate in candidates:
        provider = str(candidate.get("provider") or "pexels").strip().lower()
        if provider and provider not in provider_order:
            provider_order.append(provider)

    if len(provider_order) <= 1:
        _render_candidate_preview_grid(candidates)
        return

    tabs = st.tabs(
        [
            f"{_candidate_provider_label(provider)} ({sum(1 for item in candidates if str(item.get('provider') or 'pexels').strip().lower() == provider)})"
            for provider in provider_order
        ]
    )
    for tab, provider in zip(tabs, provider_order):
        with tab:
            _render_candidate_preview_grid(
                [
                    candidate
                    for candidate in candidates
                    if str(candidate.get("provider") or "pexels").strip().lower()
                    == provider
                ]
            )


def _truncate_clip_text(text: str, max_chars: int = 36) -> str:
    normalized = str(text or "").replace("\n", " ").strip()
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[:max_chars].rstrip()}..."


def _segment_display_term(
    segment: dict, display_terms: list[str], segment_index: int
) -> str:
    term = (
        segment.get("display_term")
        or (
            display_terms[segment_index - 1]
            if 0 <= segment_index - 1 < len(display_terms)
            else ""
        )
        or segment.get("term")
        or ""
    )
    return str(term).strip()


def _selected_candidate_for_segment(
    candidates: list[dict], selected_candidate_id: str
) -> dict:
    return next(
        (
            candidate
            for candidate in candidates
            if candidate.get("candidate_id") == selected_candidate_id
        ),
        candidates[0] if candidates else {},
    )


def _timeline_thumbnail_url(segment: dict, selected_candidate: dict) -> str:
    candidates = segment.get("candidates") or []
    first_candidate = candidates[0] if candidates else {}
    return str(
        selected_candidate.get("thumbnail_url")
        or first_candidate.get("thumbnail_url")
        or segment.get("thumbnail_url")
        or ""
    ).strip()


def _render_timeline_thumbnail(thumbnail_url: str, fallback_text: str):
    safe_text = html.escape(_truncate_clip_text(fallback_text or "无缩略图", 16))
    if thumbnail_url:
        safe_url = html.escape(thumbnail_url, quote=True)
        st.markdown(
            f"""
            <div class="candidate-timeline-thumb">
                <img src="{safe_url}" alt="clip thumbnail" />
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        f"""
        <div class="candidate-timeline-thumb candidate-timeline-placeholder">
            <span>{safe_text}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_candidate_timeline_styles():
    st.markdown(
        """
        <style>
        div[data-testid="stHorizontalBlock"]:has(.candidate-timeline-card) {
            overflow-x: auto;
            flex-wrap: nowrap;
            padding: 0.2rem 0 0.6rem;
            scrollbar-width: thin;
        }
        div[data-testid="stHorizontalBlock"]:has(.candidate-timeline-card) > div {
            flex: 0 0 220px !important;
            width: 220px !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.candidate-timeline-active) {
            border-color: #ff4b4b !important;
            box-shadow: 0 0 0 1px rgba(255, 75, 75, 0.35);
        }
        .candidate-timeline-thumb {
            width: 100%;
            height: 112px;
            border-radius: 6px;
            overflow: hidden;
            background: #20242d;
            border: 1px solid rgba(250, 250, 250, 0.12);
            display: flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 0.4rem;
        }
        .candidate-timeline-thumb img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }
        .candidate-timeline-placeholder {
            color: rgba(250, 250, 250, 0.72);
            font-size: 0.82rem;
            line-height: 1.25;
            text-align: center;
            padding: 0.45rem;
        }
        .candidate-timeline-meta {
            color: rgba(250, 250, 250, 0.72);
            font-size: 0.78rem;
            line-height: 1.25;
            margin: 0.1rem 0;
        }
        .candidate-timeline-term,
        .candidate-timeline-script {
            color: rgba(250, 250, 250, 0.78);
            font-size: 0.86rem;
            font-weight: 600;
            line-height: 1.35;
            overflow: hidden;
            display: -webkit-box;
            -webkit-box-orient: vertical;
            word-break: break-word;
        }
        .candidate-timeline-term {
            height: 2.35rem;
            -webkit-line-clamp: 2;
            margin: 0.25rem 0 0.45rem;
        }
        .candidate-timeline-script {
            height: 3.55rem;
            -webkit-line-clamp: 3;
            margin-bottom: 0.75rem;
        }
        .candidate-timeline-label {
            color: rgba(250, 250, 250, 0.58);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_candidate_timeline_text(label: str, value: str, css_class: str):
    safe_label = html.escape(label)
    safe_value = html.escape(str(value or ""))
    st.markdown(
        f"""
        <div class="{css_class}">
            <span class="candidate-timeline-label">{safe_label}</span>{safe_value}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _load_candidate_task(task_id: str):
    if not task_id:
        return None
    return sm.state.get_task(task_id)


def _render_candidate_editor(task_data: dict):
    matched_segments = task_data.get("matched_segments") or []
    if not matched_segments:
        st.info("还没有候选素材。先点击“准备候选素材”。")
        return
    display_terms = tm._normalize_video_terms(
        (task_data.get("params") or {}).get("video_terms")
    )
    if isinstance(display_terms, str):
        display_terms = []

    st.subheader("逐句候选素材")
    st.caption("所有 clips 按时间轴横向排列；点击任意片段后，在下方预览、选择候选视频并调整裁剪。")
    _render_candidate_timeline_styles()

    editor_task_id = str(
        task_data.get("task_id") or st.session_state.get("candidate_task_id") or ""
    )
    if st.session_state.get("candidate_edit_task_id") != editor_task_id:
        st.session_state["candidate_edit_task_id"] = editor_task_id
        st.session_state["candidate_edit_state"] = {}
    edit_state = st.session_state.setdefault("candidate_edit_state", {})

    segment_by_index = {
        int(segment.get("index") or index + 1): segment
        for index, segment in enumerate(matched_segments)
    }
    segment_states = {}

    for index, segment in enumerate(matched_segments):
        segment_index = int(segment.get("index") or index + 1)
        candidates = segment.get("candidates") or []
        if not candidates:
            continue

        candidate_ids = [
            candidate.get("candidate_id", "") for candidate in candidates
            if candidate.get("candidate_id")
        ]
        if not candidate_ids:
            continue

        default_candidate_id = candidate_ids[0]
        choice_key = f"candidate_choice_{segment_index}"
        choice_widget_key = f"candidate_choice_widget_{segment_index}"
        segment_edit = edit_state.setdefault(str(segment_index), {})
        if st.session_state.get(choice_widget_key) in candidate_ids:
            segment_edit["candidate_id"] = st.session_state[choice_widget_key]
        elif st.session_state.get(choice_key) in candidate_ids:
            segment_edit["candidate_id"] = st.session_state[choice_key]
        elif segment_edit.get("candidate_id") in candidate_ids:
            pass
        else:
            segment_edit["candidate_id"] = default_candidate_id
        st.session_state[choice_key] = segment_edit["candidate_id"]

        selected_candidate_id = (
            segment_edit.get("candidate_id")
            or st.session_state.get(choice_key)
            or default_candidate_id
        )
        selected_candidate = _selected_candidate_for_segment(
            candidates, selected_candidate_id
        )
        candidate_duration = float(selected_candidate.get("duration") or 0.0)
        trim_start_key = f"trim_start_{segment_index}"
        trim_end_key = f"trim_end_{segment_index}"
        text_key = f"segment_text_{segment_index}"
        max_trim_start = (
            max(candidate_duration - 0.1, 0.0)
            if candidate_duration > 0.1
            else None
        )
        if trim_start_key in st.session_state:
            segment_edit["trim_start"] = st.session_state[trim_start_key]
        if trim_end_key in st.session_state:
            segment_edit["trim_end"] = st.session_state[trim_end_key]
        if text_key in st.session_state:
            segment_edit["text"] = st.session_state[text_key]

        current_trim_start = max(
            float(segment_edit.get("trim_start", 0.0) or 0.0), 0.0
        )
        if max_trim_start is not None:
            current_trim_start = min(current_trim_start, max_trim_start)
        current_trim_end = float(
            segment_edit.get(
                "trim_end",
                candidate_duration
                or max(float(segment.get("duration") or 1.0), 1.0),
            )
        )
        min_trim_end = current_trim_start + 0.1
        if candidate_duration > 0:
            current_trim_end = min(current_trim_end, candidate_duration)
        current_trim_end = max(current_trim_end, min_trim_end)
        segment_edit["trim_start"] = current_trim_start
        segment_edit["trim_end"] = current_trim_end
        segment_edit["text"] = segment_edit.get("text", segment.get("text", ""))
        st.session_state[trim_start_key] = current_trim_start
        st.session_state[trim_end_key] = current_trim_end
        st.session_state[text_key] = segment_edit["text"]

        segment_states[segment_index] = {
            "candidates": candidates,
            "candidate_ids": candidate_ids,
            "edit_state": segment_edit,
            "choice_key": choice_key,
            "choice_widget_key": choice_widget_key,
            "trim_start_key": trim_start_key,
            "trim_end_key": trim_end_key,
            "text_key": text_key,
            "selected_candidate": selected_candidate,
            "default_candidate_id": default_candidate_id,
            "default_trim_start": current_trim_start,
            "default_trim_end": current_trim_end,
            "default_text": segment.get("text", ""),
        }

    if not segment_states:
        st.error("没有可用候选素材。")
        return

    active_segment_index = int(
        st.session_state.get("active_candidate_segment_index")
        or next(iter(segment_states))
    )
    if active_segment_index not in segment_states:
        active_segment_index = next(iter(segment_states))
        st.session_state["active_candidate_segment_index"] = active_segment_index

    st.caption(f"已准备 {len(matched_segments)} 句；时间轴只加载缩略图，选中段落才加载视频预览。")
    with st.container(
        horizontal=True,
        horizontal_alignment="left",
        vertical_alignment="top",
        gap="small",
    ):
        for segment_index, segment in segment_by_index.items():
            state = segment_states.get(segment_index)
            visible_term = _segment_display_term(segment, display_terms, segment_index)
            is_active = segment_index == active_segment_index

            with st.container(border=True, width=220):
                marker_class = "candidate-timeline-card"
                if is_active:
                    marker_class += " candidate-timeline-active"
                st.markdown(
                    f'<span class="{marker_class}"></span>',
                    unsafe_allow_html=True,
                )

                if state:
                    thumbnail_url = _timeline_thumbnail_url(
                        segment, state["selected_candidate"]
                    )
                else:
                    thumbnail_url = ""
                _render_timeline_thumbnail(thumbnail_url, visible_term)

                st.markdown(
                    f'<div class="candidate-timeline-meta">第 {segment_index} 段 · '
                    f'{float(segment.get("duration") or 0):.2f}s</div>',
                    unsafe_allow_html=True,
                )
                _render_candidate_timeline_text(
                    "关键词：",
                    _truncate_clip_text(visible_term, 20),
                    "candidate-timeline-term",
                )
                _render_candidate_timeline_text(
                    "脚本：",
                    _truncate_clip_text(segment.get("text", ""), 36),
                    "candidate-timeline-script",
                )

                if state:
                    button_label = (
                        f"正在编辑第 {segment_index} 段"
                        if is_active
                        else f"编辑第 {segment_index} 段"
                    )
                    if st.button(
                        button_label,
                        key=f"timeline_select_{segment_index}",
                        type="primary" if is_active else "secondary",
                        width="stretch",
                    ):
                        if not is_active:
                            st.session_state[
                                "active_candidate_segment_index"
                            ] = segment_index
                            st.rerun()
                else:
                    st.button(
                        "无候选素材",
                        key=f"timeline_missing_{segment_index}",
                        disabled=True,
                        width="stretch",
                    )

    active_segment = segment_by_index.get(active_segment_index) or {}
    active_state = segment_states[active_segment_index]
    candidates = active_state["candidates"]
    candidate_ids = active_state["candidate_ids"]
    choice_key = active_state["choice_key"]
    choice_widget_key = active_state["choice_widget_key"]
    trim_start_key = active_state["trim_start_key"]
    trim_end_key = active_state["trim_end_key"]
    text_key = active_state["text_key"]
    default_candidate_id = active_state["default_candidate_id"]
    visible_term = _segment_display_term(
        active_segment, display_terms, active_segment_index
    )
    material_query = str(active_segment.get("term") or "").strip()
    providers_label = ", ".join(
        preproduction.normalize_provider_list(active_segment.get("providers"))
    )

    with st.container(border=True):
        st.markdown(
            f"**{active_segment_index}. {active_segment.get('text', '')}**  \n"
            f"关键词：`{visible_term}` · 当前时长："
            f"{float(active_segment.get('duration') or 0):.2f}s  \n"
            f"素材搜索词：`{material_query}` · 素材库：`{providers_label}`"
        )
        requested_providers = preproduction.normalize_provider_list(
            active_segment.get("providers")
        )
        available_providers = {
            str(candidate.get("provider") or "").strip().lower()
            for candidate in candidates
        }
        if "x" in requested_providers and "x" not in available_providers:
            st.warning(
                "这一句没有返回可预览的 X 视频候选。可以换更贴近 X 的搜索词，"
                "或者在 CSV/预生产计划里粘贴具体 x.com 推文链接。"
            )

        default_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("is_default_group")
        ]
        other_candidates = [
            candidate
            for candidate in candidates
            if not candidate.get("is_default_group")
        ]
        if not default_candidates:
            default_candidates = candidates[:3]
            other_candidates = candidates[3:]

        _render_candidate_preview_group(default_candidates)
        if other_candidates:
            other_label = other_candidates[0].get("orientation_label") or "其他"
            with st.expander(f"其他{other_label}候选", expanded=False):
                _render_candidate_preview_group(other_candidates)

        selected_candidate_id = st.selectbox(
            "选择候选视频",
            options=candidate_ids,
            index=candidate_ids.index(active_state["edit_state"]["candidate_id"]),
            key=choice_widget_key,
            format_func=lambda candidate_id, items=candidates: next(
                (
                    _candidate_preview_caption(item)
                    for item in items
                    if item.get("candidate_id") == candidate_id
                ),
                candidate_id,
            ),
        ) or default_candidate_id
        active_state["edit_state"]["candidate_id"] = selected_candidate_id
        st.session_state[choice_key] = selected_candidate_id

        selected_candidate = _selected_candidate_for_segment(
            candidates, selected_candidate_id
        )
        candidate_duration = float(selected_candidate.get("duration") or 0.0)
        max_trim_start = (
            max(candidate_duration - 0.1, 0.0)
            if candidate_duration > 0.1
            else None
        )
        current_trim_start = min(
            max(float(st.session_state.get(trim_start_key, 0.0)), 0.0),
            max_trim_start if max_trim_start is not None else float("inf"),
        )
        current_trim_end = float(
            st.session_state.get(
                trim_end_key,
                candidate_duration
                or max(float(active_segment.get("duration") or 1.0), 1.0),
            )
        )
        min_trim_end = current_trim_start + 0.1
        if candidate_duration > 0:
            current_trim_end = min(current_trim_end, candidate_duration)
        current_trim_end = max(current_trim_end, min_trim_end)
        st.session_state[trim_start_key] = current_trim_start
        st.session_state[trim_end_key] = current_trim_end

        trim_cols = st.columns(2)
        with trim_cols[0]:
            trim_start_kwargs = {
                "min_value": 0.0,
                "max_value": max_trim_start,
                "step": 0.1,
                "key": trim_start_key,
            }
            current_trim_start = st.number_input("裁剪开始秒", **trim_start_kwargs)
        with trim_cols[1]:
            trim_end_kwargs = {
                "min_value": float(current_trim_start) + 0.1,
                "max_value": candidate_duration if candidate_duration > 0 else None,
                "step": 0.1,
                "key": trim_end_key,
            }
            current_trim_end = st.number_input("裁剪结束秒", **trim_end_kwargs)

        text_area_kwargs = {"height": 90, "key": text_key}
        edited_text = st.text_area("单句字幕 / 配音文本", **text_area_kwargs)
        active_state["edit_state"]["trim_start"] = float(current_trim_start)
        active_state["edit_state"]["trim_end"] = float(current_trim_end)
        active_state["edit_state"]["text"] = edited_text

    selections = []
    for segment_index, state in segment_states.items():
        segment_edit = state["edit_state"]
        selections.append(
            {
                "segment_index": segment_index,
                "candidate_id": segment_edit.get("candidate_id")
                or state["default_candidate_id"],
                "trim_start": float(segment_edit.get("trim_start", 0.0)),
                "trim_end": float(segment_edit.get("trim_end", 0.0)),
                "text": segment_edit.get("text", state["default_text"]),
            }
        )

    st.session_state["candidate_selections"] = selections


def _render_preproduction_editor():
    if "preproduction_csv_text_next" in st.session_state:
        st.session_state["preproduction_csv_text"] = st.session_state.pop(
            "preproduction_csv_text_next"
        )
    rows = preproduction.normalize_preproduction_rows(
        st.session_state.get("preproduction_rows") or []
    )
    with st.expander("CSV / 预生产计划", expanded=bool(rows)):
        st.caption(
            "可上传或粘贴 CSV。这里是进入剪辑前的计划表：每段脚本、中文关键词、素材搜索词和素材库会先确认好。"
        )
        uploaded_csv = st.file_uploader(
            "上传 CSV / TSV",
            type=["csv", "tsv", "txt"],
            key="preproduction_csv_upload",
        )
        st.text_area(
            "粘贴 CSV",
            key="preproduction_csv_text",
            height=150,
            help="字段：segment_index,title,script,keyword_cn,material_query,providers,source_urls,duration_sec,preferred_orientation,notes",
        )
        parse_cols = st.columns(3)
        with parse_cols[0]:
            parse_clicked = st.button("解析为预生产计划", width="stretch")
        with parse_cols[1]:
            default_clicked = st.button("填入默认 CSV", width="stretch")
        with parse_cols[2]:
            clear_clicked = st.button("清除预生产计划", width="stretch")

        if default_clicked:
            st.session_state["preproduction_csv_text_next"] = _default_preproduction_csv()
            st.session_state["preproduction_rows"] = preproduction.parse_preproduction_csv(
                st.session_state["preproduction_csv_text_next"]
            )
            _sync_preproduction_rows_to_inputs(st.session_state["preproduction_rows"])
            st.rerun()

        if clear_clicked:
            st.session_state["preproduction_rows"] = []
            st.rerun()

        if parse_clicked:
            csv_text = ""
            if uploaded_csv is not None:
                csv_text = uploaded_csv.getvalue().decode("utf-8-sig", errors="replace")
            else:
                csv_text = st.session_state.get("preproduction_csv_text", "")
            parsed_rows = preproduction.parse_preproduction_csv(csv_text)
            if parsed_rows:
                st.session_state["preproduction_rows"] = parsed_rows
                _sync_preproduction_rows_to_inputs(parsed_rows)
                st.success(f"已解析 {len(parsed_rows)} 段预生产计划。")
                st.rerun()
            else:
                st.warning("没有解析到有效行，请检查 CSV 表头和内容。")

        rows = preproduction.normalize_preproduction_rows(
            st.session_state.get("preproduction_rows") or []
        )
        if not rows:
            return

        signature = preproduction.build_preproduction_plan(rows).get("signature", "")
        edited_rows = st.data_editor(
            _preproduction_editor_rows(rows),
            hide_index=True,
            width="stretch",
            num_rows="dynamic",
            key=f"preproduction_plan_editor_{signature}",
            column_config={
                "segment_index": st.column_config.NumberColumn("段", width="small"),
                "script": st.column_config.TextColumn("脚本", width="large"),
                "keyword_cn": st.column_config.TextColumn("中文关键词", width="medium"),
                "material_query": st.column_config.TextColumn("素材搜索词", width="medium"),
                "providers": st.column_config.TextColumn("素材库", width="small"),
                "source_urls": st.column_config.TextColumn("来源链接", width="medium"),
                "duration_sec": st.column_config.NumberColumn("时长", width="small"),
                "preferred_orientation": st.column_config.TextColumn("方向", width="small"),
                "notes": st.column_config.TextColumn("备注", width="medium"),
            },
        )
        normalized_rows = _preproduction_rows_from_editor(edited_rows)
        if normalized_rows:
            st.session_state["preproduction_rows"] = normalized_rows
            _sync_preproduction_rows_to_inputs(normalized_rows)
            providers = sorted(_providers_required_for_prepare())
            st.caption(
                f"当前计划：{len(normalized_rows)} 段；素材库：{', '.join(providers)}。表格是候选素材准备的来源。"
            )


def _render_simple_editor():
    st.markdown("### 视频生成")
    if st.button("填入测试默认值"):
        st.session_state["video_subject"] = DEFAULT_TEST_VIDEO_SUBJECT
        st.session_state["video_script"] = DEFAULT_TEST_VIDEO_SCRIPT
        st.session_state["video_terms"] = DEFAULT_TEST_VIDEO_TERMS
        st.session_state["preproduction_csv_text"] = _default_preproduction_csv()
        st.session_state["preproduction_rows"] = []
        st.session_state["simple_voice_rate_percent"] = 0
        st.session_state["simple_voice_rate"] = 1.0
        st.session_state.pop("script_keyword_match_signature", None)
        st.session_state.pop("script_keyword_matches", None)
        st.session_state.pop("candidate_task_id", None)
        st.session_state.pop("candidate_task_data", None)
        st.session_state.pop("candidate_selections", None)
        st.rerun()

    _render_preproduction_editor()

    st.text_input("标题", key="video_subject", placeholder="例如：中国五大城市实力排名")
    st.text_area(
        "脚本",
        key="video_script",
        height=300,
        placeholder="每一句单独成段，逐句匹配素材会更稳定。",
    )
    st.text_area(
        "关键词",
        key="video_terms",
        height=150,
        placeholder="每句一个关键词，可以写中文，用逗号分隔。例如：广州城市天际线，北京天安门城市...",
    )
    if _preproduction_plan_from_session():
        st.caption("已启用 CSV / 预生产计划；逐句脚本、中文关键词、素材搜索词和素材库请在上方表格调整。")
    else:
        refresh_match_clicked = st.button("更新关键词匹配预览")
        _render_script_keyword_match_editor(force_refresh=refresh_match_clicked)
        st.multiselect(
            "候选素材来源",
            options=list(SIMPLE_CANDIDATE_PROVIDER_OPTIONS),
            key="simple_candidate_providers",
            format_func=_candidate_provider_label,
            help="选择 X 后，准备候选素材时会通过 AgentReach/twitter-cli 搜索 X 视频，并在候选预览里按来源分组展示。",
        )
        selected_providers = _selected_simple_candidate_providers()
        config.app["candidate_preview_providers"] = selected_providers
        if "x" in selected_providers:
            config.app["enable_x_materials"] = True
            st.caption(
                "已选择 X：候选准备会同时搜索 X 视频；如果某句没有 X 结果，仍会保留其他素材源。"
            )
    _render_voice_rate_control()

    with st.expander("高级设置", expanded=False):
        st.caption("默认使用 Pexels、9:16、顺序拼接、无转场、无 BGM、开启字幕和逐句匹配。CSV 计划可为每段指定 X/Pexels 等素材库。")
        pexels_keys = _as_list_config_value("pexels_api_keys")
        config.app["pexels_api_keys"] = pexels_keys
        new_pexels_key = st.text_input("Pexels API Key", type="password")
        if st.button("保存 Pexels Key"):
            if new_pexels_key and new_pexels_key not in config.app["pexels_api_keys"]:
                config.app["pexels_api_keys"].append(new_pexels_key)
                config.save_config()
                st.success("Pexels Key 已保存")
            elif new_pexels_key:
                st.info("这个 Pexels Key 已存在")
            else:
                st.warning("请输入有效的 Pexels Key")

        config.app["enable_x_materials"] = st.checkbox(
            "启用 X 素材库",
            value=bool(
                config.app.get("enable_x_materials", True)
                or "x" in _selected_simple_candidate_providers()
            ),
            help="通过外部 AgentReach/twitter-cli/OpenCLI 读取 X 视频候选；不会把 AgentReach 源码复制进项目。",
        )
        config.app["agent_reach_command"] = st.text_input(
            "AgentReach 命令",
            value=str(config.app.get("agent_reach_command", "agent-reach")),
            help="默认使用 PATH 里的 agent-reach；如果不可用，会尝试同级 Agent-Reach repo。",
        )
        config.app["x_search_limit"] = st.number_input(
            "X 搜索数量",
            min_value=1,
            max_value=50,
            value=int(config.app.get("x_search_limit", 10) or 10),
            step=1,
        )
        config.app["x_timeout_seconds"] = st.number_input(
            "X/AgentReach 超时秒数",
            min_value=3,
            max_value=120,
            value=int(config.app.get("x_timeout_seconds", 20) or 20),
            step=1,
        )
        config.app["x_cache_policy"] = st.selectbox(
            "X 候选缓存策略",
            options=["task", "off"],
            index=0
            if str(config.app.get("x_cache_policy", "task")).lower() != "off"
            else 1,
            help="task 会把 X top 候选视频缓存到任务目录，预览和渲染更稳定。",
        )

        aspect_options = [item.value for item in VideoAspect]
        st.selectbox(
            "画面比例",
            options=aspect_options,
            index=aspect_options.index(
                st.session_state.get("simple_video_aspect", VideoAspect.portrait.value)
            ),
            key="simple_video_aspect",
        )
        st.text_input(
            "配音声音",
            value=config.ui.get("voice_name", "en-AU-NatashaNeural-Female"),
            key="simple_voice_name",
        )
        st.number_input(
            "旧模式最大片段时长 / 候选搜索最小时长",
            min_value=1,
            max_value=20,
            value=int(st.session_state.get("simple_clip_duration", 5)),
            key="simple_clip_duration",
        )
        st.number_input(
            "字幕字号",
            min_value=20,
            max_value=120,
            value=int(st.session_state.get("simple_font_size", 60)),
            key="simple_font_size",
        )

    action_cols = st.columns(2)
    with action_cols[0]:
        prepare_clicked = st.button(
            "准备候选素材", width="stretch", type="primary"
        )
    with action_cols[1]:
        render_clicked = st.button("渲染最终视频", width="stretch")

    if prepare_clicked:
        config.save_config()
        params = _build_editor_params()
        if not params.video_subject and not params.video_script:
            st.error("标题和脚本不能同时为空。")
            st.stop()
        required_providers = _providers_required_for_prepare()
        if "pexels" in required_providers and not _as_list_config_value("pexels_api_keys"):
            st.error("请先在高级设置里保存 Pexels API Key。")
            st.stop()
        if "pixabay" in required_providers and not _as_list_config_value("pixabay_api_keys"):
            st.error("当前预生产计划使用 Pixabay，请先在配置里保存 Pixabay API Key。")
            st.stop()
        if "coverr" in required_providers and not _as_list_config_value("coverr_api_keys"):
            st.error("当前预生产计划使用 Coverr，请先在配置里保存 Coverr API Key。")
            st.stop()

        task_id = str(uuid4())
        st.session_state["candidate_task_id"] = task_id
        progress_update = _build_progress_updater("准备开始...")
        with st.spinner("正在生成音频、字幕，并为每句准备在线候选视频..."):
            tm.start(
                task_id=task_id,
                params=params,
                stop_at="candidates",
                progress_callback=progress_update,
            )
        task_data = _load_candidate_task(task_id) or {}
        if task_data.get("state") == -1:
            progress_update(1.0, "准备候选素材失败")
            st.error(task_data.get("error", "准备候选素材失败。"))
        else:
            progress_update(1.0, "候选素材准备完成")
            st.session_state["candidate_task_data"] = task_data
            st.success("候选素材准备完成，可以逐句选择和裁剪。")

    task_data = st.session_state.get("candidate_task_data")
    task_id = st.session_state.get("candidate_task_id")
    if task_id:
        task_data = _load_candidate_task(task_id) or task_data
        if task_data:
            st.session_state["candidate_task_data"] = task_data
            _render_candidate_editor(task_data)

    if render_clicked:
        task_id = st.session_state.get("candidate_task_id")
        selections = st.session_state.get("candidate_selections") or []
        if not task_id or not selections:
            st.error("请先准备候选素材，并完成每句视频选择。")
            st.stop()

        progress_update = _build_progress_updater("准备开始渲染...")
        with st.spinner("正在按你的选择重建音频、字幕并渲染视频..."):
            tm.render_selection(
                task_id=task_id,
                selections=selections,
                voice_rate=_simple_voice_rate_value(),
                progress_callback=progress_update,
            )
        task_data = _load_candidate_task(task_id) or {}
        st.session_state["candidate_task_data"] = task_data
        if task_data.get("state") == -1:
            progress_update(1.0, "渲染失败")
            st.error(task_data.get("error", "渲染失败。"))
        else:
            progress_update(1.0, "最终视频已生成")
            st.success("最终视频已生成。")

    latest_task = st.session_state.get("candidate_task_data") or {}
    final_videos = latest_task.get("videos") or []
    if final_videos:
        st.subheader("最终视频")
        for video_path in final_videos:
            _show_video_preview(video_path)
        if latest_task.get("task_id"):
            st.button(
                tr("Open Task Folder"),
                key="simple_open_task_folder",
                on_click=lambda task_id=latest_task.get("task_id"): open_task_folder(
                    task_id
                ),
            )


_render_simple_editor()
st.stop()

# 创建基础设置折叠框
if not config.app.get("hide_config", False):
    with st.expander(tr("Basic Settings"), expanded=False):
        config_panels = st.columns(3)
        left_config_panel = config_panels[0]
        middle_config_panel = config_panels[1]
        right_config_panel = config_panels[2]

        # 左侧面板 - 日志设置
        with left_config_panel:
            # 是否隐藏配置面板
            hide_config = st.checkbox(
                tr("Hide Basic Settings"), value=config.app.get("hide_config", False)
            )
            config.app["hide_config"] = hide_config

            # 是否禁用日志显示
            hide_log = st.checkbox(
                tr("Hide Log"), value=config.ui.get("hide_log", False)
            )
            config.ui["hide_log"] = hide_log

        # 中间面板 - LLM 设置

        with middle_config_panel:
            st.write(tr("LLM Settings"))
            # 下拉框需要展示“AIHubMix（推荐）”这类面向用户的文案，
            # 但配置文件和后端逻辑必须继续使用稳定的小写 provider id。
            # 因此这里显式维护 display label 和 provider id 的映射，避免
            # UI 文案变化污染 `config.app["llm_provider"]`。
            aihubmix_label = f"AIHubMix ({tr('Recommended')})"
            if config.ui.get("language") == "zh":
                aihubmix_label = "AIHubMix（推荐）"
            llm_provider_options = [
                ("OpenAI", "openai"),
                (aihubmix_label, "aihubmix"),
                ("AIML API", "aimlapi"),
                ("Moonshot", "moonshot"),
                ("Azure", "azure"),
                ("Qwen", "qwen"),
                ("DeepSeek", "deepseek"),
                ("ModelScope", "modelscope"),
                ("Gemini", "gemini"),
                ("Grok", "grok"),
                ("Groq", "groq"),
                ("Ollama", "ollama"),
                ("Codex CLI", "codex"),
                ("G4f", "g4f"),
                ("OneAPI", "oneapi"),
                ("Cloudflare", "cloudflare"),
                ("ERNIE", "ernie"),
                ("MiniMax", "minimax"),
                ("MiMo", "mimo"),
                ("Pollinations", "pollinations"),
                ("LiteLLM", "litellm"),
            ]
            llm_provider_labels = [label for label, _ in llm_provider_options]
            llm_provider_values = {
                label: provider_id for label, provider_id in llm_provider_options
            }
            saved_llm_provider = config.app.get("llm_provider", "openai").lower()
            saved_llm_provider_index = 0
            for i, (_, provider_id) in enumerate(llm_provider_options):
                if provider_id == saved_llm_provider:
                    saved_llm_provider_index = i
                    break

            llm_provider_label = st.selectbox(
                tr("LLM Provider"),
                options=llm_provider_labels,
                index=saved_llm_provider_index,
            )
            llm_helper = st.container()
            llm_provider = llm_provider_values[llm_provider_label]
            config.app["llm_provider"] = llm_provider

            llm_api_key = config.app.get(f"{llm_provider}_api_key", "")
            llm_secret_key = config.app.get(
                f"{llm_provider}_secret_key", ""
            )  # only for baidu ernie
            llm_base_url = config.app.get(f"{llm_provider}_base_url", "")
            llm_model_name = config.app.get(f"{llm_provider}_model_name", "")
            llm_account_id = config.app.get(f"{llm_provider}_account_id", "")

            tips = ""
            if llm_provider == "ollama":
                if not llm_model_name:
                    llm_model_name = "qwen:7b"
                if not llm_base_url:
                    llm_base_url = config.get_default_ollama_base_url()

                with llm_helper:
                    docker_hint = ""
                    if config.is_running_in_container():
                        docker_hint = "\n                            > 检测到容器环境，未配置 Base Url 时会默认使用 `http://host.docker.internal:11434/v1`\n"
                    tips = f"""
                            ##### Ollama配置说明
                            - **API Key**: 随便填写，比如 123
                            - **Base Url**: 一般为 http://localhost:11434/v1
                                - 如果 `MoneyPrinterTurbo` 和 `Ollama` **不在同一台机器上**，需要填写 `Ollama` 机器的IP地址
                                - 如果 `MoneyPrinterTurbo` 是 `Docker` 部署，建议填写 `http://host.docker.internal:11434/v1`{docker_hint}
                            - **Model Name**: 使用 `ollama list` 查看，比如 `qwen:7b`
                            """

            if llm_provider == "codex":
                if not llm_model_name:
                    llm_model_name = config.app.get("codex_model_name", "")
                if not config.app.get("codex_command"):
                    config.app["codex_command"] = "codex"
                if "codex_use_oss" not in config.app:
                    config.app["codex_use_oss"] = True
                if not config.app.get("codex_local_provider"):
                    config.app["codex_local_provider"] = "ollama"
                if not config.app.get("codex_timeout"):
                    config.app["codex_timeout"] = 300

                with llm_helper:
                    tips = """
                            ##### Codex CLI Configuration
                            - **Command**: local Codex CLI command or full path
                            - **Use OSS Provider**: enabled by default to avoid the OpenAI API path
                            - **Local Provider**: `ollama` or `lmstudio`
                            - **Model Name**: optional; leave empty to use the local provider default
                            """

            if llm_provider == "openai":
                if not llm_model_name:
                    llm_model_name = "gpt-3.5-turbo"
                with llm_helper:
                    tips = """
                            ##### OpenAI 配置说明
                            > 需要VPN开启全局流量模式
                            - **API Key**: [点击到官网申请](https://platform.openai.com/api-keys)
                            - **Base Url**: 官方 OpenAI 可留空；如果使用 OpenAI 兼容供应商（例如 OpenRouter），请填写对应的兼容接口地址
                            - **Model Name**: 填写**有权限**的模型；如果使用兼容供应商，请填写该平台支持的模型 ID
                            """

            if llm_provider == "aihubmix":
                if not llm_model_name:
                    llm_model_name = "gpt-5.4-mini"
                if not llm_base_url:
                    llm_base_url = "https://aihubmix.com/v1"
                with llm_helper:
                    tips = """
                            ##### AIHubMix 配置说明
                            - **注册链接**: [点击注册 AIHubMix](https://aihubmix.com/?aff=CEve)
                            - **Base Url**: 预填 https://aihubmix.com/v1
                            - **推荐模型**: 默认 gpt-5.4-mini，也可以填写 AIHubMix 支持的免费模型或其它模型 ID

                            推荐理由：
                            - **模型全**: Claude、GPT、Gemini、Grok、DeepSeek、通义等 700+ 模型一站覆盖
                            - **稳定**: 无限并发，永远在线，集群部署于谷歌云，长期为众多知名应用提供高并发服务
                            - **能力完整**: 文本、图片生成、视频生成、TTS、STT、向量嵌入、Rerank，多模态场景全搞定
                            - **计费透明**: 按量付费，无会员无包月，免费模型可使用
                            """

            if llm_provider == "aimlapi":
                if not llm_model_name:
                    llm_model_name = "openai/gpt-4o-mini"
                if not llm_base_url:
                    llm_base_url = "https://api.aimlapi.com/v1"
                with llm_helper:
                    tips = """
                            ##### AIML API Configuration
                            - **API Key**: create one at https://aimlapi.com/app/keys
                            - **Base Url**: https://api.aimlapi.com/v1
                            - **Model Name**: for example `openai/gpt-4o-mini`, `openai/gpt-4o`, `anthropic/claude-sonnet-4.5`, or `google/gemini-3-flash-preview`
                            """

            if llm_provider == "moonshot":
                if not llm_model_name:
                    llm_model_name = "moonshot-v1-8k"
                with llm_helper:
                    tips = """
                            ##### Moonshot 配置说明
                            - **API Key**: [点击到官网申请](https://platform.moonshot.cn/console/api-keys)
                            - **Base Url**: 固定为 https://api.moonshot.cn/v1
                            - **Model Name**: 比如 moonshot-v1-8k，[点击查看模型列表](https://platform.moonshot.cn/docs/intro#%E6%A8%A1%E5%9E%8B%E5%88%97%E8%A1%A8)
                            """
            if llm_provider == "oneapi":
                if not llm_model_name:
                    llm_model_name = (
                        "claude-3-5-sonnet-20240620"  # 默认模型，可以根据需要调整
                    )
                with llm_helper:
                    tips = """
                        ##### OneAPI 配置说明
                        - **API Key**: 填写您的 OneAPI 密钥
                        - **Base Url**: 填写 OneAPI 的基础 URL
                        - **Model Name**: 填写您要使用的模型名称，例如 claude-3-5-sonnet-20240620
                        """

            if llm_provider == "qwen":
                if not llm_model_name:
                    llm_model_name = "qwen-max"
                with llm_helper:
                    tips = """
                            ##### 通义千问Qwen 配置说明
                            - **API Key**: [点击到官网申请](https://dashscope.console.aliyun.com/apiKey)
                            - **Base Url**: 留空
                            - **Model Name**: 比如 qwen-max，[点击查看模型列表](https://help.aliyun.com/zh/dashscope/developer-reference/model-introduction#3ef6d0bcf91wy)
                            """

            if llm_provider == "g4f":
                if not llm_model_name:
                    llm_model_name = "gpt-3.5-turbo"
                with llm_helper:
                    tips = """
                            ##### gpt4free 配置说明
                            > [GitHub开源项目](https://github.com/xtekky/gpt4free)，可以免费使用GPT模型，但是**稳定性较差**
                            - **API Key**: 随便填写，比如 123
                            - **Base Url**: 留空
                            - **Model Name**: 比如 gpt-3.5-turbo，[点击查看模型列表](https://github.com/xtekky/gpt4free/blob/main/g4f/models.py#L308)
                            """
            if llm_provider == "azure":
                with llm_helper:
                    tips = """
                            ##### Azure 配置说明
                            > [点击查看如何部署模型](https://learn.microsoft.com/zh-cn/azure/ai-services/openai/how-to/create-resource)
                            - **API Key**: [点击到Azure后台创建](https://portal.azure.com/#view/Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/OpenAI)
                            - **Base Url**: 留空
                            - **Model Name**: 填写你实际的部署名
                            """

            if llm_provider == "gemini":
                if not llm_model_name:
                    llm_model_name = "gemini-1.0-pro"

                with llm_helper:
                    tips = """
                            ##### Gemini 配置说明
                            > 需要VPN开启全局流量模式
                            - **API Key**: [点击到官网申请](https://ai.google.dev/)
                            - **Base Url**: 留空
                            - **Model Name**: 比如 gemini-1.0-pro
                            """

            if llm_provider == "grok":
                if not llm_model_name:
                    llm_model_name = "grok-4.3"
                if not llm_base_url:
                    llm_base_url = "https://api.x.ai/v1"

                with llm_helper:
                    tips = """
                            ##### Grok 配置说明
                            - **API Key**: 填写您的 GrokAPI 密钥
                            - **Base Url**: 填写 GrokAPI 的基础 URL
                            - **Model Name**: 比如 grok-4.3
                            """

            if llm_provider == "groq":
                if not llm_model_name:
                    llm_model_name = "llama-3.3-70b-versatile"
                if not llm_base_url:
                    llm_base_url = "https://api.groq.com/openai/v1"

                with llm_helper:
                    tips = """
                            ##### Groq 配置说明
                            - **API Key**: [点击到官网申请](https://console.groq.com/keys)
                            - **Base Url**: 固定为 https://api.groq.com/openai/v1
                            - **Model Name**: 比如 llama-3.3-70b-versatile
                            """

            if llm_provider == "deepseek":
                if not llm_model_name:
                    llm_model_name = "deepseek-chat"
                if not llm_base_url:
                    llm_base_url = "https://api.deepseek.com"
                with llm_helper:
                    tips = """
                            ##### DeepSeek 配置说明
                            - **API Key**: [点击到官网申请](https://platform.deepseek.com/api_keys)
                            - **Base Url**: 固定为 https://api.deepseek.com
                            - **Model Name**: 固定为 deepseek-chat
                            """

            if llm_provider == "mimo":
                if not llm_model_name:
                    llm_model_name = "mimo-v2.5-pro"
                if not llm_base_url:
                    llm_base_url = "https://api.xiaomimimo.com/v1"
                with llm_helper:
                    tips = """
                            ##### Xiaomi MiMo 配置说明
                            - **API Key**: [点击到官网申请](https://platform.xiaomimimo.com/docs/zh-CN/quick-start/first-api-call)
                            - **Base Url**: 固定为 https://api.xiaomimimo.com/v1
                            - **Model Name**: 默认 mimo-v2.5-pro，也可以按官方文档填写其它可用模型
                            """

            if llm_provider == "modelscope":
                if not llm_model_name:
                    llm_model_name = "Qwen/Qwen3-32B"
                if not llm_base_url:
                    llm_base_url = "https://api-inference.modelscope.cn/v1/"
                with llm_helper:
                    tips = """
                            ##### ModelScope 配置说明
                            - **API Key**: [点击到官网申请](https://modelscope.cn/docs/model-service/API-Inference/intro)
                            - **Base Url**: 固定为 https://api-inference.modelscope.cn/v1/
                            - **Model Name**: 比如 Qwen/Qwen3-32B，[点击查看模型列表](https://modelscope.cn/models?filter=inference_type&page=1)
                            """

            if llm_provider == "ernie":
                with llm_helper:
                    tips = """
                            ##### 百度文心一言 配置说明
                            - **API Key**: [点击到官网申请](https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application)
                            - **Secret Key**: [点击到官网申请](https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application)
                            - **Base Url**: 填写 **请求地址** [点击查看文档](https://cloud.baidu.com/doc/WENXINWORKSHOP/s/jlil56u11#%E8%AF%B7%E6%B1%82%E8%AF%B4%E6%98%8E)
                            """

            if llm_provider == "pollinations":
                if not llm_model_name:
                    llm_model_name = "default"
                with llm_helper:
                    tips = """
                            ##### Pollinations AI Configuration
                            - **API Key**: Optional - Leave empty for public access
                            - **Base Url**: Default is https://text.pollinations.ai/openai
                            - **Model Name**: Use 'openai-fast' or specify a model name
                            """

            if llm_provider == "litellm":
                if not llm_model_name:
                    llm_model_name = "openai/gpt-4o-mini"
                with llm_helper:
                    tips = """
                            ##### LiteLLM Configuration
                            > [LiteLLM](https://github.com/BerriAI/litellm) routes to 100+ LLM providers via a unified interface.
                            > Set your provider's API key as an env var: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `AWS_ACCESS_KEY_ID`, etc.
                            - **Model Name**: LiteLLM format — `openai/gpt-4o`, `anthropic/claude-sonnet-4-20250514`, `bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0`, `gemini/gemini-2.5-flash`. See [full provider list](https://docs.litellm.ai/docs/providers)
                            """

            if tips and config.ui["language"] == "zh":
                # AIHubMix 自身就是 OpenAI-compatible 聚合平台；用户主动选择
                # 该 provider 时，再显示 DeepSeek/Moonshot 的通用推荐会造成
                # 信息干扰，也不利于保持合作入口的轻量、清晰。
                if llm_provider != "aihubmix":
                    st.warning(
                        "中国用户建议使用 **DeepSeek** 或 **Moonshot** 作为大模型提供商\n- 国内可直接访问，不需要VPN \n- 注册就送额度，基本够用"
                    )
                st.info(tips)

            if llm_provider == "codex":
                st_codex_command = st.text_input(
                    "Codex Command",
                    value=config.app.get("codex_command", "codex"),
                )
                st_codex_use_oss = st.checkbox(
                    "Use local OSS provider",
                    value=bool(config.app.get("codex_use_oss", True)),
                )
                codex_provider_options = ["ollama", "lmstudio"]
                saved_codex_provider = config.app.get("codex_local_provider", "ollama")
                codex_provider_index = 0
                if saved_codex_provider in codex_provider_options:
                    codex_provider_index = codex_provider_options.index(
                        saved_codex_provider
                    )
                st_codex_local_provider = st.selectbox(
                    "Local Provider",
                    options=codex_provider_options,
                    index=codex_provider_index,
                )
                st_llm_model_name = st.text_input(
                    tr("Model Name"),
                    value=llm_model_name,
                    key="codex_model_name_input",
                )
                st_codex_timeout = st.number_input(
                    "Timeout (seconds)",
                    min_value=30,
                    max_value=3600,
                    value=int(config.app.get("codex_timeout", 300) or 300),
                    step=30,
                )

                config.app["codex_command"] = st_codex_command
                config.app["codex_use_oss"] = st_codex_use_oss
                config.app["codex_local_provider"] = st_codex_local_provider
                config.app["codex_timeout"] = int(st_codex_timeout)
                config.app["codex_model_name"] = st_llm_model_name
            else:
                st_llm_api_key = st.text_input(
                    tr("API Key"), value=llm_api_key, type="password"
                )
                st_llm_base_url = st.text_input(tr("Base Url"), value=llm_base_url)
                st_llm_model_name = ""
                if llm_provider != "ernie":
                    if llm_provider == "groq":
                        effective_api_key = st_llm_api_key or llm_api_key
                        effective_base_url = st_llm_base_url or llm_base_url
                        groq_models = get_groq_model_ids(
                            api_key=effective_api_key,
                            base_url=effective_base_url,
                        )

                        if groq_models:
                            selected_index = 0
                            if llm_model_name in groq_models:
                                selected_index = groq_models.index(llm_model_name)

                            st_llm_model_name = st.selectbox(
                                tr("Model Name"),
                                options=groq_models,
                                index=selected_index,
                                key="groq_model_name_select",
                            )
                        else:
                            st_llm_model_name = st.text_input(
                                tr("Model Name"),
                                value=llm_model_name,
                                key="groq_model_name_input",
                            )
                            if effective_api_key:
                                st.caption(
                                    "Unable to load Groq model list right now. You can still enter a model name manually — note it won't be validated until generation."
                                )
                            else:
                                st.caption(
                                    "Add a Groq API key to load available models automatically."
                                )
                    else:
                        st_llm_model_name = st.text_input(
                            tr("Model Name"),
                            value=llm_model_name,
                            key=f"{llm_provider}_model_name_input",
                        )
                    if st_llm_model_name:
                        config.app[f"{llm_provider}_model_name"] = st_llm_model_name
                else:
                    st_llm_model_name = None

                if st_llm_api_key:
                    config.app[f"{llm_provider}_api_key"] = st_llm_api_key
                if st_llm_base_url:
                    config.app[f"{llm_provider}_base_url"] = st_llm_base_url
                if st_llm_model_name:
                    config.app[f"{llm_provider}_model_name"] = st_llm_model_name
                if llm_provider == "ernie":
                    st_llm_secret_key = st.text_input(
                        tr("Secret Key"), value=llm_secret_key, type="password"
                    )
                    config.app[f"{llm_provider}_secret_key"] = st_llm_secret_key

                if llm_provider == "cloudflare":
                    st_llm_account_id = st.text_input(
                        tr("Account ID"), value=llm_account_id
                    )
                    if st_llm_account_id:
                        config.app[f"{llm_provider}_account_id"] = st_llm_account_id

        # 右侧面板 - API 密钥设置
        with right_config_panel:

            def get_keys_from_config(cfg_key):
                api_keys = config.app.get(cfg_key, [])
                if isinstance(api_keys, str):
                    api_keys = [api_keys]
                api_key = ", ".join(api_keys)
                return api_key

            def save_keys_to_config(cfg_key, value):
                value = value.replace(" ", "")
                if value:
                    config.app[cfg_key] = value.split(",")

            st.write(tr("Video Source Settings"))

            pexels_api_key = get_keys_from_config("pexels_api_keys")
            pexels_api_key = st.text_input(
                tr("Pexels API Key"), value=pexels_api_key, type="password"
            )
            save_keys_to_config("pexels_api_keys", pexels_api_key)

            pixabay_api_key = get_keys_from_config("pixabay_api_keys")
            pixabay_api_key = st.text_input(
                tr("Pixabay API Key"), value=pixabay_api_key, type="password"
            )
            save_keys_to_config("pixabay_api_keys", pixabay_api_key)

            coverr_api_key = get_keys_from_config("coverr_api_keys")
            coverr_api_key = st.text_input(
                tr("Coverr API Key"), value=coverr_api_key, type="password"
            )
            save_keys_to_config("coverr_api_keys", coverr_api_key)

llm_provider = config.app.get("llm_provider", "").lower()
panel = st.columns(3)
left_panel = panel[0]
middle_panel = panel[1]
right_panel = panel[2]

params = VideoParams(video_subject="")
params.match_materials_to_script = bool(
    st.session_state.get("match_materials_to_script", False)
)
uploaded_files = []
uploaded_audio_file = None

with left_panel:
    with st.container(border=True):
        st.write(tr("Video Script Settings"))
        params.video_subject = st.text_input(
            tr("Video Subject"),
            key="video_subject",
        ).strip()

        video_languages = [
            (tr("Auto Detect"), ""),
        ]
        for code in support_locales:
            video_languages.append((code, code))

        selected_index = st.selectbox(
            tr("Script Language"),
            index=0,
            options=range(
                len(video_languages)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_languages[x][
                0
            ],  # The label is displayed to the user
        )
        params.video_language = video_languages[selected_index][1]

        with st.expander(tr("Advanced Script Settings"), expanded=False):
            params.paragraph_number = st.slider(
                tr("Script Paragraph Number"),
                min_value=llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
                max_value=llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
                value=st.session_state.get("paragraph_number_input", 1),
                key="paragraph_number_input",
            )
            params.video_script_prompt = st.text_area(
                tr("Custom Script Requirements"),
                height=100,
                max_chars=llm.MAX_SCRIPT_PROMPT_LENGTH,
                placeholder=tr("Custom Script Requirements Placeholder"),
                key="video_script_prompt",
            ).strip()

            use_custom_system_prompt = st.checkbox(
                tr("Use Custom System Prompt"),
                help=tr("Use Custom System Prompt Help"),
                key="use_custom_system_prompt",
            )

            if use_custom_system_prompt:
                custom_system_prompt = st.text_area(
                    tr("Custom System Prompt"),
                    height=240,
                    max_chars=llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
                    key="custom_system_prompt",
                ).strip()
                params.custom_system_prompt = custom_system_prompt
            else:
                params.custom_system_prompt = ""

        if st.button(
            tr("Generate Video Script and Keywords"), key="auto_generate_script"
        ):
            with st.spinner(tr("Generating Video Script and Keywords")):
                script = llm.generate_script(
                    video_subject=params.video_subject,
                    language=params.video_language,
                    paragraph_number=params.paragraph_number,
                    video_script_prompt=params.video_script_prompt,
                    custom_system_prompt=params.custom_system_prompt,
                )
                terms = llm.generate_terms(
                    params.video_subject,
                    script,
                    amount=8 if params.match_materials_to_script else 5,
                    match_script_order=params.match_materials_to_script,
                )
                if "Error: " in script:
                    st.error(tr(script))
                elif "Error: " in terms:
                    st.error(tr(terms))
                else:
                    st.session_state["video_script"] = script
                    st.session_state["video_terms"] = ", ".join(terms)
        params.video_script = st.text_area(
            tr("Video Script"), value=st.session_state["video_script"], height=280
        )
        if st.button(tr("Generate Video Keywords"), key="auto_generate_terms"):
            if not params.video_script:
                st.error(tr("Please Enter the Video Subject"))
                st.stop()

            with st.spinner(tr("Generating Video Keywords")):
                terms = llm.generate_terms(
                    params.video_subject,
                    params.video_script,
                    amount=8 if params.match_materials_to_script else 5,
                    match_script_order=params.match_materials_to_script,
                )
                if "Error: " in terms:
                    st.error(tr(terms))
                else:
                    st.session_state["video_terms"] = ", ".join(terms)

        params.video_terms = st.text_area(
            tr("Video Keywords"), value=st.session_state["video_terms"]
        )

with middle_panel:
    with st.container(border=True):
        st.write(tr("Video Settings"))
        video_concat_modes = [
            (tr("Sequential"), "sequential"),
            (tr("Random"), "random"),
        ]
        video_sources = [
            (tr("Pexels"), "pexels"),
            (tr("Pixabay"), "pixabay"),
            (tr("Coverr"), "coverr"),
            ("X", "x"),
            (tr("Local file"), "local"),
            (tr("TikTok"), "douyin"),
            (tr("Bilibili"), "bilibili"),
            (tr("Xiaohongshu"), "xiaohongshu"),
        ]

        saved_video_source_name = config.app.get("video_source", "pexels")
        saved_video_source_index = [v[1] for v in video_sources].index(
            saved_video_source_name
        )

        selected_index = st.selectbox(
            tr("Video Source"),
            options=range(len(video_sources)),
            format_func=lambda x: video_sources[x][0],
            index=saved_video_source_index,
        )
        params.video_source = video_sources[selected_index][1]
        config.app["video_source"] = params.video_source

        if params.video_source == "local":
            # Streamlit 的文件类型校验对扩展名大小写敏感，这里同时放行大小写两种形式。
            local_file_types = ["mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png"]
            uploaded_files = st.file_uploader(
                tr("Upload Local Files"),
                type=local_file_types + [file_type.upper() for file_type in local_file_types],
                accept_multiple_files=True,
            )

        selected_index = st.selectbox(
            tr("Video Concat Mode"),
            index=1,
            options=range(
                len(video_concat_modes)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_concat_modes[x][
                0
            ],  # The label is displayed to the user
        )
        params.video_concat_mode = VideoConcatMode(
            video_concat_modes[selected_index][1]
        )

        # 视频转场模式
        video_transition_modes = [
            (tr("None"), VideoTransitionMode.none.value),
            (tr("Shuffle"), VideoTransitionMode.shuffle.value),
            (tr("FadeIn"), VideoTransitionMode.fade_in.value),
            (tr("FadeOut"), VideoTransitionMode.fade_out.value),
            (tr("SlideIn"), VideoTransitionMode.slide_in.value),
            (tr("SlideOut"), VideoTransitionMode.slide_out.value),
        ]
        selected_index = st.selectbox(
            tr("Video Transition Mode"),
            options=range(len(video_transition_modes)),
            format_func=lambda x: video_transition_modes[x][0],
            index=0,
        )
        params.video_transition_mode = VideoTransitionMode(
            video_transition_modes[selected_index][1]
        )

        video_aspect_ratios = [
            (tr("Portrait"), VideoAspect.portrait.value),
            (tr("Landscape"), VideoAspect.landscape.value),
        ]
        # Coverr 库 99% 是 16:9 横屏,默认竖屏会让画面被大量黑边包围。
        # 用 source-specific widget key 让每个 source 各自记忆 aspect 选择:
        #   - 首次切到 coverr → 默认 Landscape(index=1)
        #   - 其他 source 沿用 Portrait(index=0)
        #   - 用户在某 source 下手动改过 aspect,session_state 会记住,
        #     下次回到同一 source 时尊重用户选择,不会再被强制覆盖。
        default_aspect_index = 1 if params.video_source == "coverr" else 0
        selected_index = st.selectbox(
            tr("Video Ratio"),
            options=range(
                len(video_aspect_ratios)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_aspect_ratios[x][
                0
            ],  # The label is displayed to the user
            index=default_aspect_index,
            key=f"video_aspect_for_{params.video_source}",
        )
        params.video_aspect = VideoAspect(video_aspect_ratios[selected_index][1])

        params.video_clip_duration = st.selectbox(
            tr("Clip Duration"), options=[2, 3, 4, 5, 6, 7, 8, 9, 10], index=1
        )
        params.video_count = st.selectbox(
            tr("Number of Videos Generated Simultaneously"),
            options=[1, 2, 3, 4, 5],
            index=0,
        )

        with st.expander(tr("Advanced Video Settings"), expanded=False):
            # 默认关闭，避免影响老用户的随机素材体验。开启后只改变关键词和素材
            # 下载/拼接顺序，用于改善画面主题早于或晚于旁白的问题。
            params.match_materials_to_script = st.checkbox(
                tr("Match Materials to Script Order"),
                help=tr("Match Materials to Script Order Help"),
                key="match_materials_to_script",
            )
            config.app["match_materials_to_script"] = params.match_materials_to_script

            video_codec_options = [
                ("libx264 (CPU)", "libx264"),
                ("NVIDIA NVENC (h264_nvenc)", "h264_nvenc"),
                ("AMD AMF (h264_amf)", "h264_amf"),
                ("Intel QSV (h264_qsv)", "h264_qsv"),
                ("Windows MediaFoundation (h264_mf)", "h264_mf"),
                ("macOS VideoToolbox (h264_videotoolbox)", "h264_videotoolbox"),
            ]
            saved_video_codec = config.app.get("video_codec", "libx264")
            saved_video_codec_values = [item[1] for item in video_codec_options]
            if saved_video_codec not in saved_video_codec_values:
                saved_video_codec = "libx264"
            selected_codec_index = saved_video_codec_values.index(saved_video_codec)
            selected_codec_index = st.selectbox(
                tr("Video Encoder"),
                options=range(len(video_codec_options)),
                index=selected_codec_index,
                format_func=lambda x: video_codec_options[x][0],
                help=tr("Video Encoder Help"),
            )
            config.app["video_codec"] = video_codec_options[selected_codec_index][1]
    with st.container(border=True):
        st.write(tr("Audio Settings"))

        # 添加TTS服务器选择下拉框
        tts_servers = [
            (voice.NO_VOICE_NAME, tr("No Voice")),
            ("azure-tts-v1", "Azure TTS V1"),
            ("azure-tts-v2", "Azure TTS V2"),
            ("siliconflow", "SiliconFlow TTS"),
            ("gemini-tts", "Google Gemini TTS"),
            ("mimo-tts", "Xiaomi MiMo TTS"),
        ]

        # 获取保存的TTS服务器，默认为v1
        saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
        saved_tts_server_index = 0
        for i, (server_value, _) in enumerate(tts_servers):
            if server_value == saved_tts_server:
                saved_tts_server_index = i
                break

        selected_tts_server_index = st.selectbox(
            tr("TTS Servers"),
            options=range(len(tts_servers)),
            format_func=lambda x: tts_servers[x][1],
            index=saved_tts_server_index,
        )

        selected_tts_server = tts_servers[selected_tts_server_index][0]
        config.ui["tts_server"] = selected_tts_server

        # 根据选择的TTS服务器获取声音列表
        filtered_voices = []

        if selected_tts_server == voice.NO_VOICE_NAME:
            # 无配音是显式模式，只提供一个稳定 sentinel。这样普通 TTS 的空配置
            # 不会被误判为静音，后端也能继续通过同一条音频/字幕流程生成视频。
            filtered_voices = [voice.NO_VOICE_NAME]
        elif selected_tts_server == "siliconflow":
            # 获取硅基流动的声音列表
            filtered_voices = voice.get_siliconflow_voices()
        elif selected_tts_server == "gemini-tts":
            # 获取Gemini TTS的声音列表
            filtered_voices = voice.get_gemini_voices()
        elif selected_tts_server == "mimo-tts":
            # 获取 Xiaomi MiMo TTS 的预置音色列表
            filtered_voices = voice.get_mimo_voices()
        else:
            # 获取Azure的声音列表
            all_voices = voice.get_all_azure_voices(filter_locals=None)

            # 根据选择的TTS服务器筛选声音
            for v in all_voices:
                if selected_tts_server == "azure-tts-v2":
                    # V2版本的声音名称中包含"v2"
                    if "V2" in v:
                        filtered_voices.append(v)
                else:
                    # V1版本的声音名称中不包含"v2"
                    if "V2" not in v:
                        filtered_voices.append(v)

        if selected_tts_server == voice.NO_VOICE_NAME:
            friendly_names = {voice.NO_VOICE_NAME: tr("No Voice")}
        else:
            friendly_names = {
                v: v.replace("Female", tr("Female"))
                .replace("Male", tr("Male"))
                .replace("Neural", "")
                for v in filtered_voices
            }

        saved_voice_name = config.ui.get("voice_name", "")
        saved_voice_name_index = 0

        # 检查保存的声音是否在当前筛选的声音列表中
        if saved_voice_name in friendly_names:
            saved_voice_name_index = list(friendly_names.keys()).index(saved_voice_name)
        else:
            # 如果不在，则根据当前UI语言选择一个默认声音
            for i, v in enumerate(filtered_voices):
                if v.lower().startswith(st.session_state["ui_language"].lower()):
                    saved_voice_name_index = i
                    break

        # 如果没有找到匹配的声音，使用第一个声音
        if saved_voice_name_index >= len(friendly_names) and friendly_names:
            saved_voice_name_index = 0

        # 确保有声音可选
        if friendly_names:
            selected_friendly_name = st.selectbox(
                tr("Speech Synthesis"),
                options=list(friendly_names.values()),
                index=min(saved_voice_name_index, len(friendly_names) - 1)
                if friendly_names
                else 0,
            )

            voice_name = list(friendly_names.keys())[
                list(friendly_names.values()).index(selected_friendly_name)
            ]
            params.voice_name = voice_name
            config.ui["voice_name"] = voice_name
        else:
            # 如果没有声音可选，显示提示信息
            st.warning(
                tr(
                    "No voices available for the selected TTS server. Please select another server."
                )
            )
            params.voice_name = ""
            config.ui["voice_name"] = ""

        # 无配音模式会生成静音占位音频，不展示试听按钮，避免用户误以为需要测试声音。
        if (
            friendly_names
            and selected_tts_server != voice.NO_VOICE_NAME
            and st.button(tr("Play Voice"))
        ):
            play_content = params.video_subject
            if not play_content:
                play_content = params.video_script
            if not play_content:
                play_content = tr("Voice Example")
            with st.spinner(tr("Synthesizing Voice")):
                temp_dir = utils.storage_dir("temp", create=True)
                audio_file = os.path.join(temp_dir, f"tmp-voice-{str(uuid4())}.mp3")
                sub_maker = voice.tts(
                    text=play_content,
                    voice_name=voice_name,
                    voice_rate=params.voice_rate,
                    voice_file=audio_file,
                    voice_volume=params.voice_volume,
                )
                # if the voice file generation failed, try again with a default content.
                if not sub_maker:
                    play_content = "This is a example voice. if you hear this, the voice synthesis failed with the original content."
                    sub_maker = voice.tts(
                        text=play_content,
                        voice_name=voice_name,
                        voice_rate=params.voice_rate,
                        voice_file=audio_file,
                        voice_volume=params.voice_volume,
                    )

                if sub_maker and os.path.exists(audio_file):
                    st.audio(audio_file, format="audio/mp3")
                    if os.path.exists(audio_file):
                        os.remove(audio_file)

        # 当选择V2版本或者声音是V2声音时，显示服务区域和API key输入框
        if selected_tts_server == "azure-tts-v2" or (
            voice_name and voice.is_azure_v2_voice(voice_name)
        ):
            saved_azure_speech_region = config.azure.get("speech_region", "")
            saved_azure_speech_key = config.azure.get("speech_key", "")
            azure_speech_region = st.text_input(
                tr("Speech Region"),
                value=saved_azure_speech_region,
                key="azure_speech_region_input",
            )
            azure_speech_key = st.text_input(
                tr("Speech Key"),
                value=saved_azure_speech_key,
                type="password",
                key="azure_speech_key_input",
            )
            config.azure["speech_region"] = azure_speech_region
            config.azure["speech_key"] = azure_speech_key

        # 当选择硅基流动时，显示API key输入框和说明信息
        if selected_tts_server == "siliconflow" or (
            voice_name and voice.is_siliconflow_voice(voice_name)
        ):
            saved_siliconflow_api_key = config.siliconflow.get("api_key", "")

            siliconflow_api_key = st.text_input(
                tr("SiliconFlow API Key"),
                value=saved_siliconflow_api_key,
                type="password",
                key="siliconflow_api_key_input",
            )

            # 显示硅基流动的说明信息
            st.info(
                tr("SiliconFlow TTS Settings")
                + ":\n"
                + "- "
                + tr("Speed: Range [0.25, 4.0], default is 1.0")
                + "\n"
                + "- "
                + tr("Volume: Uses Speech Volume setting, default 1.0 maps to gain 0")
            )

            config.siliconflow["api_key"] = siliconflow_api_key

        # 当选择 Xiaomi MiMo TTS 时，复用 MiMo LLM provider 的 API Key。
        # 这样用户如果同时使用 MiMo 生成文案和语音，只需要维护一份密钥。
        if selected_tts_server == "mimo-tts" or (
            voice_name and voice.is_mimo_voice(voice_name)
        ):
            saved_mimo_api_key = config.app.get("mimo_api_key", "")

            mimo_api_key = st.text_input(
                tr("MiMo API Key"),
                value=saved_mimo_api_key,
                type="password",
                key="mimo_tts_api_key_input",
            )

            st.info(
                tr("MiMo TTS Settings")
                + ":\n"
                + "- "
                + tr("Uses Xiaomi MiMo V2.5 TTS preset voices")
                + "\n"
                + "- "
                + tr("Speed and volume are currently handled by the provider defaults")
            )

            config.app["mimo_api_key"] = mimo_api_key

        params.voice_volume = st.selectbox(
            tr("Speech Volume"),
            options=[0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 5.0],
            index=2,
        )

        params.voice_rate = st.selectbox(
            tr("Speech Rate"),
            options=[0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0],
            index=2,
        )

        custom_audio_file_types = ["mp3", "wav", "m4a", "aac", "flac", "ogg"]
        uploaded_audio_file = st.file_uploader(
            tr("Custom Audio File"),
            type=custom_audio_file_types
            + [file_type.upper() for file_type in custom_audio_file_types],
            accept_multiple_files=False,
            key="custom_audio_file_uploader",
        )
        if uploaded_audio_file:
            st.audio(uploaded_audio_file, format="audio/mp3")
            st.info(
                tr(
                    "Custom audio will be used directly. TTS synthesis will be skipped for this task."
                )
            )

        bgm_options = [
            (tr("No Background Music"), ""),
            (tr("Random Background Music"), "random"),
            (tr("Custom Background Music"), "custom"),
        ]
        selected_index = st.selectbox(
            tr("Background Music"),
            index=1,
            options=range(
                len(bgm_options)
            ),  # Use the index as the internal option value
            format_func=lambda x: bgm_options[x][
                0
            ],  # The label is displayed to the user
        )
        # Get the selected background music type
        params.bgm_type = bgm_options[selected_index][1]

        # Show or hide components based on the selection
        if params.bgm_type == "custom":
            custom_bgm_file = st.text_input(
                tr("Custom Background Music File"), key="custom_bgm_file_input"
            )
            if custom_bgm_file:
                # 这里不直接用 os.path.exists 判断，因为用户常见输入是
                # output000.mp3，这个文件名需要由服务层映射到 resource/songs
                # 目录后再校验。服务层会统一限制目录和文件类型，避免任意路径读取。
                params.bgm_file = custom_bgm_file.strip()
                # st.write(f":red[已选择自定义背景音乐]：**{custom_bgm_file}**")
        params.bgm_volume = st.selectbox(
            tr("Background Music Volume"),
            options=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
            index=2,
        )

with right_panel:
    with st.container(border=True):
        st.write(tr("Subtitle Settings"))
        params.subtitle_enabled = st.checkbox(tr("Enable Subtitles"), value=True)
        font_names = get_all_fonts()
        saved_font_name = config.ui.get("font_name", "MicrosoftYaHeiBold.ttc")
        saved_font_name_index = 0
        if saved_font_name in font_names:
            saved_font_name_index = font_names.index(saved_font_name)
        params.font_name = st.selectbox(
            tr("Font"), font_names, index=saved_font_name_index
        )
        config.ui["font_name"] = params.font_name

        subtitle_positions = [
            (tr("Top"), "top"),
            (tr("Center"), "center"),
            (tr("Bottom"), "bottom"),
            (tr("Custom"), "custom"),
        ]
        saved_subtitle_position = config.ui.get("subtitle_position", "bottom")
        saved_position_index = 2
        for i, (_, pos_value) in enumerate(subtitle_positions):
            if pos_value == saved_subtitle_position:
                saved_position_index = i
                break
        selected_index = st.selectbox(
            tr("Position"),
            index=saved_position_index,
            options=range(len(subtitle_positions)),
            format_func=lambda x: subtitle_positions[x][0],
        )
        params.subtitle_position = subtitle_positions[selected_index][1]
        config.ui["subtitle_position"] = params.subtitle_position

        if params.subtitle_position == "custom":
            saved_custom_position = config.ui.get("custom_position", 70.0)
            custom_position = st.text_input(
                tr("Custom Position (% from top)"),
                value=str(saved_custom_position),
                key="custom_position_input",
            )
            try:
                params.custom_position = float(custom_position)
                if params.custom_position < 0 or params.custom_position > 100:
                    st.error(tr("Please enter a value between 0 and 100"))
                else:
                    config.ui["custom_position"] = params.custom_position
            except ValueError:
                st.error(tr("Please enter a valid number"))

        font_cols = st.columns([0.3, 0.7])
        with font_cols[0]:
            saved_text_fore_color = config.ui.get("text_fore_color", "#FFFFFF")
            params.text_fore_color = st.color_picker(
                tr("Font Color"), saved_text_fore_color
            )
            config.ui["text_fore_color"] = params.text_fore_color

        with font_cols[1]:
            saved_font_size = config.ui.get("font_size", 60)
            params.font_size = st.slider(tr("Font Size"), 30, 100, saved_font_size)
            config.ui["font_size"] = params.font_size

        stroke_cols = st.columns([0.3, 0.7])
        with stroke_cols[0]:
            params.stroke_color = st.color_picker(tr("Stroke Color"), "#000000")
        with stroke_cols[1]:
            params.stroke_width = st.slider(tr("Stroke Width"), 0.0, 10.0, 1.5)

        subtitle_bg_cols = st.columns([0.4, 0.6])
        saved_subtitle_background_enabled = config.ui.get(
            "subtitle_background_enabled", True
        )
        with subtitle_bg_cols[0]:
            subtitle_background_enabled = st.checkbox(
                tr("Enable Subtitle Background"),
                value=saved_subtitle_background_enabled,
            )
        config.ui["subtitle_background_enabled"] = subtitle_background_enabled
        if subtitle_background_enabled:
            with subtitle_bg_cols[1]:
                saved_subtitle_background_color = config.ui.get(
                    "subtitle_background_color", "#000000"
                )
                params.text_background_color = st.color_picker(
                    tr("Subtitle Background Color"),
                    saved_subtitle_background_color,
                )
                config.ui["subtitle_background_color"] = params.text_background_color
        else:
            params.text_background_color = False

        saved_rounded_subtitle_background = config.ui.get(
            "rounded_subtitle_background", False
        )
        # 背景关闭时，圆角背景没有可渲染的底色。这里禁用控件并保留原配置，
        # 用户下次重新开启字幕背景后，可以继续使用之前保存的圆角偏好。
        params.rounded_subtitle_background = st.checkbox(
            tr("Rounded Subtitle Background"),
            value=(
                saved_rounded_subtitle_background
                if subtitle_background_enabled
                else False
            ),
            help=tr("Rounded Subtitle Background Help"),
            disabled=not subtitle_background_enabled,
        )
        if subtitle_background_enabled:
            config.ui["rounded_subtitle_background"] = (
                params.rounded_subtitle_background
            )
    with st.expander(tr("Click to show API Key management"), expanded=False):
        st.subheader(tr("Manage Pexels, Pixabay and Coverr API Keys"))

        col1, col2, col3 = st.tabs([
            tr("Pexels API Keys"),
            tr("Pixabay API Keys"),
            tr("Coverr API Keys"),
        ])

        with col1:
            st.subheader(tr("Pexels API Keys"))
            if config.app["pexels_api_keys"]:
                st.write(tr("Current Keys:"))
                for key in config.app["pexels_api_keys"]:
                    st.code(key)
            else:
                st.info(tr("No Pexels API Keys currently"))

            new_key = st.text_input(tr("Add Pexels API Key"), key="pexels_new_key")
            if st.button(tr("Add Pexels API Key")):
                if new_key and new_key not in config.app["pexels_api_keys"]:
                    config.app["pexels_api_keys"].append(new_key)
                    config.save_config()
                    st.success(tr("Pexels API Key added successfully"))
                elif new_key in config.app["pexels_api_keys"]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app["pexels_api_keys"]:
                delete_key = st.selectbox(
                    tr("Select Pexels API Key to delete"), config.app["pexels_api_keys"], key="pexels_delete_key"
                )
                if st.button(tr("Delete Selected Pexels API Key")):
                    config.app["pexels_api_keys"].remove(delete_key)
                    config.save_config()
                    st.success(tr("Pexels API Key deleted successfully"))

        with col2:
            st.subheader(tr("Pixabay API Keys"))

            if config.app["pixabay_api_keys"]:
                st.write(tr("Current Keys:"))
                for key in config.app["pixabay_api_keys"]:
                    st.code(key)
            else:
                st.info(tr("No Pixabay API Keys currently"))

            new_key = st.text_input(tr("Add Pixabay API Key"), key="pixabay_new_key")
            if st.button(tr("Add Pixabay API Key")):
                if new_key and new_key not in config.app["pixabay_api_keys"]:
                    config.app["pixabay_api_keys"].append(new_key)
                    config.save_config()
                    st.success(tr("Pixabay API Key added successfully"))
                elif new_key in config.app["pixabay_api_keys"]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app["pixabay_api_keys"]:
                delete_key = st.selectbox(
                    tr("Select Pixabay API Key to delete"), config.app["pixabay_api_keys"], key="pixabay_delete_key"
                )
                if st.button(tr("Delete Selected Pixabay API Key")):
                    config.app["pixabay_api_keys"].remove(delete_key)
                    config.save_config()
                    st.success(tr("Pixabay API Key deleted successfully"))

        with col3:
            st.subheader(tr("Coverr API Keys"))

            # 与 pexels/pixabay 不同,coverr_api_keys 是 PR 新增配置项,
            # 老用户的 config.toml 不一定包含,这里先兜底初始化为空列表,
            # 防止下面 .append / 索引访问触发 KeyError。
            if "coverr_api_keys" not in config.app or config.app["coverr_api_keys"] is None:
                config.app["coverr_api_keys"] = []

            if config.app["coverr_api_keys"]:
                st.write(tr("Current Keys:"))
                for key in config.app["coverr_api_keys"]:
                    st.code(key)
            else:
                st.info(tr("No Coverr API Keys currently"))

            new_key = st.text_input(tr("Add Coverr API Key"), key="coverr_new_key")
            if st.button(tr("Add Coverr API Key")):
                if new_key and new_key not in config.app["coverr_api_keys"]:
                    config.app["coverr_api_keys"].append(new_key)
                    config.save_config()
                    st.success(tr("Coverr API Key added successfully"))
                elif new_key in config.app["coverr_api_keys"]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app["coverr_api_keys"]:
                delete_key = st.selectbox(
                    tr("Select Coverr API Key to delete"), config.app["coverr_api_keys"], key="coverr_delete_key"
                )
                if st.button(tr("Delete Selected Coverr API Key")):
                    config.app["coverr_api_keys"].remove(delete_key)
                    config.save_config()
                    st.success(tr("Coverr API Key deleted successfully"))

start_button = st.button(tr("Generate Video"), width="stretch", type="primary")
if start_button:
    config.save_config()
    task_id = str(uuid4())
    if not params.video_subject and not params.video_script:
        st.error(tr("Video Script and Subject Cannot Both Be Empty"))
        scroll_to_bottom()
        st.stop()

    if params.video_source not in ["pexels", "pixabay", "coverr", "local", "x"]:
        st.error(tr("Please Select a Valid Video Source"))
        scroll_to_bottom()
        st.stop()

    if params.video_source == "pexels" and not config.app.get("pexels_api_keys", ""):
        st.error(tr("Please Enter the Pexels API Key"))
        scroll_to_bottom()
        st.stop()

    if params.video_source == "pixabay" and not config.app.get("pixabay_api_keys", ""):
        st.error(tr("Please Enter the Pixabay API Key"))
        scroll_to_bottom()
        st.stop()

    if params.video_source == "coverr" and not config.app.get("coverr_api_keys", ""):
        st.error(tr("Please Enter the Coverr API Key"))
        scroll_to_bottom()
        st.stop()

    if uploaded_audio_file:
        task_dir = utils.task_dir(task_id)
        # 上传文件名来自浏览器，不能直接拼到磁盘路径里；这里只保留扩展名，
        # 并使用固定文件名保存到当前任务目录，避免路径穿越或特殊字符问题。
        _, audio_ext = os.path.splitext(os.path.basename(uploaded_audio_file.name))
        audio_ext = audio_ext.lower() or ".mp3"
        custom_audio_path = os.path.join(task_dir, f"custom-audio{audio_ext}")
        with open(custom_audio_path, "wb") as f:
            f.write(uploaded_audio_file.getbuffer())
        params.custom_audio_file = custom_audio_path

    if uploaded_files:
        local_videos_dir = utils.storage_dir("local_videos", create=True)
        # 每次重新上传时都以本次选择的素材为准，避免旧素材不断重复追加。
        params.video_materials = []
        persisted_local_materials = []
        for file in uploaded_files:
            file_path = os.path.join(local_videos_dir, f"{file.file_id}_{file.name}")
            with open(file_path, "wb") as f:
                f.write(file.getbuffer())
                m = MaterialInfo()
                m.provider = "local"
                m.url = file_path
                params.video_materials.append(m)
                persisted_local_materials.append(
                    {
                        "provider": m.provider,
                        "url": m.url,
                        "duration": m.duration,
                    }
                )
        # 将已上传并保存到本地的视频素材写入会话，供后续只改文案时直接复用。
        st.session_state["local_video_materials"] = persisted_local_materials
    elif params.video_source == "local" and st.session_state["local_video_materials"]:
        # 当用户没有重新上传文件时，复用最近一次已经保存到磁盘的本地素材列表。
        params.video_materials = []
        for material in st.session_state["local_video_materials"]:
            m = MaterialInfo()
            m.provider = material.get("provider", "local")
            m.url = material.get("url", "")
            m.duration = material.get("duration", 0)
            if m.url:
                params.video_materials.append(m)

    log_container = st.empty()
    log_records = []

    def log_received(msg):
        if config.ui["hide_log"]:
            return
        with log_container:
            log_records.append(msg)
            st.code("\n".join(log_records))

    logger.add(log_received)

    st.toast(tr("Generating Video"))
    logger.info(tr("Start Generating Video"))
    logger.info(utils.to_json(params))
    scroll_to_bottom()

    result = tm.start(task_id=task_id, params=params)
    if not result or "videos" not in result:
        st.error(tr("Video Generation Failed"))
        logger.error(tr("Video Generation Failed"))
        scroll_to_bottom()
        st.stop()

    video_files = result.get("videos", [])
    st.success(tr("Video Generation Completed"))
    try:
        if video_files:
            player_cols = st.columns(len(video_files) * 2 + 1)
            for i, url in enumerate(video_files):
                player_cols[i * 2 + 1].video(url)
    except Exception:
        pass

    open_task_folder(task_id)
    logger.info(tr("Video Generation Completed"))
    scroll_to_bottom()

config.save_config()
