import csv
import io
import re
from typing import Any

from app.utils import utils


ALLOWED_PROVIDERS = {"x", "pexels", "pixabay", "coverr", "local", "url"}
DEFAULT_PROVIDERS = ["pexels"]
DEFAULT_COLUMNS = (
    "segment_index",
    "title",
    "script",
    "keyword_cn",
    "material_query",
    "providers",
    "source_urls",
    "duration_sec",
    "preferred_orientation",
    "notes",
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_provider_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = re.split(r"[,，;；|/]+", _clean_text(value))

    providers = []
    for item in raw_items:
        provider = _clean_text(item).lower()
        if provider == "twitter":
            provider = "x"
        if provider in ALLOWED_PROVIDERS and provider not in providers:
            providers.append(provider)
    return providers or list(DEFAULT_PROVIDERS)


def normalize_source_urls(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = re.split(r"[\n,，;；]+", _clean_text(value))
    urls = []
    for item in raw_items:
        url = _clean_text(item)
        if url and url not in urls:
            urls.append(url)
    return urls


def _read_csv_rows(csv_text: str) -> list[dict[str, Any]]:
    csv_text = (csv_text or "").strip("\ufeff \n\r\t")
    if not csv_text:
        return []

    sample = csv_text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;，")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(csv_text), dialect=dialect)
    if reader.fieldnames:
        normalized_fieldnames = [str(field or "").strip() for field in reader.fieldnames]
        reader.fieldnames = normalized_fieldnames
    return [dict(row) for row in reader if any(_clean_text(value) for value in row.values())]


def normalize_preproduction_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        script = _clean_text(row.get("script") or row.get("text") or row.get("句子"))
        keyword_cn = _clean_text(
            row.get("keyword_cn")
            or row.get("keyword")
            or row.get("关键词")
            or row.get("term")
        )
        material_query = _clean_text(
            row.get("material_query")
            or row.get("search_query")
            or row.get("搜索词")
            or keyword_cn
        )
        if not script and not keyword_cn and not material_query:
            continue

        index_raw = row.get("segment_index") or row.get("index") or row.get("句")
        try:
            segment_index = int(index_raw)
        except (TypeError, ValueError):
            segment_index = len(normalized) + 1

        duration_raw = row.get("duration_sec") or row.get("duration") or ""
        try:
            duration_sec = float(duration_raw) if _clean_text(duration_raw) else 0.0
        except (TypeError, ValueError):
            duration_sec = 0.0

        source_urls = normalize_source_urls(row.get("source_urls") or row.get("素材链接"))
        provider_value = row.get("providers") or row.get("素材库")
        providers = normalize_provider_list(provider_value)
        if not _clean_text(provider_value) and source_urls:
            providers = ["url"]

        normalized.append(
            {
                "segment_index": segment_index,
                "title": _clean_text(row.get("title") or row.get("标题")),
                "script": script,
                "keyword_cn": keyword_cn or material_query,
                "material_query": material_query or keyword_cn or script,
                "providers": providers,
                "source_urls": source_urls,
                "duration_sec": duration_sec,
                "preferred_orientation": _clean_text(
                    row.get("preferred_orientation") or row.get("orientation") or row.get("方向")
                ),
                "notes": _clean_text(row.get("notes") or row.get("备注")),
            }
        )

    for index, row in enumerate(normalized, start=1):
        row["segment_index"] = index
    return normalized


def parse_preproduction_csv(csv_text: str) -> list[dict[str, Any]]:
    return normalize_preproduction_rows(_read_csv_rows(csv_text))


def rows_to_video_script(rows: list[dict[str, Any]]) -> str:
    return "\n".join(row["script"] for row in normalize_preproduction_rows(rows) if row.get("script"))


def rows_to_display_terms(rows: list[dict[str, Any]]) -> list[str]:
    return [
        row["keyword_cn"]
        for row in normalize_preproduction_rows(rows)
        if row.get("keyword_cn")
    ]


def rows_to_search_terms(rows: list[dict[str, Any]]) -> list[str]:
    return [
        row["material_query"]
        for row in normalize_preproduction_rows(rows)
        if row.get("material_query")
    ]


def build_preproduction_plan(rows: list[dict[str, Any]], title: str = "") -> dict[str, Any]:
    segments = normalize_preproduction_rows(rows)
    if not title:
        for row in segments:
            if row.get("title"):
                title = row["title"]
                break
    return {
        "version": 1,
        "title": _clean_text(title),
        "script": rows_to_video_script(segments),
        "display_terms": rows_to_display_terms(segments),
        "search_terms": rows_to_search_terms(segments),
        "segments": segments,
        "signature": utils.md5(utils.to_json(segments)),
    }


def segment_plan_by_index(plan: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    segment_map = {}
    if not isinstance(plan, dict):
        return segment_map
    for index, segment in enumerate(plan.get("segments") or [], start=1):
        if not isinstance(segment, dict):
            continue
        try:
            segment_index = int(segment.get("segment_index") or index)
        except (TypeError, ValueError):
            segment_index = index
        segment_map[segment_index] = segment
    return segment_map
