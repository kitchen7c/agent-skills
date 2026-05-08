import os
import sys
import argparse
import json
import base64
import mimetypes
import html
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_FONT_DIR = PROJECT_ROOT / "assets" / "fonts"
FONT_FILE_CANDIDATES = {
    "regular": (
        "NotoSansSC-Regular.ttf",
        "NotoSansCJKsc-Regular.otf",
        "SourceHanSansSC-Regular.otf",
    ),
    "bold": (
        "NotoSansSC-Bold.ttf",
        "NotoSansCJKsc-Bold.otf",
        "SourceHanSansSC-Bold.otf",
    ),
}
FALLBACK_FONT_STACK = (
    '"Noto Sans SC Embedded", "Noto Sans SC", "Noto Sans CJK SC", '
    '"Source Han Sans SC", "WenQuanYi Zen Hei", "PingFang SC", '
    '"Microsoft YaHei", sans-serif'
)
PUBLICATION_SEMANTIC_TOKENS = (
    ("argus", 3),
    ("asia", 2),
    ("bitumen", 3),
    ("daily", 2),
    ("report", 1),
    ("pdf", 2),
    ("download", 2),
    ("article", 1),
)
NEWS_SENTENCE_HINTS = (
    "holiday",
    "close",
    "assessment",
    "agreement",
    "approve",
    "approved",
    "transit",
    "strait",
    "tanker",
    "vessel",
    "shipping",
    "cargo",
    "supply",
    "export",
    "import",
    "sanction",
)
DEFAULT_ARGUS_PUBLICATION_ID = "291"
MAX_REPORT_LAG_DAYS = 7
AGENT_BROWSER_WAIT_MS = 1500
AGENT_BROWSER_CAPTURE_TIMEOUT_SECONDS = 20


def get_required_env(name):
    value = os.environ.get(name)
    if value:
        return value
    raise RuntimeError(f"缺少必需环境变量: {name}")


def import_requests():
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少依赖 requests，请先安装 requests") from exc
    return requests


def import_pymupdf():
    try:
        import fitz
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少依赖 PyMuPDF，请先安装 PyMuPDF") from exc
    return fitz


def import_openai_client():
    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少依赖 openai，请先安装 openai") from exc
    return OpenAI


def ensure_output_dir(output_dir):
    """规范化并确保输出目录存在。"""
    if not output_dir:
        return None
    return os.path.abspath(os.path.expanduser(output_dir))


def prepare_output_path(path_like):
    """确保输出目录存在，并显式删除旧文件以保证覆盖。"""
    output_path = Path(path_like)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    return output_path


def convert_image_to_jpeg(source_path, output_path, quality=92):
    """将浏览器截图转换为真正的 JPEG 文件，避免只改扩展名。"""
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少依赖 Pillow，请先安装 Pillow") from exc

    source_path = Path(source_path)
    output_path = prepare_output_path(output_path)
    with Image.open(source_path) as image:
        image.load()
        if image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        ):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            rgb_image = background
        else:
            rgb_image = image.convert("RGB")
        rgb_image.save(output_path, format="JPEG", quality=quality, optimize=True)
    return output_path


def filename_matches_target_date(filename, target_yyyymmdd):
    """判断文件名中是否包含目标日期。"""
    stem = Path(filename).name
    compact = target_yyyymmdd
    dashed = f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
    month_abbr = datetime.strptime(compact, "%Y%m%d").strftime("%d-%b-%Y")

    lowered = stem.lower()
    return (
        compact in stem
        or dashed in stem
        or month_abbr.lower() in lowered
    )


def argus_expected_report_date(reference_dt=None):
    """Argus 日报日期规则：
    - 周一下载对应上周五
    - 周六、周日下载对应上周五
    - 周二到周五下载对应前一天
    """
    dt = reference_dt or datetime.now()
    if dt.weekday() == 0:
        delta_days = 3
    elif dt.weekday() == 6:
        delta_days = 2
    else:
        delta_days = 1
    return (dt - timedelta(days=delta_days)).strftime("%Y%m%d")


def extract_report_date_from_filename(filename):
    stem = Path(filename).name

    compact_match = re.search(r"(20\d{6})", stem)
    if compact_match:
        return datetime.strptime(compact_match.group(1), "%Y%m%d").strftime("%Y年%m月%d日")

    dashed_match = re.search(r"(20\d{2}-\d{2}-\d{2})", stem)
    if dashed_match:
        return datetime.strptime(dashed_match.group(1), "%Y-%m-%d").strftime("%Y年%m月%d日")

    month_match = re.search(r"(\d{2}-[A-Za-z]{3}-20\d{2})", stem)
    if month_match:
        return datetime.strptime(month_match.group(1), "%d-%b-%Y").strftime("%Y年%m月%d日")

    return "-"


def format_report_date_cn(raw):
    raw = (raw or "").strip()
    if not raw:
        return "-"
    if re.fullmatch(r"20\d{2}年\d{2}月\d{2}日", raw):
        return raw

    compact = extract_report_date_compact(raw)
    if compact:
        return datetime.strptime(compact, "%Y%m%d").strftime("%Y年%m月%d日")

    raise ValueError(f"无法解析报告日期: {raw}")


def extract_report_date_compact(text):
    raw = (text or "").strip()
    if not raw:
        return None

    compact_match = re.search(r"(20\d{6})", raw)
    if compact_match:
        return compact_match.group(1)

    dashed_match = re.search(r"(20\d{2}-\d{2}-\d{2})", raw)
    if dashed_match:
        return datetime.strptime(dashed_match.group(1), "%Y-%m-%d").strftime("%Y%m%d")

    month_match = re.search(r"(\d{2}-[A-Za-z]{3}-20\d{2})", raw)
    if month_match:
        return datetime.strptime(month_match.group(1), "%d-%b-%Y").strftime("%Y%m%d")

    return None


def is_report_date_within_lag(candidate_date, target_date, max_lag_days=MAX_REPORT_LAG_DAYS):
    if not candidate_date or not target_date:
        return False

    try:
        candidate_dt = datetime.strptime(candidate_date, "%Y%m%d")
        target_dt = datetime.strptime(target_date, "%Y%m%d")
    except ValueError:
        return False

    lag_days = (target_dt - candidate_dt).days
    return 0 <= lag_days <= max_lag_days


def report_date_lag_days(candidate_date, target_date):
    if not candidate_date or not target_date:
        return None
    try:
        candidate_dt = datetime.strptime(candidate_date, "%Y%m%d")
        target_dt = datetime.strptime(target_date, "%Y%m%d")
    except ValueError:
        return None
    return (target_dt - candidate_dt).days


def build_generated_report_stem(current_date_cn):
    compact = current_date_cn.replace("年", "").replace("月", "").replace("日", "")
    return f"Argus_Asia_Bitumen_Daily_{compact}"


def is_current_report_file(file_path, target_yyyymmdd):
    path = Path(file_path)
    return path.exists() and path.suffix.lower() == ".pdf" and filename_matches_target_date(
        path.name, target_yyyymmdd
    )


@dataclass
class FetchResult:
    source_type: str
    source_name: str
    source_report_date: str = "-"
    pdf_path: Optional[Path] = None
    artifact_path: Optional[Path] = None
    text_content: Optional[str] = None
    fallback_reason: Optional[str] = None


@dataclass(frozen=True)
class ProxySettings:
    proxy: Optional[str] = None
    proxy_bypass: Optional[str] = None

    def build_environment(self, base_env=None):
        env = dict(base_env or os.environ)
        managed_proxy_keys = (
            "AGENT_BROWSER_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        )
        managed_bypass_keys = (
            "AGENT_BROWSER_PROXY_BYPASS",
            "NO_PROXY",
            "no_proxy",
        )

        if self.proxy:
            for key in managed_proxy_keys:
                env[key] = self.proxy
        else:
            for key in managed_proxy_keys:
                env.pop(key, None)

        if self.proxy_bypass:
            for key in managed_bypass_keys:
                env[key] = self.proxy_bypass
        else:
            for key in managed_bypass_keys:
                env.pop(key, None)

        return env


class ArgusStageError(RuntimeError):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


def first_env_value(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def resolve_proxy_settings():
    proxy = first_env_value(
        "AGENT_BROWSER_PROXY",
    )
    proxy_bypass = first_env_value(
        "AGENT_BROWSER_PROXY_BYPASS",
        "NO_PROXY",
        "no_proxy",
    )
    return ProxySettings(proxy=proxy, proxy_bypass=proxy_bypass)


def apply_proxy_environment(proxy_settings):
    managed_keys = {
        "AGENT_BROWSER_PROXY",
        "AGENT_BROWSER_PROXY_BYPASS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
    previous = {key: os.environ.get(key) for key in managed_keys}
    normalized = proxy_settings.build_environment({})

    for key in managed_keys:
        if key in normalized:
            os.environ[key] = normalized[key]
        else:
            os.environ.pop(key, None)

    return previous


def restore_proxy_environment(previous):
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def load_verified_text_fallback(
    verified_text_path,
    verified_report_date,
    fallback_reason=None,
):
    artifact_path = Path(verified_text_path).expanduser().resolve()
    if not artifact_path.exists():
        raise FileNotFoundError(f"已核验正文文件不存在: {artifact_path}")

    text_content = artifact_path.read_text(encoding="utf-8").strip()
    if not text_content:
        raise ValueError(f"已核验正文文件为空: {artifact_path}")

    source_report_date = format_report_date_cn(verified_report_date)
    return FetchResult(
        source_type="verified_text_fallback",
        source_name=artifact_path.name,
        source_report_date=source_report_date,
        artifact_path=artifact_path,
        text_content=text_content,
        fallback_reason=fallback_reason
        or f"显式沿用 {source_report_date} 最新已核验正文，不假设新一期内容不变",
    )


def determine_exit_code(exc):
    if not isinstance(exc, ArgusStageError):
        return 1
    return {
        "login": 2,
        "publications_download": 3,
        "article_fallback": 4,
        "price_fetch": 5,
        "report_generation": 6,
        "delivery": 7,
    }.get(exc.stage, 1)


def score_publication_candidate(candidate, target_date):
    text = (candidate.get("text") or "").lower()
    score = 0
    if "asia bitumen daily:" in text:
        score += 14
    elif "asia bitumen daily" in text:
        score += 10
    for token, weight in PUBLICATION_SEMANTIC_TOKENS:
        if token in text:
            score += weight
    if "news & analysis" in text:
        score -= 6
    if "latest news" in text:
        score -= 8
    if "us-iran war" in text:
        score -= 10
    if "argus asia bitumen daily" in text:
        score += 5
    candidate_date = extract_report_date_compact(candidate.get("text", ""))
    if filename_matches_target_date(candidate.get("text", ""), target_date):
        score += 8
    elif is_report_date_within_lag(candidate_date, target_date):
        lag_days = report_date_lag_days(candidate_date, target_date)
        if lag_days is not None:
            score += max(1, 6 - lag_days)
    elif candidate_date:
        score -= 8
    return score


def normalize_status_display(status, label=None):
    raw = str(status or "").strip()
    if not raw or raw == "-":
        return "-"

    compact = raw.replace(" ", "")
    if "持平" in compact:
        return "持平"

    numbers = re.findall(r"[-+]?\d+(?:\.\d+)?", compact)
    if numbers:
        try:
            if all(abs(float(number)) < 1e-9 for number in numbers):
                return "持平"
        except ValueError:
            pass

    sign = None
    if any(token in compact for token in ("上涨", "上升", "增加", "+", "▲")):
        sign = "▲"
    if any(token in compact for token in ("下跌", "下降", "减少", "▼")) or compact.startswith("-"):
        sign = "▼"

    numbers = re.findall(r"\d+(?:\.\d+)?", compact)
    if sign is None and numbers:
        if compact.startswith("-"):
            sign = "▼"
        elif "|" in compact or "%" in compact:
            sign = "▲"
    if numbers:
        value = numbers[0].rstrip("0").rstrip(".") if "." in numbers[0] else numbers[0]
        if sign:
            currency_symbol = detect_currency_symbol(label)
            if currency_symbol:
                return f"{sign} {currency_symbol}{value}"
            return f"{sign} {value}"

    if sign:
        return sign
    return raw


def format_status_from_delta(delta):
    try:
        delta_value = float(delta)
    except (TypeError, ValueError):
        return "-"

    if abs(delta_value) < 1e-9:
        return "持平"

    normalized = f"{abs(delta_value):f}".rstrip("0").rstrip(".")
    if delta_value > 0:
        return f"▲ {normalized}"
    return f"▼ {normalized}"


def compute_status_from_chart_item(item):
    try:
        previous = float(item.get("previous", 0) or 0)
        current = float(item.get("current", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return "-"
    return format_status_from_delta(current - previous)


def detect_currency_symbol(label):
    text = str(label or "").strip()
    if not text:
        return ""
    if any(token in text for token in ("FOB", "ABX", "新加坡", "韩国", "伊朗", "离岸")):
        return "$"
    if any(token in text for token in ("上海主力", "BU主力", "华东", "华南")):
        return "¥"
    return ""


def normalize_price_display(value, label=None):
    raw = str(value).strip() if value is not None else "-"
    if not raw or raw == "-":
        return "-"

    raw = (
        raw.replace("美元/吨", "")
        .replace("元/吨", "")
        .replace("美元", "")
        .replace("元", "")
        .strip()
    )
    compact = raw.replace(",", "").replace(" ", "")
    currency_symbol = detect_currency_symbol(label)
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", compact):
        numeric = f"{float(compact):f}"
        normalized = numeric.rstrip("0").rstrip(".")
        if currency_symbol:
            return f"{currency_symbol}{normalized}/吨"
        return normalized
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?-[-+]?\d+(?:\.\d+)?", compact):
        normalized = compact
        if currency_symbol:
            return f"{currency_symbol}{normalized}/吨"
        return normalized
    return raw


def format_price_html(price_text):
    text = str(price_text or "").strip()
    if not text or text == "-":
        return html.escape(text or "-")
    if text.endswith("/吨"):
        return f'{html.escape(text[:-2])}<span class="unit-text">/吨</span>'
    return html.escape(text)


def has_meaningful_value(value):
    text = str(value or "").strip()
    return text not in {"", "-", "未知", "None", "null"}


def parse_target_user_ids(raw_user_ids):
    if raw_user_ids is None:
        return []
    if isinstance(raw_user_ids, (list, tuple, set)):
        values = raw_user_ids
    else:
        values = [raw_user_ids]

    result = []
    for value in values:
        for part in re.split(r"[,，;\s]+", str(value or "").strip()):
            if part:
                result.append(part)
    return result


def extract_market_price_snapshot(card_text, region_label):
    lines = [
        line.strip()
        for line in str(card_text or "").splitlines()
        if line.strip()
    ]
    lines = [line for line in lines if line != region_label]

    price = "未知"
    change = "未知"

    for idx, line in enumerate(lines):
        compact = line.replace(",", "").replace(" ", "")
        if price == "未知" and re.fullmatch(r"\d+(?:\.\d+)?", compact):
            price = line.strip()
            for next_line in lines[idx + 1 :]:
                next_compact = next_line.replace(" ", "")
                if (
                    "%" in next_compact
                    or "|" in next_compact
                    or "上涨" in next_compact
                    or "下跌" in next_compact
                    or next_compact.startswith(("+", "-"))
                ):
                    change = next_line.strip()
                    break
            continue

        if change == "未知":
            if (
                "%" in compact
                or "|" in compact
                or "上涨" in compact
                or "下跌" in compact
                or compact.startswith(("+", "-"))
            ):
                change = line.strip()

    return price, change


def extract_market_price_from_section(section_text, region_label):
    lines = [
        line.strip()
        for line in str(section_text or "").splitlines()
        if line.strip()
    ]

    for idx, line in enumerate(lines):
        if line != region_label:
            continue

        remaining = lines[idx + 1 : idx + 4]
        price, change = extract_market_price_snapshot(
            "\n".join([region_label] + remaining), region_label
        )
        if has_meaningful_value(price):
            return price, change

    return "未知", "未知"


def status_css_class(status_text):
    text = str(status_text or "").strip()
    if text.startswith("▲"):
        return "status-up"
    if text.startswith("▼"):
        return "status-down"
    return "status-flat"


def normalize_forecast_reason(text, title, kind):
    raw = str(text or "").strip()
    if not raw or raw == "-":
        return "-"

    compact = raw.replace(" ", "")
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", compact):
        if kind == "support":
            return f"关注{compact}一线买盘承接，低位补库与现货成本通常会提供支撑。"
        return f"关注{compact}一线的上方压力，报盘抬升与获利了结可能限制继续上冲。"

    return raw


def extract_news_candidates_from_source_text(source_text, limit=3):
    if not source_text:
        return []

    normalized = re.sub(r"\s+", " ", source_text)
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[\.\!\?])\s+", normalized)
        if sentence.strip()
    ]

    ranked = []
    for sentence in sentences:
        lowered = sentence.lower()
        score = sum(1 for token in NEWS_SENTENCE_HINTS if token in lowered)
        if score <= 0:
            continue
        if len(sentence) < 30 or len(sentence) > 240:
            continue
        ranked.append((score, sentence))

    ranked.sort(key=lambda item: (-item[0], sentences.index(item[1])))

    selected = []
    seen = set()
    for _, sentence in ranked:
        if sentence in seen:
            continue
        seen.add(sentence)
        selected.append(sentence)
        if len(selected) >= limit:
            break
    return selected


def article_text_looks_like_target_report(article_text):
    text = (article_text or "").lower()
    if "asia bitumen daily" not in text:
        return False

    bitumen_signals = sum(
        1
        for token in (
            "singapore",
            "south korea",
            "bitumen",
            "prices rise",
            "prices fall",
            "fob",
            "buying interest",
        )
        if token in text
    )
    generic_news_signals = sum(
        1
        for token in (
            "latest news",
            "top headlines",
            "round-up of the latest argus news stories",
            "news & analysis",
        )
        if token in text
    )
    return bitumen_signals >= 3 and generic_news_signals == 0


def select_publication_candidate(candidates, target_date):
    """按语义和日期综合打分选出最佳候选项。"""
    if not candidates:
        return None

    def lag_rank(candidate):
        lag = report_date_lag_days(
            extract_report_date_compact(candidate.get("text", "")), target_date
        )
        return -999 if lag is None else -lag

    ranked = sorted(
        candidates,
        key=lambda candidate: (
            score_publication_candidate(candidate, target_date),
            1 if filename_matches_target_date(candidate.get("text", ""), target_date) else 0,
            lag_rank(candidate),
            -candidates.index(candidate),
        ),
    )
    best = ranked[-1]
    if score_publication_candidate(best, target_date) <= 0:
        return None
    return best


def build_source_metadata(
    source_type,
    source_label,
    source_report_date,
    source_name,
    fallback_reason=None,
):
    """统一构造报告页脚里的源信息标注。"""
    mode_label = {
        "argus_pdf": "Argus PDF",
        "argus_direct_article_fallback": "Argus Direct 文章回退",
        "verified_text_fallback": "已核验正文回退",
    }.get(source_type, source_type)

    note_parts = [f"来源: {mode_label}", f"引用日期: {source_report_date or '-'}"]
    show_fallback_reason = source_type != "argus_pdf"
    if fallback_reason and show_fallback_reason:
        note_parts.append(f"回退原因: {fallback_reason}")

    return {
        "argus_source_date_note": " | ".join(note_parts),
        "argus_source_file": f"{source_name or source_label or '-'}、隆众资讯",
    }


def build_source_prompt_context(fetch_result):
    if fetch_result and fetch_result.source_type == "verified_text_fallback":
        source_date = fetch_result.source_report_date or "-"
        return (
            "已核验 Argus 正文文本（非当日新一期正文）",
            (
                f"补充约束：当前供需口径只能显式沿用 {source_date} 最新已核验正文；"
                "不要将其表述为今日新一期已确认内容，不要假设新一期内容不变；"
                "若引用供需、成交、装船或区域流向，请写成基于已核验正文的沿用口径。"
            ),
        )

    if fetch_result and fetch_result.source_type == "argus_direct_article_fallback":
        return (
            "Argus Direct 文章正文内容",
            "补充约束：正文来自 Argus Direct 文章页回退，请仅依据原文可确认信息输出，不要补写未验证的新一期细节。",
        )

    return (
        "最新的英文沥青日报源文本内容（若来源为 PDF，则为 PDF 提取文本）",
        "补充约束：请严格依据源文本输出，不要补写原文中未确认的内容。",
    )


def iter_document_surfaces(page):
    """返回主页面及其嵌套 frame，避免站点把内容迁移到 iframe 后完全失明。"""
    if page is None:
        return []

    surfaces = []
    seen_ids = set()
    queue = [page]

    while queue:
        surface = queue.pop(0)
        marker = id(surface)
        if marker in seen_ids:
            continue
        seen_ids.add(marker)
        surfaces.append(surface)

        child_frames = getattr(surface, "frames", None) or []
        for frame in child_frames:
            if frame is surface:
                continue
            queue.append(frame)

    return surfaces


def discover_followup_urls(base_url, html_text):
    """从 HTML/内联脚本里提取后续可尝试的 URL。"""
    discovered = []
    seen = set()

    def push(raw_url):
        if not raw_url:
            return
        absolute = urljoin(base_url, html.unescape(raw_url))
        lowered = absolute.lower()
        if absolute in seen:
            return
        if not any(
            token in lowered
            for token in (
                ".pdf",
                "publication",
                "download",
                "integration",
                "api",
            )
        ):
            return
        seen.add(absolute)
        discovered.append(absolute)

    for match in re.findall(r'(?:src|href)=["\']([^"\']+)["\']', html_text or "", flags=re.I):
        push(match)

    for match in re.findall(
        r'["\'](https?:\/\/[^"\']+|\/[^"\']*(?:pdf|publication|download|integration|api)[^"\']*)["\']',
        html_text or "",
        flags=re.I,
    ):
        push(match)

    return discovered


def extract_filename_from_content_disposition(header_value):
    if not header_value:
        return None

    utf8_match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", header_value, flags=re.I)
    if utf8_match:
        return unquote(utf8_match.group(1))

    plain_match = re.search(r'filename\s*=\s*"([^"]+)"', header_value, flags=re.I)
    if plain_match:
        return plain_match.group(1)

    plain_match = re.search(r"filename\s*=\s*([^;]+)", header_value, flags=re.I)
    if plain_match:
        return plain_match.group(1).strip()

    return None


def resolve_agent_browser_executable_path():
    explicit = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH")
    if explicit and Path(explicit).exists():
        return explicit

    candidate_roots = [
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ]
    candidates = []
    for root in candidate_roots:
        if not root.exists():
            continue
        candidates.extend(
            root.glob("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell")
        )
        candidates.extend(root.glob("chromium-*/chrome-*/chrome"))

    resolved = sorted(path for path in candidates if path.exists())
    return str(resolved[-1]) if resolved else None


def unpack_agent_browser_json(payload):
    if isinstance(payload, dict) and "success" in payload:
        data = payload.get("data")
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data
    return payload


def build_agent_browser_render_metrics_script():
    return """
(() => {
  const container = document.getElementById('report-container') || document.body;
  const rect = container.getBoundingClientRect();
  const fontFamilies = Array.from(document.querySelectorAll('body, body *'))
    .slice(0, 30)
    .map((el) => window.getComputedStyle(el).fontFamily);
  return {
    title: document.title,
    bodyFontFamily: window.getComputedStyle(document.body).fontFamily,
    fontStatus: document.fonts ? document.fonts.status : 'unsupported',
    sampleTextCheck: document.fonts
      ? document.fonts.check('16px "Noto Sans SC Embedded"', '中文沥青价格测试')
      : false,
    containerWidth: Math.ceil(rect.width),
    containerHeight: Math.ceil(container.scrollHeight),
    fontFamilies,
  };
})();
"""


def build_agent_browser_image_prepare_script():
    return """
(() => {
  const container = document.getElementById('report-container') || document.body;
  const rect = container.getBoundingClientRect();
  const width = Math.ceil(rect.width);
  const height = Math.ceil(container.scrollHeight);
  document.documentElement.style.background = '#ffffff';
  document.body.style.background = '#ffffff';
  document.body.style.margin = '0';
  document.body.style.padding = '0';
  document.body.style.width = `${width}px`;
  document.body.style.minWidth = `${width}px`;
  container.style.margin = '0';
  return { width, height };
})();
"""


def build_exact_link_href_script(link_text):
    return f"""
(() => {{
  const target = {json.dumps(link_text)};
  const links = Array.from(document.querySelectorAll('a'));
  const match = links.find((link) => (link.innerText || '').trim() === target && link.href);
  return match ? match.href : null;
}})();
"""


def build_market_price_section_script():
    return """
(() => {
  const sections = Array.from(document.querySelectorAll('div, section'));
  const match = sections.find((section) => {
    const text = (section.innerText || '').trim();
    return /沥青市场价格|Market Price/i.test(text) && /华东/.test(text) && /华南/.test(text);
  });
  return match ? match.innerText : '';
})();
"""


def build_publication_api_payload(publication_id, target_date):
    return {
        "publicationId": int(publication_id),
        "publicationDate": datetime.strptime(target_date, "%Y%m%d").strftime(
            "%Y-%m-%dT00:00:00.0000000Z"
        ),
        "language": "en-GB",
    }


def compute_single_page_pdf_size(content_width_px, content_height_px, padding_px=80):
    width = max(900, int(content_width_px) + padding_px)
    height = max(1200, int(content_height_px) + padding_px)
    return f"{width}px", f"{height}px"


def resolve_chinese_font_paths(font_dir=DEFAULT_FONT_DIR):
    """返回可嵌入报告 HTML 的中文字体 URI。"""
    font_dir = Path(font_dir)
    resolved = {}

    for weight, candidates in FONT_FILE_CANDIDATES.items():
        for candidate in candidates:
            font_path = font_dir / candidate
            if font_path.exists():
                resolved[weight] = font_path.resolve().as_uri()
                break

    return resolved


def font_source_to_css_url(font_source):
    """把本地字体文件转换成可嵌入 CSS 的 data URL。"""
    if not font_source:
        return None
    if str(font_source).startswith("data:"):
        return str(font_source)

    if str(font_source).startswith("file://"):
        font_path = Path(font_source.removeprefix("file://"))
    else:
        font_path = Path(font_source)

    font_bytes = font_path.read_bytes()
    mime_type, _ = mimetypes.guess_type(font_path.name)
    if not mime_type:
        suffix = font_path.suffix.lower()
        mime_type = "font/otf" if suffix == ".otf" else "font/ttf"

    encoded = base64.b64encode(font_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def build_embedded_font_css(font_sources):
    """构造报告渲染专用字体 CSS，优先使用项目内置字体。"""
    font_faces = []
    if font_sources.get("regular"):
        regular_src = font_source_to_css_url(font_sources["regular"])
        font_faces.append(
            "\n".join(
                [
                    "@font-face {",
                    '    font-family: "Noto Sans SC Embedded";',
                    "    font-style: normal;",
                    "    font-weight: 400;",
                    "    font-display: swap;",
                    f'    src: url("{regular_src}");',
                    "}",
                ]
            )
        )

    if font_sources.get("bold"):
        bold_src = font_source_to_css_url(font_sources["bold"])
        font_faces.append(
            "\n".join(
                [
                    "@font-face {",
                    '    font-family: "Noto Sans SC Embedded";',
                    "    font-style: normal;",
                    "    font-weight: 700;",
                    "    font-display: swap;",
                    f'    src: url("{bold_src}");',
                    "}",
                ]
            )
        )

    font_faces.append(
        "\n".join(
            [
                "*, *::before, *::after {",
                f"    font-family: {FALLBACK_FONT_STACK} !important;",
                "}",
                "html, body {",
                f"    font-family: {FALLBACK_FONT_STACK} !important;",
                "}",
            ]
        )
    )

    return "\n\n".join(font_faces)


def inject_pdf_font_styles(html_content, font_css):
    """把报告渲染用的字体样式注入到 HTML 中。"""
    style_tag = f'\n<style id="pdf-font-fallbacks">\n{font_css}\n</style>\n'
    if "</head>" in html_content:
        return html_content.replace("</head>", f"{style_tag}</head>", 1)
    return f"{style_tag}{html_content}"


def remove_pdf_ignored_elements(html_content):
    """移除只应保留在交互页面中的节点，例如下载按钮。"""
    html_content = re.sub(
        r'\s*<button class="download-btn"[^>]*?>.*?</button>\s*',
        "\n",
        html_content,
        flags=re.DOTALL,
    )
    return html_content


def render_html_image_via_agent_browser(
    runner,
    html_path,
    output_image_path,
    session_name,
):
    source_html_path = Path(html_path).resolve()
    output_image_path = Path(output_image_path).resolve()
    wants_jpeg = output_image_path.suffix.lower() in {".jpg", ".jpeg"}
    screenshot_path = (
        prepare_output_path(output_image_path.with_name(f"{output_image_path.stem}.capture.png"))
        if wants_jpeg
        else output_image_path
    )

    runner(
        ["open", source_html_path.as_uri()],
        timeout=60,
        allow_file_access=True,
        session=session_name,
    )
    for _ in range(20):
        status = runner(
            ["eval", "--stdin"],
            json_output=True,
            timeout=20,
            stdin_text="document.fonts ? document.fonts.status : 'unsupported'",
            allow_file_access=True,
            session=session_name,
        )
        if status == "loaded":
            break
        runner(["wait", "500"], timeout=10, allow_file_access=True, session=session_name)

    metrics = runner(
        ["eval", "--stdin"],
        json_output=True,
        timeout=20,
        stdin_text=build_agent_browser_render_metrics_script(),
        allow_file_access=True,
        session=session_name,
    )
    render_width, render_height = compute_single_page_pdf_size(
        metrics["containerWidth"], metrics["containerHeight"]
    )
    prepared = runner(
        ["eval", "--stdin"],
        json_output=True,
        timeout=20,
        stdin_text=build_agent_browser_image_prepare_script(),
        allow_file_access=True,
        session=session_name,
    )
    prepared_width = int(prepared["width"])
    prepared_height = int(prepared["height"])
    runner(
        ["set", "viewport", str(prepared_width), str(min(max(prepared_height, 900), 4000))],
        timeout=20,
        allow_file_access=True,
        session=session_name,
    )
    runner(
        ["screenshot", str(screenshot_path)],
        timeout=60,
        allow_file_access=True,
        session=session_name,
        full_page=True,
    )
    if wants_jpeg:
        convert_image_to_jpeg(screenshot_path, output_image_path)
        try:
            screenshot_path.unlink()
        except FileNotFoundError:
            pass

    metrics["imageSize"] = {"width": render_width, "height": render_height}
    metrics["captureBox"] = {"width": prepared_width, "height": prepared_height}
    return metrics


def extract_json_payload(response_text):
    """从模型输出中提取 JSON 对象。"""
    cleaned = response_text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("模型输出中未找到有效 JSON 对象")
    candidate = cleaned[start : end + 1]

    replacements = {
        "“": '"',
        "”": '"',
        "‘": '"',
        "’": '"',
        "：": ":",
        "，": ",",
    }
    for source, target in replacements.items():
        candidate = candidate.replace(source, target)

    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    candidate = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", candidate)

    return json.loads(candidate)


def _format_inline_text(text):
    escaped = html.escape(text or "")
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def _render_paragraphs(paragraphs):
    if not paragraphs:
        return "<p>暂无数据。</p>"
    return "".join(f"<p>{_format_inline_text(paragraph)}</p>" for paragraph in paragraphs)


def _render_price_strip_items(items):
    blocks = []
    for item in items[:5]:
        status_text = normalize_status_display(
            item.get("status", "-"), label=item.get("label")
        )
        status_class = status_css_class(status_text)
        value_text = normalize_price_display(item.get("value", "-"), label=item.get("label"))
        blocks.append(
            "\n".join(
                [
                    '<div class="price-item">',
                    f'    <div class="item-label">{html.escape(item.get("label", "-"))}</div>',
                    f'    <div class="item-val">{format_price_html(value_text)}</div>',
                    f'    <div class="item-status {status_class}">{html.escape(status_text)}</div>',
                    "</div>",
                ]
            )
        )
    return "\n".join(blocks)


def _render_news_cards(items):
    accent_class = {
        "red": "card-red",
        "orange": "card-orange",
        "blue": "card-blue",
    }
    blocks = []
    for idx, item in enumerate(items[:3]):
        blocks.append(
            "\n".join(
                [
                    f'<div class="news-card {accent_class.get(item.get("accent"), ["card-red", "card-orange", "card-blue"][idx])}">',
                    f'    <div class="card-tag">{html.escape(item.get("tag", "动态"))}</div>',
                    f'    <div class="card-title">{html.escape(item.get("title", "-"))}</div>',
                    f'    <div class="card-desc">{_format_inline_text(item.get("desc", "-"))}</div>',
                    "</div>",
                ]
            )
        )
    return "\n".join(blocks)


def _render_chart_bars(chart_items):
    values = [float(item.get(key, 0) or 0) for item in chart_items[:3] for key in ("previous", "current")]
    max_value = max(values) if values else 1

    blocks = []
    for item in chart_items[:3]:
        previous = float(item.get("previous", 0) or 0)
        current = float(item.get("current", 0) or 0)
        previous_height = max(12, round(previous / max_value * 120)) if max_value else 12
        current_height = max(12, round(current / max_value * 120)) if max_value else 12
        blocks.append(
            "\n".join(
                [
                    '<div class="bar-group">',
                    f'    <div class="bar bar-bg-grey" style="height: {previous_height}px;">',
                    f'        <div class="bar-val val-grey">{html.escape(str(item.get("previous", "-")))}</div>',
                    "    </div>",
                    f'    <div class="bar bar-bg-red" style="height: {current_height}px;">',
                    f'        <div class="bar-val val-red">{html.escape(str(item.get("current", "-")))}</div>',
                    "    </div>",
                    "</div>",
                ]
            )
        )
    return "\n".join(blocks)


def _render_x_labels(chart_items):
    return "\n".join(
        f"<div>{html.escape(item.get('label', '-'))}</div>" for item in chart_items[:3]
    )


def _render_aw_items(items):
    blocks = []
    for item in items[:3]:
        blocks.append(
            "\n".join(
                [
                    '<div class="aw-item">',
                    f'    <div class="aw-item-title">{html.escape(item.get("title", "-"))}</div>',
                    f'    <div class="aw-item-desc">{_format_inline_text(item.get("desc", "-"))}</div>',
                    "</div>",
                ]
            )
        )
    return "\n".join(blocks)


def _render_forecasts(items):
    blocks = []
    for item in items[:3]:
        support_reason = normalize_forecast_reason(
            item.get("support", "-"), item.get("title", "-"), "support"
        )
        resistance_reason = normalize_forecast_reason(
            item.get("resistance", "-"), item.get("title", "-"), "resistance"
        )
        blocks.append(
            "\n".join(
                [
                    "<div>",
                    f'    <div class="fc-col-title">{html.escape(item.get("title", "-"))}</div>',
                    f'    <div class="fc-price">{html.escape(item.get("price_range", "-"))}</div>',
                    f'    <div class="fc-desc"><span class="fc-tag-green">支撑项:</span> {_format_inline_text(support_reason)}</div>',
                    f'    <div class="fc-desc"><span class="fc-tag-red">阻力项:</span> {_format_inline_text(resistance_reason)}</div>',
                    "</div>",
                ]
            )
        )
    return "\n".join(blocks)


def render_report_template(template_html, report_data):
    """使用固定模板渲染报告，避免模型输出任意 HTML/CSS。"""
    chart = report_data.get("chart", {})
    main_price_status = normalize_status_display(
        report_data.get("main_price_status", ""),
        label=report_data.get("contract", ""),
    )
    main_price_status_class = status_css_class(main_price_status)
    tokens = {
        "{{REPORT_DATE}}": html.escape(report_data.get("report_date", "")),
        "{{CONTRACT}}": html.escape(report_data.get("contract", "")),
        "{{MAIN_PRICE}}": (
            f'<span class="price-main-value">{format_price_html(normalize_price_display(report_data.get("main_price", ""), label=report_data.get("contract", "")))}</span>'
        ),
        "{{MAIN_PRICE_STATUS_HTML}}": f'<span class="{main_price_status_class}">{html.escape(main_price_status)}</span>',
        "{{PRICE_STRIP_ITEMS}}": _render_price_strip_items(report_data.get("price_strip", [])),
        "{{MARKET_SUMMARY_HTML}}": _render_paragraphs(report_data.get("market_summary", [])),
        "{{TRADE_DYNAMICS_HTML}}": _render_paragraphs(report_data.get("trade_dynamics", [])),
        "{{NEWS_CARDS}}": _render_news_cards(report_data.get("news", [])),
        "{{CHART_PREVIOUS_DATE}}": html.escape(chart.get("previous_date", "")),
        "{{CHART_CURRENT_DATE}}": html.escape(chart.get("current_date", "")),
        "{{CHART_BARS}}": _render_chart_bars(chart.get("items", [])),
        "{{CHART_X_LABELS}}": _render_x_labels(chart.get("items", [])),
        "{{ADVICE_ITEMS}}": _render_aw_items(report_data.get("advice", [])),
        "{{WARNING_ITEMS}}": _render_aw_items(report_data.get("warnings", [])),
        "{{FORECAST_COLUMNS}}": _render_forecasts(report_data.get("forecasts", [])),
        "{{FOOTER_DATE}}": html.escape(report_data.get("footer_date", "")),
        "{{ARGUS_SOURCE_DATE_NOTE}}": html.escape(report_data.get("argus_source_date_note", "")),
        "{{ARGUS_SOURCE_FILE}}": html.escape(report_data.get("argus_source_file", "")),
    }

    rendered = template_html
    for token, value in tokens.items():
        rendered = rendered.replace(token, value)
    return rendered


def normalize_report_data(report_data, today_date, prices=None):
    """补齐模板渲染所需字段，避免模型漏字段时直接渲染失败。"""
    price_strip = report_data.get("price_strip", [])[:5]
    while len(price_strip) < 5:
        price_strip.append({"label": "-", "value": "-", "status": "-"})

    label_aliases = {
        "华东": "华东地区批发",
        "华南": "华南地区批发",
    }
    for item in price_strip:
        item["label"] = label_aliases.get(item.get("label"), item.get("label"))

    if prices:
        price_map = {
            "华东": prices.get("huadong"),
            "华南": prices.get("huanan"),
            "华东地区批发": prices.get("huadong"),
            "华南地区批发": prices.get("huanan"),
        }
        for item in price_strip:
            market = price_map.get(item.get("label"))
            if market:
                scraped_price = market.get("price", item.get("value", "-"))
                scraped_change = market.get("change", item.get("status", "-"))
                if has_meaningful_value(scraped_price):
                    item["value"] = scraped_price
                if has_meaningful_value(scraped_change):
                    item["status"] = scraped_change

    chart = report_data.get("chart", {})
    chart_items = chart.get("items", [])[:3]
    while len(chart_items) < 3:
        chart_items.append({"label": "-", "previous": 0, "current": 0})

    chart_status_map = {}
    for item in chart_items:
        label = str(item.get("label", "")).strip()
        status = compute_status_from_chart_item(item)
        if label:
            chart_status_map[label] = status

    strip_status_aliases = {
        "FOB 新加坡 (ABX 1)": "新加坡 ABX 1",
        "FOB 韩国 (ABX 2)": "韩国离岸价",
        "FOB 伊朗 (散装)": "伊朗散装价",
    }
    for item in price_strip:
        if has_meaningful_value(item.get("status")):
            continue
        chart_label = strip_status_aliases.get(item.get("label"))
        if chart_label and has_meaningful_value(chart_status_map.get(chart_label)):
            item["status"] = chart_status_map[chart_label]

    def ensure_items(items, count):
        normalized = list(items[:count])
        while len(normalized) < count:
            normalized.append({"title": "-", "desc": "-"})
        return normalized

    forecasts = list(report_data.get("forecasts", [])[:3])
    while len(forecasts) < 3:
        forecasts.append(
            {"title": "-", "price_range": "-", "support": "-", "resistance": "-"}
        )

    main_price_status = normalize_status_display(
        report_data.get("main_price_status", ""),
        label=report_data.get("contract", ""),
    )
    if not has_meaningful_value(main_price_status):
        for preferred_label in ("新加坡 ABX 1", "韩国离岸价", "伊朗散装价"):
            candidate = chart_status_map.get(preferred_label)
            if has_meaningful_value(candidate):
                main_price_status = normalize_status_display(
                    candidate, label=report_data.get("contract", "")
                )
                break

    return {
        "report_date": report_data.get("report_date", today_date),
        "contract": report_data.get("contract", "上海主力合约 (BU主力)"),
        "main_price": report_data.get("main_price", "-"),
        "main_price_status": main_price_status,
        "price_strip": price_strip,
        "market_summary": report_data.get("market_summary", []),
        "trade_dynamics": report_data.get("trade_dynamics", []),
        "news": report_data.get("news", [])[:3],
        "chart": {
            "previous_date": chart.get("previous_date", ""),
            "current_date": chart.get("current_date", today_date),
            "items": chart_items,
        },
        "advice": ensure_items(report_data.get("advice", []), 3),
        "warnings": ensure_items(report_data.get("warnings", []), 3),
        "forecasts": forecasts,
        "footer_date": report_data.get("footer_date", today_date.replace("年", "-").replace("月", "-").replace("日", "")),
        "argus_source_date_note": report_data.get("argus_source_date_note", ""),
        "argus_source_file": report_data.get("argus_source_file", ""),
    }


def is_dingtalk_preview_image(file_name):
    return Path(file_name).suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


def send_file_to_dingtalk(file_path, target_user_ids=None):
    """将报告产物推送到一个或多个钉钉用户；图片按可预览图片消息发送。"""
    requests = import_requests()
    proxy_settings = resolve_proxy_settings()
    previous_proxy_env = {}
    user_id_list = parse_target_user_ids(target_user_ids)
    if not user_id_list:
        print("未提供有效的钉钉用户ID，跳过发送。")
        return False

    try:
        previous_proxy_env = apply_proxy_environment(proxy_settings)

        client_id = get_required_env("DINGTALK_APP_KEY")
        client_secret = get_required_env("DINGTALK_APP_SECRET")
        print(f"开始推送报告到钉钉，目标用户: {', '.join(user_id_list)}...")
        file_name = os.path.basename(file_path)
        guessed_type, _ = mimetypes.guess_type(file_name)
        mime_type = guessed_type or "application/octet-stream"
        file_type = Path(file_name).suffix.lower().lstrip(".") or "file"
        is_preview_image = is_dingtalk_preview_image(file_name)
        media_type = "image" if is_preview_image else "file"

        # 1. 获取 OAPI Access Token
        oapi_url = f"https://oapi.dingtalk.com/gettoken?appkey={client_id}&appsecret={client_secret}"
        oapi_res = requests.get(oapi_url).json()
        oapi_token = oapi_res.get("access_token")
        if not oapi_token:
            print(f"获取 OAPI Token 失败: {oapi_res}")
            return False

        # 2. 获取 New API Access Token
        new_api_url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        new_api_res = requests.post(
            new_api_url, json={"appKey": client_id, "appSecret": client_secret}
        ).json()
        new_token = new_api_res.get("accessToken")
        if not new_token:
            print(f"获取 New API Token 失败: {new_api_res}")
            return False

        # 3. 上传到钉钉媒体库。图片使用 type=image，后续可按图片消息直接预览。
        upload_url = f"https://oapi.dingtalk.com/media/upload?access_token={oapi_token}&type={media_type}"
        with open(file_path, "rb") as f:
            files = {"media": (file_name, f, mime_type)}
            upload_res = requests.post(upload_url, files=files).json()

        media_id = upload_res.get("media_id")
        if not media_id:
            print(f"媒体上传失败: {upload_res}")
            return False
        print(f"媒体上传成功, media_id: {media_id}")

        # 4. 发送消息。图片走 sampleImageMsg，避免在钉钉端显示为需下载的附件。
        send_url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        if is_preview_image:
            msg_key = "sampleImageMsg"
            msg_param = {"photoURL": media_id}
        else:
            msg_key = "sampleFile"
            msg_param = {
                "mediaId": media_id,
                "fileName": file_name,
                "fileType": file_type,
            }

        body = {
            "robotCode": client_id,
            "userIds": user_id_list,
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param),
        }
        headers = {
            "x-acs-dingtalk-access-token": new_token,
            "Content-Type": "application/json",
        }

        send_res = requests.post(send_url, json=body, headers=headers).json()
        if send_res.get("processQueryKey"):
            print(f"报告推送成功，标识: {send_res['processQueryKey']}")
            return True
        else:
            print(f"报告推送响应异常: {send_res}")
            return False

    except Exception as e:
        print(f"推送报告到钉钉时发生错误: {e}")
        return False
    finally:
        restore_proxy_environment(previous_proxy_env)


class ArgusDownloader:
    def __init__(
        self,
        headless=True,
        target_user_id=None,
        output_dir=None,
        verified_text_path=None,
        verified_report_date=None,
    ):
        self.headless = headless
        self.target_user_ids = parse_target_user_ids(target_user_id)
        self.output_dir = ensure_output_dir(output_dir)
        self.p = None
        self.browser = None
        self.context = None
        self.page = None
        self.warnings = []
        self.publication_id = os.environ.get(
            "ARGUS_ASIA_BITUMEN_PUBLICATION_ID", DEFAULT_ARGUS_PUBLICATION_ID
        )
        raw_verified_text_path = verified_text_path or os.environ.get("ARGUS_VERIFIED_TEXT_PATH")
        self.verified_text_path = (
            os.path.abspath(os.path.expanduser(raw_verified_text_path))
            if raw_verified_text_path
            else None
        )
        self.verified_report_date = (
            verified_report_date or os.environ.get("ARGUS_VERIFIED_TEXT_DATE")
        )
        self.agent_browser_executable_path = resolve_agent_browser_executable_path()
        self.agent_browser_session = (
            f"argus-publication-{self.publication_id}-{uuid.uuid4().hex[:8]}"
        )
        self.proxy_settings = resolve_proxy_settings()

    def publication_entrypoint_url(self):
        return f"https://direct.argusmedia.com/publication?publicationId={self.publication_id}"

    def integration_publication_url(self):
        return f"https://direct.argusmedia.com/integration/publication?publicationId={self.publication_id}"

    def _build_agent_browser_command(
        self,
        command_args,
        json_output=False,
        session=None,
        allow_file_access=False,
        full_page=False,
    ):
        command = ["agent-browser"]
        if self.agent_browser_executable_path:
            command.extend(["--executable-path", self.agent_browser_executable_path])
        command.extend(["--session", session or self.agent_browser_session])
        if self.proxy_settings.proxy:
            command.extend(["--proxy", self.proxy_settings.proxy])
        if self.proxy_settings.proxy_bypass:
            command.extend(["--proxy-bypass", self.proxy_settings.proxy_bypass])
        if allow_file_access:
            command.append("--allow-file-access")
        if full_page:
            command.append("--full")
        if json_output:
            command.append("--json")
        command.extend(command_args)
        return command

    def run_agent_browser(
        self,
        command_args,
        *,
        json_output=False,
        timeout=60,
        stdin_text=None,
        allow_file_access=False,
        session=None,
        full_page=False,
    ):
        command = self._build_agent_browser_command(
            command_args,
            json_output=json_output,
            allow_file_access=allow_file_access,
            session=session,
            full_page=full_page,
        )
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            input=stdin_text,
            timeout=timeout,
            env=self.proxy_settings.build_environment(),
        )
        stdout = completed.stdout.strip()
        if not json_output:
            return stdout
        if not stdout:
            return None
        return unpack_agent_browser_json(json.loads(stdout))

    def agent_browser_eval(self, script, *, timeout=60):
        return self.run_agent_browser(["eval", "--stdin"], json_output=True, timeout=timeout, stdin_text=script)

    def agent_browser_wait(self, wait_target, *, timeout=60):
        self.run_agent_browser(["wait", str(wait_target)], timeout=timeout)

    @staticmethod
    def current_report_date():
        return datetime.now().strftime("%Y%m%d")

    @staticmethod
    def current_run_dir_name():
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def expected_argus_report_date():
        return argus_expected_report_date(datetime.now())

    def get_target_dir(self):
        run_dir_name = self.current_run_dir_name()
        if self.output_dir:
            output_path = Path(self.output_dir)
            if output_path.name in {run_dir_name, self.current_report_date()}:
                target_dir = output_path
            else:
                target_dir = output_path / run_dir_name
        else:
            target_dir = Path(os.getcwd()) / run_dir_name

        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def capture_debug_artifacts(self, stage, error_message):
        """在失败阶段保存页面证据，便于后续排障。"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_dir = self.get_target_dir() / "_argus_debug" / f"{stamp}_{stage}"
        debug_dir.mkdir(parents=True, exist_ok=True)

        screenshot_path = debug_dir / "page.png"
        html_path = debug_dir / "page.html"
        meta_path = debug_dir / "meta.json"

        try:
            self.page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception as exc:
            print(f"保存截图失败: {exc}")

        try:
            html_path.write_text(self.page.content(), encoding="utf-8")
        except Exception as exc:
            print(f"保存 HTML 快照失败: {exc}")

        try:
            meta_path.write_text(
                json.dumps(
                    {
                        "stage": stage,
                        "error": error_message,
                        "url": self.page.url,
                        "title": self.page.title() if self.page else "",
                        "frames": [frame.url for frame in self.page.frames] if self.page else [],
                        "captured_at": datetime.now().isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"保存调试元数据失败: {exc}")

        print(f"已保存调试证据到: {debug_dir}")
        return debug_dir

    def add_warning(self, stage, message):
        warning = {"stage": stage, "message": str(message)}
        self.warnings.append(warning)
        print(f"[warning][{stage}] {message}")
        return warning

    def load_explicit_verified_text_fallback(self, fallback_reason):
        if not self.verified_text_path:
            return None
        if not self.verified_report_date:
            raise ArgusStageError(
                "article_fallback",
                "已提供已核验正文文件，但缺少核验日期 (--verified-report-date 或 ARGUS_VERIFIED_TEXT_DATE)",
            )

        try:
            return load_verified_text_fallback(
                verified_text_path=self.verified_text_path,
                verified_report_date=self.verified_report_date,
                fallback_reason=fallback_reason,
            )
        except Exception as exc:
            raise ArgusStageError("article_fallback", f"加载已核验正文回退失败: {exc}") from exc

    def _extract_candidate_title(self, candidate):
        text = candidate.get("text") or ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        for line in reversed(lines):
            if re.search(r"[A-Za-z]", line):
                return line
        return lines[-1]

    def is_expected_report_file(self, file_path, target_date):
        path = Path(file_path)
        if not path.exists() or path.suffix.lower() != ".pdf":
            return False

        if filename_matches_target_date(path.name, target_date):
            return True

        candidate_date = extract_report_date_compact(path.name)
        return is_report_date_within_lag(candidate_date, target_date)

    def _build_pdf_fetch_result(self, pdf_bytes, file_name, target_date, stage, fallback_reason=None):
        file_name = file_name or f"Argus Asia Bitumen Daily ({target_date}).pdf"
        download_path = prepare_output_path(self.get_target_dir() / Path(file_name).name)
        download_path.write_bytes(pdf_bytes)

        if not self.is_expected_report_file(download_path, target_date):
            raise ArgusStageError(
                stage,
                f"下载的 PDF 不在允许日期窗口内: {download_path.name}",
            )

        return FetchResult(
            source_type="argus_pdf",
            source_name=download_path.name,
            source_report_date=extract_report_date_from_filename(download_path.name),
            pdf_path=download_path,
            artifact_path=download_path,
            fallback_reason=fallback_reason,
        )

    def _build_pdf_fetch_result_from_response(self, response, target_date, stage, fallback_reason=None):
        headers = getattr(response, "headers", {}) or {}
        content_type = (headers.get("content-type") or headers.get("Content-Type") or "").lower()
        body = response.body()
        if "application/pdf" not in content_type and not (body or b"").startswith(b"%PDF-"):
            return None

        file_name = extract_filename_from_content_disposition(
            headers.get("content-disposition") or headers.get("Content-Disposition")
        )
        if not file_name:
            response_url = str(getattr(response, "url", "") or "")
            file_name = Path(urlparse(response_url).path).name or f"Argus Asia Bitumen Daily ({target_date}).pdf"

        return self._build_pdf_fetch_result(
            body,
            file_name=file_name,
            target_date=target_date,
            stage=stage,
            fallback_reason=fallback_reason,
        )

    def _build_pdf_fetch_result_from_http_response(self, response, target_date, stage, fallback_reason=None):
        headers = getattr(response, "headers", {}) or {}
        content_type = (headers.get("content-type") or headers.get("Content-Type") or "").lower()
        body = response.content or b""
        if "application/pdf" not in content_type and not body.startswith(b"%PDF-"):
            return None

        file_name = extract_filename_from_content_disposition(
            headers.get("content-disposition") or headers.get("Content-Disposition")
        )
        if not file_name:
            file_name = Path(urlparse(str(response.url)).path).name or f"Argus Asia Bitumen Daily ({target_date}).pdf"

        return self._build_pdf_fetch_result(
            body,
            file_name=file_name,
            target_date=target_date,
            stage=stage,
            fallback_reason=fallback_reason,
        )

    def _response_text_for_auth_probe(self, response):
        text = getattr(response, "text", "") or ""
        if isinstance(text, bytes):
            return text.decode("utf-8", errors="ignore")
        return str(text)

    def _response_indicates_auth_failure(self, response):
        if response is None:
            return False

        status_code = getattr(response, "status_code", None)
        if status_code in {401, 403}:
            return True

        final_url = str(getattr(response, "url", "") or "")
        if self._agent_browser_is_login_url(final_url):
            return True

        text = self._response_text_for_auth_probe(response).lower()
        return any(token in text for token in ("unauthorized", "sign in", "my account"))

    def _build_auth_failure_message(self, response, context):
        status_code = getattr(response, "status_code", None)
        final_url = str(getattr(response, "url", "") or "") or self.publication_entrypoint_url()
        if status_code == 401:
            status_label = "Unauthorized"
        elif status_code == 403:
            status_label = "Forbidden"
        elif self._agent_browser_is_login_url(final_url):
            status_label = "login page"
        else:
            status_label = "authentication shell"

        if status_code:
            return f"Argus 认证失败: {context} 返回 {status_code} {status_label} ({final_url})"
        return f"Argus 认证失败: {context} 命中 {status_label} ({final_url})"

    def _fetch_pdf_from_candidate_urls(self, candidate_urls, target_date, stage, fallback_reason=None):
        session = self._build_requests_session_from_agent_browser()
        attempted = set()

        for url in candidate_urls:
            if not url or url in attempted:
                continue
            attempted.add(url)
            try:
                response = session.get(url, timeout=30, allow_redirects=True)
            except Exception as exc:
                self.add_warning(stage, f"候选 URL 拉取失败: {url} -> {exc}")
                continue

            final_url = str(response.url)
            content_type = (response.headers.get("content-type") or "").lower()
            body = response.content or b""
            if "application/pdf" not in content_type and not body.startswith(b"%PDF-"):
                continue

            file_name = Path(urlparse(final_url).path).name or f"Argus Asia Bitumen Daily ({target_date}).pdf"
            return self._build_pdf_fetch_result(
                body,
                file_name=file_name,
                target_date=target_date,
                stage=stage,
                fallback_reason=fallback_reason,
            )

        return None

    def _agent_browser_publication_state(self):
        script = """
(() => {
  const frame = document.querySelector('#directWorkspacesIframe');
  const frameDoc = frame?.contentDocument ?? null;
  const frameText = frameDoc?.body?.innerText?.trim() ?? '';
  const frameHtml = frameDoc?.documentElement?.innerHTML ?? '';
  const heading = frameDoc?.querySelector('#publication-preview h2, h2')?.textContent?.trim() ?? '';
  const dateValue = frameDoc?.querySelector('#date-picker, #alt-date, input.date-input')?.value ?? '';
  return {
    href: window.location.href,
    hasIframe: !!frame,
    iframeSrc: frame?.src ?? '',
    hasEmail: !!document.querySelector('input[type="email"]'),
    hasPassword: !!document.querySelector('input[type="password"]'),
    hasPdfButton: !!frameDoc?.querySelector('#pdf-button'),
    iframeBlank: !!frame && !!frameDoc && frameText.length === 0,
    iframeError: frameHtml.includes('requireConfig') ? 'Script error for: requireConfig' : '',
    heading,
    dateValue,
  };
})();
"""
        return self.agent_browser_eval(script, timeout=30) or {}

    def _agent_browser_click_button_by_text(self, pattern):
        self.run_agent_browser(
            ["find", "role", "button", "click", "--name", pattern],
            timeout=30,
        )

    def _agent_browser_get_current_url(self):
        try:
            return self.run_agent_browser(["get", "url"], timeout=30)
        except Exception:
            return ""

    def _agent_browser_is_login_url(self, url):
        lowered = (url or "").lower()
        return "myaccount.argusmedia.com" in lowered or "/login" in lowered

    def _agent_browser_session_is_authenticated(self):
        try:
            session = self._build_requests_session_from_agent_browser()
        except Exception:
            return False
        try:
            response = session.get(
                self.publication_entrypoint_url(),
                timeout=15,
                allow_redirects=True,
            )
        except Exception:
            return False

        final_url = str(getattr(response, "url", "") or "")
        if self._agent_browser_is_login_url(final_url):
            return False
        return self.publication_entrypoint_url() in final_url

    def _agent_browser_login_argus(self):
        argus_email = get_required_env("ARGUS_EMAIL")
        argus_password = get_required_env("ARGUS_PASSWORD")

        print("正在使用 agent-browser 访问网站...")
        self.run_agent_browser(["open", self.publication_entrypoint_url()], timeout=90)
        self.agent_browser_wait(AGENT_BROWSER_WAIT_MS, timeout=15)
        deadline = time.time() + 30
        state = {}
        state_probe_failed = False
        while time.time() < deadline:
            try:
                state = self._agent_browser_publication_state()
            except Exception:
                state = {}
                state_probe_failed = True
            href = state.get("href") or self._agent_browser_get_current_url()
            if (
                state.get("hasEmail")
                or self._agent_browser_is_login_url(href)
                or self.publication_entrypoint_url() in href
            ):
                state["href"] = href
                break
            self.agent_browser_wait(1000, timeout=10)

        if state.get("hasEmail") or self._agent_browser_is_login_url(state.get("href", "")):
            self.agent_browser_wait('input[type="email"]', timeout=30)
            self.run_agent_browser(["fill", 'input[type="email"]', argus_email], timeout=30)
            self._agent_browser_click_button_by_text("Next")
            self.agent_browser_wait('input[type="password"]', timeout=30)
            self.run_agent_browser(["fill", 'input[type="password"]', argus_password], timeout=30)
            self._agent_browser_click_button_by_text("Sign in")

        deadline = time.time() + 60
        last_state = state
        while time.time() < deadline:
            try:
                state = self._agent_browser_publication_state()
            except Exception:
                state = {}
                state_probe_failed = True
            last_state = state
            href = state.get("href") or self._agent_browser_get_current_url()
            if (
                self.publication_entrypoint_url() in href
                and not state.get("hasEmail")
                and not state.get("hasPassword")
            ):
                if state_probe_failed and not self._agent_browser_session_is_authenticated():
                    raise ArgusStageError("login", "agent-browser 状态探测失败，且未建立可用登录态")
                state["href"] = href
                if state_probe_failed:
                    state["stateProbeFailed"] = True
                return state

            try:
                self._agent_browser_click_button_by_text("Yes")
            except Exception:
                pass
            self.agent_browser_wait(1000, timeout=10)

        last_href = last_state.get("href") or self._agent_browser_get_current_url()
        if (
            self.publication_entrypoint_url() in last_href
            and not last_state.get("hasEmail")
            and not last_state.get("hasPassword")
        ):
            if state_probe_failed and not self._agent_browser_session_is_authenticated():
                raise ArgusStageError("login", "agent-browser 状态探测失败，且未建立可用登录态")
            last_state["href"] = last_href
            if state_probe_failed:
                last_state["stateProbeFailed"] = True
            return last_state
        if last_state.get("hasEmail"):
            raise ArgusStageError("login", "agent-browser 登录后仍停留在邮箱输入页")
        if last_state.get("hasPassword"):
            raise ArgusStageError("login", "agent-browser 登录后仍停留在密码输入页")
        raise ArgusStageError("login", "agent-browser 登录超时，未进入 publication 页面")

    def _agent_browser_install_publication_capture(self):
        script = f"""
(() => {{
  const targetUrl = {json.dumps('/workspaces/api/publication')};
  const frame = document.querySelector('#directWorkspacesIframe');
  const windows = [window];
  if (frame?.contentWindow && frame.contentWindow !== window) {{
    windows.push(frame.contentWindow);
  }}

  for (const win of windows) {{
    if (win.__argusCaptureInstalled) {{
      continue;
    }}
    win.__argusCaptureInstalled = true;
    win.__argusCapture = null;

    const xhrProto = win.XMLHttpRequest?.prototype;
    if (xhrProto && !xhrProto.__argusWrapped) {{
      const originalOpen = xhrProto.open;
      const originalSend = xhrProto.send;
      const originalSetRequestHeader = xhrProto.setRequestHeader;
      xhrProto.open = function(method, url, ...rest) {{
        this.__argusRequest = {{ method, url, headers: {{}} }};
        return originalOpen.call(this, method, url, ...rest);
      }};
      xhrProto.setRequestHeader = function(name, value) {{
        if (this.__argusRequest) {{
          this.__argusRequest.headers[name] = value;
        }}
        return originalSetRequestHeader.call(this, name, value);
      }};
      xhrProto.send = function(body) {{
        if (this.__argusRequest && String(this.__argusRequest.url).includes(targetUrl)) {{
          const requestMeta = this.__argusRequest;
          this.addEventListener('load', () => {{
            win.__argusCapture = {{
              method: requestMeta.method,
              url: requestMeta.url,
              headers: requestMeta.headers,
              body: typeof body === 'string' ? body : null,
              status: this.status,
            }};
          }});
        }}
        return originalSend.call(this, body);
      }};
      xhrProto.__argusWrapped = true;
    }}

    if (win.fetch && !win.__argusFetchWrapped) {{
      const originalFetch = win.fetch.bind(win);
      win.fetch = async (...args) => {{
        const [resource, init] = args;
        const url = typeof resource === 'string' ? resource : resource?.url;
        const response = await originalFetch(...args);
        if (String(url || '').includes(targetUrl)) {{
          win.__argusCapture = {{
            method: init?.method || 'GET',
            url,
            headers: init?.headers || {{}},
            body: typeof init?.body === 'string' ? init.body : null,
            status: response.status,
          }};
        }}
        return response;
      }};
      win.__argusFetchWrapped = true;
    }}
  }}
  return true;
}})();
"""
        self.agent_browser_eval(script, timeout=30)

    def _agent_browser_trigger_publication_download(self):
        script = """
(() => {
  const frame = document.querySelector('#directWorkspacesIframe');
  const button = frame?.contentDocument?.querySelector('#pdf-button');
  if (!button) {
    throw new Error('pdf button not found');
  }
  button.click();
  return true;
})();
"""
        self.agent_browser_eval(script, timeout=30)

    def _agent_browser_poll_publication_capture(self):
        script = """
(() => {
  const frame = document.querySelector('#directWorkspacesIframe');
  return window.__argusCapture ?? frame?.contentWindow?.__argusCapture ?? null;
})();
"""
        deadline = time.time() + AGENT_BROWSER_CAPTURE_TIMEOUT_SECONDS
        while time.time() < deadline:
            capture = self.agent_browser_eval(script, timeout=15)
            if capture:
                return capture
            self.agent_browser_wait(1000, timeout=10)
        raise ArgusStageError("publications_download", "agent-browser 未捕获到 publication API 请求")

    def _build_requests_session_from_agent_browser(self):
        requests = import_requests()
        session = requests.Session()
        session.trust_env = True
        cookies_payload = self.run_agent_browser(["cookies", "get"], json_output=True, timeout=30) or {}
        cookies = cookies_payload.get("cookies", []) if isinstance(cookies_payload, dict) else cookies_payload
        for cookie in cookies or []:
            try:
                session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
            except Exception:
                continue
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "application/pdf,application/json,text/plain,*/*",
                "Origin": "https://direct.argusmedia.com",
                "Referer": self.publication_entrypoint_url(),
            }
        )
        return session

    def _fetch_publication_pdf_via_direct_api(self, target_date, fallback_reason):
        session = self._build_requests_session_from_agent_browser()
        request_url = "https://direct.argusmedia.com/workspaces/api/publication"
        request_body = json.dumps(build_publication_api_payload(self.publication_id, target_date))
        response = session.post(
            request_url,
            data=request_body,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if self._response_indicates_auth_failure(response):
            raise ArgusStageError(
                "login",
                self._build_auth_failure_message(response, "publication API"),
            )
        response.raise_for_status()
        result = self._build_pdf_fetch_result_from_http_response(
            response,
            target_date=target_date,
            stage="publications_download",
            fallback_reason=fallback_reason,
        )
        if not result:
            raise ArgusStageError("publications_download", "direct publication API 未返回有效 PDF")
        print(f"『Argus Asia Bitumen Daily』PDF 下载完成: {result.pdf_path}")
        return result

    def _download_publication_pdf_via_agent_browser(self, target_date):
        state = self._agent_browser_login_argus()
        print("已进入目标 Publications 页面: agent-browser")
        direct_label = "\n".join(
            part for part in ("Argus Asia Bitumen Daily", "Download PDF", state.get("dateValue")) if part
        )
        print(f"准备下载候选项: {direct_label}")
        try:
            return self._fetch_publication_pdf_via_direct_api(
                target_date,
                fallback_reason="agent-browser authenticated session",
            )
        except ArgusStageError as exc:
            if exc.stage == "login":
                raise
            self.add_warning(
                "publications_download",
                f"direct publication API 失败，回退到 legacy UI: {exc}",
            )
        except Exception as exc:
            self.add_warning(
                "publications_download",
                f"direct publication API 失败，回退到 legacy UI: {exc}",
            )

        self._agent_browser_install_publication_capture()
        try:
            self._agent_browser_trigger_publication_download()
            capture = self._agent_browser_poll_publication_capture()
        except Exception as exc:
            fallback_reason = f"legacy publication UI unavailable: {exc}"
            if state.get("iframeError"):
                fallback_reason = f"{fallback_reason}; {state['iframeError']}"
            return self._fetch_publication_pdf_via_direct_api(target_date, fallback_reason)

        session = self._build_requests_session_from_agent_browser()
        headers = capture.get("headers") or {}
        if isinstance(headers, list):
            headers = {item.get("name"): item.get("value") for item in headers if isinstance(item, dict)}
        request_headers = {
            key: value
            for key, value in headers.items()
            if key and key.lower() not in {"content-length", "cookie", "host"}
        }
        session.headers.update(request_headers)

        request_url = capture.get("url") or "https://direct.argusmedia.com/workspaces/api/publication"
        if request_url.startswith("/"):
            request_url = urljoin(self.publication_entrypoint_url(), request_url)
        request_body = capture.get("body") or json.dumps(
            build_publication_api_payload(self.publication_id, target_date)
        )

        response = session.request(
            method=(capture.get("method") or "POST").upper(),
            url=request_url,
            data=request_body,
            timeout=30,
        )
        if self._response_indicates_auth_failure(response):
            raise ArgusStageError(
                "login",
                self._build_auth_failure_message(response, "agent-browser publication API"),
            )
        response.raise_for_status()
        result = self._build_pdf_fetch_result_from_http_response(
            response,
            target_date=target_date,
            stage="publications_download",
            fallback_reason="agent_browser_publication_api",
        )
        if not result:
            raise ArgusStageError("publications_download", "agent-browser 请求未返回有效 PDF")
        print(f"『Argus Asia Bitumen Daily』PDF 下载完成: {result.pdf_path}")
        return result

    def fetch_publication_pdf_via_http_fallback(self, target_date, fallback_reason):
        print(f"进入认证 HTTP 回退流程，原因: {fallback_reason}")
        session = self._build_requests_session_from_agent_browser()

        seed_urls = [
            f"https://direct.argusmedia.com/publication?publicationId={self.publication_id}",
            f"https://direct.argusmedia.com/integration/publication?publicationId={self.publication_id}",
        ]
        pending_urls = list(seed_urls)
        attempted = set()

        while pending_urls and len(attempted) < 8:
            url = pending_urls.pop(0)
            if url in attempted:
                continue
            attempted.add(url)

            try:
                response = session.get(url, timeout=30, allow_redirects=True)
            except Exception as exc:
                self.add_warning("article_fallback", f"HTTP 回退请求失败: {url} -> {exc}")
                continue

            final_url = str(response.url)
            content_type = (response.headers.get("content-type") or "").lower()
            body = response.content or b""

            if self._response_indicates_auth_failure(response):
                raise ArgusStageError(
                    "login",
                    self._build_auth_failure_message(response, "HTTP publication fallback"),
                )

            if "application/pdf" in content_type or body.startswith(b"%PDF-"):
                file_name = (
                    Path(urlparse(final_url).path).name
                    or f"Argus Asia Bitumen Daily ({target_date}).pdf"
                )
                download_path = prepare_output_path(self.get_target_dir() / file_name)
                download_path.write_bytes(body)

                if not self.is_expected_report_file(download_path, target_date):
                    raise ArgusStageError(
                        "article_fallback",
                        f"HTTP 回退下载到了非目标窗口内的 PDF: {download_path.name}",
                    )

                print(f"认证 HTTP 回退下载成功: {download_path}")
                return FetchResult(
                    source_type="argus_pdf",
                    source_name=download_path.name,
                    source_report_date=extract_report_date_from_filename(download_path.name),
                    pdf_path=download_path,
                    artifact_path=download_path,
                    fallback_reason=fallback_reason,
                )

            html_text = response.text or ""
            lowered = html_text.lower()
            if "chrome-error" in lowered:
                self.add_warning(
                    "article_fallback",
                    f"HTTP 回退命中认证/壳页面: {final_url}",
                )
                continue

            discovered = [
                url
                for url in discover_followup_urls(final_url, html_text)
                if url not in attempted and url not in pending_urls
            ]
            pending_urls.extend(discovered[:5])

        raise ArgusStageError("article_fallback", "认证 HTTP 回退失败: 未解析到可下载 PDF")

    def download_publication_pdf(self, target_date):
        try:
            return self._download_publication_pdf_via_agent_browser(target_date)
        except ArgusStageError as exc:
            if exc.stage == "login":
                raise
            self.add_warning("publications_download", f"agent-browser 精确路径失败，先回退到 direct API: {exc}")
            try:
                return self._fetch_publication_pdf_via_direct_api(
                    target_date,
                    fallback_reason=f"agent-browser direct path failed: {exc}",
                )
            except ArgusStageError as direct_api_exc:
                if direct_api_exc.stage == "login":
                    raise
                self.add_warning(
                    "publications_download",
                    f"direct publication API 回退失败，继续回退到 HTTP: {direct_api_exc}",
                )
            return self.fetch_publication_pdf_via_http_fallback(
                target_date,
                fallback_reason=f"agent-browser direct path failed: {exc}",
            )
        except Exception as exc:
            self.add_warning("publications_download", f"agent-browser 精确路径失败，先回退到 direct API: {exc}")
            try:
                return self._fetch_publication_pdf_via_direct_api(
                    target_date,
                    fallback_reason=f"agent-browser direct path failed: {exc}",
                )
            except Exception as direct_api_exc:
                self.add_warning(
                    "publications_download",
                    f"direct publication API 回退失败，继续回退到 HTTP: {direct_api_exc}",
                )
            return self.fetch_publication_pdf_via_http_fallback(
                target_date,
                fallback_reason=f"agent-browser direct path failed: {exc}",
            )

    def fetch_article_fallback(self, target_date, fallback_reason):
        """下载失败时，尝试打开 Argus Direct 文章页并提取正文文本。"""
        print(f"进入 Argus Direct 文章回退流程，原因: {fallback_reason}")
        self._agent_browser_login_argus()
        article_text = self.agent_browser_eval(
            """
(() => {
  const frame = document.querySelector('#directWorkspacesIframe');
  const frameDoc = frame?.contentDocument ?? null;
  if (!frameDoc) return '';
  const selectors = ['article', 'main', '[role="main"]', 'body'];
  for (const selector of selectors) {
    const text = frameDoc.querySelector(selector)?.innerText?.trim() ?? '';
    if (text.length >= 500) return text;
  }
  return frameDoc.body?.innerText?.trim() ?? '';
})();
""",
            timeout=30,
        ) or ""

        if len(article_text) < 200:
            raise ArgusStageError("article_fallback", "文章回退失败: 页面正文提取长度不足")
        if not article_text_looks_like_target_report(article_text):
            raise ArgusStageError("article_fallback", "文章回退失败: 命中了非 Asia bitumen daily 正文页面")

        artifact_path = prepare_output_path(
            self.get_target_dir() / f"Argus_Asia_Bitumen_Daily_{target_date}_article_fallback.txt"
        )
        artifact_path.write_text(article_text, encoding="utf-8")
        print(f"已保存文章回退文本: {artifact_path}")

        return FetchResult(
            source_type="argus_direct_article_fallback",
            source_name="Argus Direct Article",
            source_report_date="-",
            artifact_path=artifact_path,
            text_content=article_text,
            fallback_reason=fallback_reason,
        )

    def get_asia_bitumen_daily(self):
        """获取亚洲沥青日报 (Argus Asia Bitumen Daily)"""
        print("准备下载: Argus Asia Bitumen Daily")
        target_date = self.expected_argus_report_date()
        fetch_result = None
        primary_error = None
        prices = None

        try:
            fetch_result = self.download_publication_pdf(target_date)
        except Exception as exc:
            primary_error = str(exc)
            self.add_warning("publications_download", primary_error)
            self.capture_debug_artifacts("publications_download", primary_error)
            if isinstance(exc, ArgusStageError) and exc.stage == "login":
                raise

        if fetch_result is None:
            try:
                fetch_result = self.fetch_article_fallback(
                    target_date, primary_error or "Publications download failed"
                )
            except Exception as exc:
                self.add_warning("article_fallback", str(exc))
                self.capture_debug_artifacts("article_fallback", str(exc))

        if fetch_result is None:
            try:
                fetch_result = self.fetch_publication_pdf_via_http_fallback(
                    target_date,
                    primary_error or "Publications/article fallback failed",
                )
            except Exception as exc:
                self.add_warning("article_fallback", str(exc))
                self.capture_debug_artifacts("article_fallback", str(exc))

        if fetch_result is None and self.verified_text_path:
            verified_date_cn = (
                format_report_date_cn(self.verified_report_date)
                if self.verified_report_date
                else "-"
            )
            fallback_reason = (
                f"{primary_error or 'Argus 自动抓取失败'}；"
                f"显式沿用 {verified_date_cn} 最新已核验正文，不假设新一期内容不变"
            )
            fetch_result = self.load_explicit_verified_text_fallback(fallback_reason)

        if fetch_result is None:
            raise ArgusStageError(
                "publications_download",
                "未获取到可用的 Argus 源内容",
            )

        # 获取隆众沥青价格
        try:
            prices = self.get_oilchem_asphalt_price()
        except Exception as exc:
            self.add_warning("price_fetch", str(exc))
            prices = None

        try:
            if fetch_result.pdf_path:
                final_pdf_path = self.generate_chinese_report(fetch_result.pdf_path, prices, fetch_result)
            else:
                final_pdf_path = self.generate_chinese_report_from_text(
                    fetch_result.text_content, prices, fetch_result
                )
        except Exception as exc:
            if isinstance(exc, ArgusStageError):
                raise
            raise ArgusStageError("report_generation", str(exc)) from exc

        if not final_pdf_path or not Path(final_pdf_path).exists():
            raise ArgusStageError("report_generation", "最终图片未生成成功")

        return final_pdf_path

    def generate_chinese_report(self, pdf_path, prices=None, fetch_result=None):
        """读取 PDF 内容并继续进入统一的中文报告生成流程。"""
        fitz = import_pymupdf()
        print("\n=== 开始生成中文版报告 ===")
        print("1. 正在提取PDF文本内容...")
        source_pdf_name = os.path.basename(pdf_path)
        source_report_date = extract_report_date_from_filename(source_pdf_name)
        try:
            doc = fitz.open(pdf_path)
            pdf_text = ""
            for page in doc:
                pdf_text += page.get_text()
        except Exception as e:
            raise ArgusStageError("report_generation", f"读取PDF失败: {e}") from e

        if fetch_result is None:
            fetch_result = FetchResult(
                source_type="argus_pdf",
                source_name=source_pdf_name,
                source_report_date=source_report_date,
                pdf_path=Path(pdf_path),
                artifact_path=Path(pdf_path),
            )

        return self.generate_chinese_report_from_text(pdf_text, prices, fetch_result)

    def generate_chinese_report_from_text(self, source_text, prices=None, fetch_result=None):
        """将 PDF 或文章正文统一转换为结构化中文报告并导出 JPEG 图片。"""
        if not source_text:
            raise ArgusStageError("report_generation", "缺少可用于生成报告的源文本")

        print("2. 正在读取 HTML 模板...")
        template_path = os.path.join(os.path.dirname(__file__), "hnxcl.html")
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                html_template = f.read()
        except Exception as e:
            raise ArgusStageError("report_generation", f"读取HTML模板失败: {e}") from e

        print("3. 正在调用大模型提取结构化中文内容 (这可能需要几十秒)...")
        api_key = get_required_env("LLM_API_KEY")
        base_url = get_required_env("LLM_BASE_URL")
        model_name = os.environ.get("MODEL_NAME", "gemini-3.1-flash-lite-preview")
        previous_proxy_env = {}

        try:
            previous_proxy_env = apply_proxy_environment(self.proxy_settings)
            OpenAI = import_openai_client()
            client = OpenAI(api_key=api_key, base_url=base_url)

            import base64
            from datetime import datetime

            # 获取当前日期，格式如 "2024年3月28日"
            today_date = datetime.now().strftime("%Y年%m月%d日")

            # 组装价格提示信息
            price_info = ""
            if prices and prices.get("huadong") and prices.get("huanan"):
                hd = prices["huadong"]
                hn = prices["huanan"]
                price_info = f"\n【今日隆众沥青市场价格参考】\n- 华东地区: 价格 {hd['price']}, 较前日变动 {hd['change']}\n- 华南地区: 价格 {hn['price']}, 较前日变动 {hn['change']}\n"

            news_candidates = extract_news_candidates_from_source_text(source_text, limit=5)
            news_candidates_prompt = "\n".join(
                f"- {candidate}" for candidate in news_candidates
            ) or "- 未识别到明确新闻句，请仅使用原文中能确认的事件。"

            source_kind_prompt, source_usage_prompt = build_source_prompt_context(fetch_result)

            prompt = f"""
你是一个专业的沥青行业分析师。
我将提供一份【{source_kind_prompt}】。请你提取核心数据，并输出一份严格符合指定 schema 的 JSON。

注意：
1. 只输出 JSON 对象，不要输出 markdown 代码块，不要输出任何解释。
2. 所有字段都必须返回；若原文没有明确信息，请填 "-" 或空数组。
3. `market_summary`、`trade_dynamics` 为字符串数组，每项是一段中文。
4. `news` 固定返回 3 条；`accent` 只能是 `red`、`orange`、`blue`。
5. `news` 只能围绕下方“候选行业动态原文”改写，不要引入原文中未出现的国家、公司、政策或事件。
6. `advice`、`warnings` 固定返回 3 条，每条包含 `title` 和 `desc`。
7. `forecasts` 固定返回 3 条，每条包含 `title`、`price_range`、`support`、`resistance`。
8. `support`、`resistance` 请写得专业、清晰、简洁，解释支撑/阻力的依据与洞察；不要只填数字价位，如果需要提到价位，请把价位写进说明里。
9. `chart.items` 固定返回 3 条，分别对应新加坡、韩国、伊朗；数值字段返回数字。
10. 报告日期使用 `{today_date}`。
11. 今日隆众沥青市场价格参考如下，请合并到 `price_strip` 中的“华东地区批发”“华南地区批发”项：{price_info}
12. `main_price` 与 `main_price_status` 必须填写上海主力合约的价格与相对前一交易日变动；若原文表述为持平，`main_price_status` 填 `持平`。
13. `price_strip` 中前 3 项必须填写 FOB 新加坡、韩国、伊朗的价格与相对前一日变动；`status` 统一填写 `▲数字`、`▼数字` 或 `持平`，不要留 `-`，除非原文完全没有该项数据。
14. `chart.previous_date` 必须填写前一交易日日期；`chart.items.current` 与 `chart.items.previous` 必须与前 3 项 FOB 价格变化保持一致。
15. {source_usage_prompt}

候选行业动态原文：
{news_candidates_prompt}

JSON schema:
{{
  "report_date": "{today_date}",
  "contract": "上海主力合约 (BU主力)",
  "main_price": "-",
  "main_price_status": "-",
  "price_strip": [
    {{"label": "FOB 新加坡 (ABX 1)", "value": "-", "status": "-"}},
    {{"label": "FOB 韩国 (ABX 2)", "value": "-", "status": "-"}},
    {{"label": "FOB 伊朗 (散装)", "value": "-", "status": "-"}},
    {{"label": "华东地区批发", "value": "-", "status": "-"}},
    {{"label": "华南地区批发", "value": "-", "status": "-"}}
  ],
  "market_summary": ["-"],
  "trade_dynamics": ["-"],
  "news": [
    {{"tag": "-", "title": "-", "desc": "-", "accent": "red"}},
    {{"tag": "-", "title": "-", "desc": "-", "accent": "orange"}},
    {{"tag": "-", "title": "-", "desc": "-", "accent": "blue"}}
  ],
  "chart": {{
    "previous_date": "-",
    "current_date": "{today_date}",
    "items": [
      {{"label": "新加坡 ABX 1", "previous": 0, "current": 0}},
      {{"label": "韩国离岸价", "previous": 0, "current": 0}},
      {{"label": "伊朗散装价", "previous": 0, "current": 0}}
    ]
  }},
  "advice": [
    {{"title": "-", "desc": "-"}},
    {{"title": "-", "desc": "-"}},
    {{"title": "-", "desc": "-"}}
  ],
  "warnings": [
    {{"title": "-", "desc": "-"}},
    {{"title": "-", "desc": "-"}},
    {{"title": "-", "desc": "-"}}
  ],
  "forecasts": [
    {{"title": "新加坡 (ABX 1)", "price_range": "-", "support": "关注低位买盘与区域现货成本支撑。", "resistance": "上方卖盘与需求疲软可能压制继续上涨。"}},
    {{"title": "韩国离岸 (ABX 2)", "price_range": "-", "support": "原料偏紧与供应收缩提供底部支撑。", "resistance": "东中国买兴不足限制高价成交。"}},
    {{"title": "伊朗离岸 (FOB)", "price_range": "-", "support": "南亚刚需采购仍对低位形成承接。", "resistance": "运费与地缘风险抬升后，高位追涨意愿有限。"}}
  ],
  "footer_date": "{today_date}"
}}

【最新英文源文本】：
{source_text}
"""
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            raw_model_output = response.choices[0].message.content.strip()
            raw_output_path = prepare_output_path(
                self.get_target_dir() / f"llm_raw_response_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            )
            raw_output_path.write_text(raw_model_output, encoding="utf-8")

            report_payload = extract_json_payload(raw_model_output)
            report_payload = normalize_report_data(report_payload, today_date, prices)
            report_payload["report_date"] = today_date
            report_payload["footer_date"] = today_date.replace("年", "-").replace("月", "-").replace("日", "")
            report_payload.update(
                build_source_metadata(
                    source_type=fetch_result.source_type if fetch_result else "argus_pdf",
                    source_label="Argus Asia Bitumen Daily",
                    source_report_date=fetch_result.source_report_date if fetch_result else "-",
                    source_name=fetch_result.source_name if fetch_result else "-",
                    fallback_reason=fetch_result.fallback_reason if fetch_result else None,
                )
            )
            new_html_content = render_report_template(html_template, report_payload)

            # 为图片渲染阶段显式注入中文字体，避免运行环境缺少系统字体导致乱码
            embedded_fonts = resolve_chinese_font_paths()
            font_css = build_embedded_font_css(embedded_fonts)
            new_html_content = inject_pdf_font_styles(
                new_html_content.strip(), font_css
            )
            new_html_content = remove_pdf_ignored_elements(new_html_content)

            if embedded_fonts:
                print(
                    f"-> 已注入内置中文字体: {', '.join(sorted(embedded_fonts.keys()))}"
                )
            else:
                print("-> 未找到内置中文字体文件，将依赖系统字体回退链")

            base_name = build_generated_report_stem(today_date)
            new_html_path = prepare_output_path(
                self.get_target_dir() / f"{base_name}_zh.html"
            )

            with open(new_html_path, "w", encoding="utf-8") as f:
                f.write(new_html_content.strip())
            print(f"-> 中文版 HTML 生成成功，已保存至: {new_html_path}")

            print("4. 正在使用 agent-browser 将 HTML 转换为 JPEG...")
            new_image_path = prepare_output_path(
                self.get_target_dir() / f"{base_name}_zh.jpg"
            )

            image_session = f"argus-html-image-{uuid.uuid4().hex[:8]}"
            image_metrics = render_html_image_via_agent_browser(
                self.run_agent_browser,
                html_path=new_html_path,
                output_image_path=new_image_path,
                session_name=image_session,
            )
            image_width = image_metrics["imageSize"]["width"]
            image_height = image_metrics["imageSize"]["height"]
            print(f"-> 图片渲染尺寸: width={image_width}, height={image_height}")

            print(f"-> 中文版 JPEG 生成成功，已保存至: {new_image_path}")

            # 仅在提供了投递目标时才尝试发送；缺少钉钉凭证时只记录 warning，不影响本地产物。
            if self.target_user_ids:
                delivered = send_file_to_dingtalk(str(new_image_path), self.target_user_ids)
                if delivered is False:
                    self.add_warning("delivery", "钉钉发送未成功，但图片已生成")
            else:
                print("未提供钉钉投递目标，跳过发送，仅保留本地产物。")

            print("=== 处理完成 ===\n")
            return new_image_path

        except Exception as e:
            raise ArgusStageError("report_generation", f"大模型调用或图片生成过程中发生错误: {e}") from e
        finally:
            restore_proxy_environment(previous_proxy_env)

    def get_prices_data(self):
        """获取首页 Prices 模块的表格数据"""
        print("正在获取 Prices 表格数据...")

        # 确保回到首页，或者确保页面加载完成
        # 因为 login 方法最后已经在首页了，这里直接等待 Prices 表格出现
        self.page.wait_for_selector("text=Prices")

        # 定位包含 Prices 标题的区域中的表格
        # 由于网页结构复杂，我们先定位到这个大容器，再找里面的行
        # 假设它是通过一个特定的容器包裹的，我们可以通过标题 "Prices" 找到它所在的最近一层父组件
        prices_section = (
            self.page.locator("div")
            .filter(has=self.page.locator("h2, h3, div", has_text="Prices"))
            .first
        )

        # 等待表格行加载
        self.page.wait_for_selector("table tr")

        # 获取所有的表格行 (这里可能需要根据实际页面的 HTML 结构调整，比如是 table/tbody/tr 还是 div 模拟的行)
        # 尝试通用选择器：寻找最近的表格
        rows = self.page.locator("table tr").all()

        if not rows:
            print("未找到标准的 table tr 元素，尝试其他结构...")
            # 某些网站使用 div 模拟表格 (ag-grid 等)
            rows = self.page.locator(".ag-row").all()

        print(f"共找到 {len(rows)} 行数据 (包含表头)。\n")

        data_list = []
        for i, row in enumerate(rows):
            # 获取该行内的所有单元格 (td/th 或者特定的 class)
            cells = row.locator("td, th, .ag-cell").all()
            row_data = [cell.inner_text().strip().replace("\n", " ") for cell in cells]

            # 过滤掉空行
            if any(row_data):
                data_list.append(row_data)
                print(f"第 {i + 1} 行: {row_data}")

        return data_list

    def get_oilchem_asphalt_price(self):
        """获取隆众沥青价格信息"""
        print("\n=== 开始获取隆众沥青价格 ===")
        print("1. 正在访问隆众能源网...")
        session_name = f"oilchem-asphalt-{uuid.uuid4().hex[:8]}"
        self.run_agent_browser(
            ["open", "https://oil.oilchem.net/oil/asphalt.shtml"],
            timeout=90,
            session=session_name,
        )
        self.run_agent_browser(["wait", "3000"], timeout=15, session=session_name)

        print("2. 解析'沥青'目标链接...")
        asphalt_href = self.run_agent_browser(
            ["eval", "--stdin"],
            json_output=True,
            timeout=20,
            stdin_text=build_exact_link_href_script("沥青"),
            session=session_name,
        )
        if asphalt_href:
            print("3. 直接打开'沥青'页面...")
            self.run_agent_browser(["open", asphalt_href], timeout=90, session=session_name)
            self.run_agent_browser(["wait", "5000"], timeout=15, session=session_name)
        else:
            print("3. 未找到精确'沥青'链接，继续使用当前页面。")

        print("4. 正在提取华东和华南价格及其变动...")
        result = {}
        try:
            market_price_text = self.run_agent_browser(
                ["eval", "--stdin"],
                json_output=True,
                timeout=20,
                stdin_text=build_market_price_section_script(),
                session=session_name,
            ) or ""
            if not market_price_text:
                raise RuntimeError("页面中未提取到沥青市场价格板块")

            huadong_price, huadong_change = extract_market_price_from_section(
                market_price_text, "华东"
            )
            result["huadong"] = {"price": huadong_price, "change": huadong_change}

            huanan_price, huanan_change = extract_market_price_from_section(
                market_price_text, "华南"
            )
            result["huanan"] = {"price": huanan_price, "change": huanan_change}

            print(
                f"-> 提取成功！\n【华东沥青】价格: {huadong_price}, 变动: {huadong_change}\n【华南沥青】价格: {huanan_price}, 变动: {huanan_change}"
            )
        except Exception as e:
            print(f"提取价格数据失败: {e}")

        print("=== 获取隆众沥青价格完成 ===\n")
        return result or None

    def close(self):
        """关闭浏览器和上下文"""
        try:
            self.run_agent_browser(["close"], timeout=15)
        except Exception:
            pass
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.p:
            self.p.stop()


def main():
    parser = argparse.ArgumentParser(description="Argus 报告下载工具")
    parser.add_argument(
        "--method",
        type=str,
        required=True,
        help="指定要执行的方法名称，例如: get_asia_bitumen_daily",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="是否使用无头模式(默认 True)",
    )
    parser.add_argument(
        "--user_id",
        type=str,
        default=None,
        help="指定要推送消息的钉钉用户ID；未提供时仅生成本地报告，不发送钉钉",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="指定报告输出根目录；源 PDF 与最终生成的 JPEG 图片会写入其中的运行当天日期目录（若传入的已是当天日期目录则直接使用）",
    )
    parser.add_argument(
        "--verified-text-path",
        type=str,
        default=None,
        help="显式指定已核验正文文本文件；仅在自动抓取全部失败时作为最终回退输入使用",
    )
    parser.add_argument(
        "--verified-report-date",
        type=str,
        default=None,
        help="已核验正文对应的报告日期，例如 2026-04-14；与 --verified-text-path 配套使用",
    )

    args = parser.parse_args()

    downloader = ArgusDownloader(
        headless=args.headless,
        target_user_id=args.user_id,
        output_dir=args.output_dir,
        verified_text_path=args.verified_text_path,
        verified_report_date=args.verified_report_date,
    )

    try:
        method_name = args.method
        if hasattr(downloader, method_name) and callable(
            getattr(downloader, method_name)
        ):
            print(f"正在执行方法: {method_name}")
            # 获取方法并执行
            func = getattr(downloader, method_name)
            final_pdf_path = func()
            print(
                json.dumps(
                    {
                        "status": "success",
                        "method": method_name,
                        "final_artifact_path": str(final_pdf_path) if final_pdf_path else None,
                        "final_pdf_path": str(final_pdf_path) if final_pdf_path and str(final_pdf_path).lower().endswith(".pdf") else None,
                        "warnings": downloader.warnings,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        else:
            print(f"错误: 找不到指定的方法 '{method_name}'。请检查方法名是否正确。")
            return 1

    except Exception as e:
        exit_code = determine_exit_code(e)
        stage = e.stage if isinstance(e, ArgusStageError) else "unknown"
        print(f"执行过程中发生错误[{stage}]: {e}")
        print(
            json.dumps(
                {
                    "status": "failed",
                    "stage": stage,
                    "message": str(e),
                    "exit_code": exit_code,
                },
                ensure_ascii=False,
            )
        )
        return exit_code
    finally:
        downloader.close()


if __name__ == "__main__":
    sys.exit(main())
