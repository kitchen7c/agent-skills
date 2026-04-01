import os
import sys
import argparse
import requests
import json
import base64
import mimetypes
import html
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from playwright.sync_api import sync_playwright
import fitz  # PyMuPDF，用于读取PDF内容
from openai import OpenAI  # 用于调用大模型


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


def get_required_env(name):
    value = os.environ.get(name)
    if value:
        return value
    raise RuntimeError(f"缺少必需环境变量: {name}")


def ensure_output_dir(output_dir):
    """规范化并确保输出目录存在。"""
    if not output_dir:
        return None
    normalized_dir = os.path.abspath(os.path.expanduser(output_dir))
    os.makedirs(normalized_dir, exist_ok=True)
    return normalized_dir


def prepare_output_path(path_like):
    """确保输出目录存在，并显式删除旧文件以保证覆盖。"""
    output_path = Path(path_like)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
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


class ArgusStageError(RuntimeError):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


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
    if filename_matches_target_date(candidate.get("text", ""), target_date):
        score += 8
    return score


def normalize_status_display(status):
    raw = str(status or "").strip()
    if not raw or raw == "-":
        return "-"

    compact = raw.replace(" ", "")
    if "持平" in compact:
        return "-"

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
            return f"{sign}{value}"

    if sign:
        return sign
    return raw


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

    ranked = sorted(
        candidates,
        key=lambda candidate: (score_publication_candidate(candidate, target_date), candidates.index(candidate)),
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
    }.get(source_type, source_type)

    note_parts = [f"来源: {mode_label}", f"引用日期: {source_report_date or '-'}"]
    if fallback_reason:
        note_parts.append(f"回退原因: {fallback_reason}")

    return {
        "argus_source_date_note": " | ".join(note_parts),
        "argus_source_file": source_name or source_label or "-",
    }


def compute_single_page_pdf_size(content_width_px, content_height_px, padding_px=80):
    width = max(900, int(content_width_px) + padding_px)
    height = max(1200, int(content_height_px) + padding_px)
    return f"{width}px", f"{height}px"


def resolve_chinese_font_paths(font_dir=DEFAULT_FONT_DIR):
    """返回可嵌入 PDF HTML 的中文字体 URI。"""
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
    """构造 PDF 打印专用字体 CSS，优先使用项目内置字体。"""
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
    """把 PDF 打印用的字体样式注入到 HTML 中。"""
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
    return json.loads(cleaned[start : end + 1])


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
        status_text = normalize_status_display(item.get("status", "-"))
        blocks.append(
            "\n".join(
                [
                    '<div class="price-item">',
                    f'    <div class="item-label">{html.escape(item.get("label", "-"))}</div>',
                    f'    <div class="item-val">{html.escape(str(item.get("value", "-")))}</div>',
                    f'    <div class="item-status">{html.escape(status_text)}</div>',
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
    tokens = {
        "{{REPORT_DATE}}": html.escape(report_data.get("report_date", "")),
        "{{CONTRACT}}": html.escape(report_data.get("contract", "")),
        "{{MAIN_PRICE}}": html.escape(str(report_data.get("main_price", ""))),
        "{{MAIN_PRICE_STATUS}}": html.escape(report_data.get("main_price_status", "")),
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

    if prices:
        price_map = {
            "华东": prices.get("huadong"),
            "华南": prices.get("huanan"),
        }
        for item in price_strip:
            market = price_map.get(item.get("label"))
            if market:
                item["value"] = market.get("price", item.get("value", "-"))
                item["status"] = market.get("change", item.get("status", "-"))

    chart = report_data.get("chart", {})
    chart_items = chart.get("items", [])[:3]
    while len(chart_items) < 3:
        chart_items.append({"label": "-", "previous": 0, "current": 0})

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

    return {
        "report_date": report_data.get("report_date", today_date),
        "contract": report_data.get("contract", "上海主力合约 (BU主力)"),
        "main_price": report_data.get("main_price", "-"),
        "main_price_status": normalize_status_display(
            report_data.get("main_price_status", "")
        ),
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


def send_pdf_to_dingtalk(file_path, target_user_id="42706"):
    """将文件推送到指定的钉钉用户"""
    client_id = get_required_env("DINGTALK_APP_KEY")
    client_secret = get_required_env("DINGTALK_APP_SECRET")

    try:
        print(f"开始推送文件到钉钉，目标用户: {target_user_id}...")

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

        # 3. 上传文件到钉钉媒体库
        upload_url = f"https://oapi.dingtalk.com/media/upload?access_token={oapi_token}&type=file"
        with open(file_path, "rb") as f:
            files = {"media": (os.path.basename(file_path), f, "application/pdf")}
            upload_res = requests.post(upload_url, files=files).json()

        media_id = upload_res.get("media_id")
        if not media_id:
            print(f"文件上传失败: {upload_res}")
            return False
        print(f"文件上传成功, media_id: {media_id}")

        # 4. 发送文件消息
        send_url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        msg_param = {
            "mediaId": media_id,
            "fileName": os.path.basename(file_path),
            "fileType": "pdf",
        }

        body = {
            "robotCode": client_id,
            "userIds": [target_user_id],
            "msgKey": "sampleFile",
            "msgParam": json.dumps(msg_param),
        }
        headers = {
            "x-acs-dingtalk-access-token": new_token,
            "Content-Type": "application/json",
        }

        send_res = requests.post(send_url, json=body, headers=headers).json()
        if send_res.get("processQueryKey"):
            print(f"文件推送成功，标识: {send_res['processQueryKey']}")
            return True
        else:
            print(f"文件推送响应异常: {send_res}")
            return False

    except Exception as e:
        print(f"推送文件到钉钉时发生错误: {e}")
        return False


class ArgusDownloader:
    def __init__(self, headless=True, target_user_id="42706", output_dir=None):
        self.headless = headless
        self.target_user_id = target_user_id
        self.output_dir = ensure_output_dir(output_dir)
        self.p = None
        self.browser = None
        self.context = None
        self.page = None
        self.warnings = []

    @staticmethod
    def current_report_date():
        return datetime.now().strftime("%Y%m%d")

    def get_target_dir(self):
        return Path(self.output_dir or os.getcwd())

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

    def _click_candidate_for_article(self, candidate):
        row = candidate["row"]
        click_targets = [
            lambda: row.get_by_role("link", name=re.compile(r"Argus Asia Bitumen Daily", re.I)).first,
            lambda: row.get_by_text("Argus Asia Bitumen Daily").first,
            lambda: row.locator("a, button").first,
        ]
        last_error = None
        for factory in click_targets:
            try:
                target = factory()
                target.click(timeout=5000)
                return
            except Exception as exc:
                last_error = exc
        try:
            row.click(timeout=5000)
            return
        except Exception as exc:
            last_error = exc
        raise RuntimeError(f"无法打开文章页: {last_error}")

    def _click_candidate_for_pdf(self, candidate):
        row = candidate["row"]
        click_targets = [
            lambda: row.get_by_role("link", name=re.compile(r"PDF|Download", re.I)).first,
            lambda: row.get_by_role("button", name=re.compile(r"PDF|Download", re.I)).first,
            lambda: row.get_by_text(re.compile(r"PDF|Download", re.I)).first,
            lambda: row.locator("a, button").filter(has_text=re.compile(r"PDF|Download", re.I)).first,
        ]
        last_error = None
        for factory in click_targets:
            try:
                target = factory()
                target.click(timeout=5000)
                return
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"无法点击 PDF/Download 动作: {last_error}")

    def open_publications_menu(self):
        """使用多套定位策略打开 Publications 菜单。"""
        strategies = [
            ("role_button", lambda: self.page.get_by_role("button", name=re.compile(r"Publications", re.I)).first),
            ("role_link", lambda: self.page.get_by_role("link", name=re.compile(r"Publications", re.I)).first),
            ("nav_text", lambda: self.page.locator("nav, header, [role='navigation']").get_by_text("Publications").first),
            ("page_text", lambda: self.page.get_by_text("Publications", exact=True).first),
        ]

        last_error = None
        for strategy_name, locator_factory in strategies:
            try:
                locator = locator_factory()
                locator.wait_for(state="visible", timeout=5000)
                locator.click(timeout=5000)
                self.page.wait_for_timeout(1000)
                print(f"已通过策略打开 Publications: {strategy_name}")
                return strategy_name
            except Exception as exc:
                last_error = exc

        raise ArgusStageError("publications_download", f"无法定位 Publications 菜单: {last_error}")

    def collect_publication_candidates(self):
        """按语义枚举与目标刊物相关的候选节点，兼容新版页面结构变化。"""
        rows = self.page.locator("div, li, tr, article, section").filter(
            has_text=re.compile(r"Argus|Bitumen|Daily|Report|Asia", re.I)
        )
        count = min(rows.count(), 60)
        candidates = []
        seen_texts = set()
        for index in range(count):
            row = rows.nth(index)
            try:
                text = row.inner_text(timeout=2000).strip()
            except Exception:
                continue
            if not text or text in seen_texts:
                continue
            score = score_publication_candidate({"text": text}, self.current_report_date())
            if score <= 0:
                continue
            seen_texts.add(text)
            candidates.append({"row": row, "text": text, "score": score})
        return candidates

    def find_today_report_download(self, target_date):
        candidates = self.collect_publication_candidates()
        selected = select_publication_candidate(candidates, target_date)
        if not selected:
            return None
        with self.page.expect_download() as download_info:
            selected["row"].get_by_text("PDF", exact=True).first.click()
        return download_info.value

    def download_publication_pdf(self, target_date):
        print("正在打开 Publications 菜单...")
        self.open_publications_menu()

        candidates = self.collect_publication_candidates()
        if not candidates:
            raise ArgusStageError("publications_download", "未枚举到任何 Argus Asia Bitumen Daily 候选项")

        print("已枚举到以下候选项:")
        for candidate in candidates:
            print(f"- {candidate['text']}")

        selected = select_publication_candidate(candidates, target_date)
        if not selected:
            raise ArgusStageError("publications_download", "未找到可用的 Argus Asia Bitumen Daily 候选项")

        print(f"准备下载候选项: {selected['text']}")
        try:
            with self.page.expect_download(timeout=15000) as download_info:
                self._click_candidate_for_pdf(selected)
            download = download_info.value
        except Exception as exc:
            raise ArgusStageError("publications_download", f"点击 PDF 下载失败: {exc}") from exc

        file_name = download.suggested_filename
        download_path = prepare_output_path(self.get_target_dir() / file_name)
        download.save_as(str(download_path))

        if not is_current_report_file(download_path, target_date):
            raise ArgusStageError("publications_download", f"下载的 PDF 日期不匹配: {download_path.name}")

        print(f"『Argus Asia Bitumen Daily』PDF 下载完成: {download_path}")
        return FetchResult(
            source_type="argus_pdf",
            source_name=file_name,
            source_report_date=extract_report_date_from_filename(file_name),
            pdf_path=download_path,
            artifact_path=download_path,
        )

    def fetch_article_fallback(self, target_date, fallback_reason):
        """下载失败时，尝试打开 Argus Direct 文章页并提取正文文本。"""
        print(f"进入 Argus Direct 文章回退流程，原因: {fallback_reason}")
        try:
            self.open_publications_menu()
        except Exception:
            pass

        candidates = self.collect_publication_candidates()
        selected = select_publication_candidate(candidates, target_date)
        if not selected:
            raise ArgusStageError("article_fallback", "文章回退失败: 未找到 Argus Asia Bitumen Daily 候选项")

        active_page = self.page
        try:
            with self.context.expect_page(timeout=5000) as new_page_info:
                self._click_candidate_for_article(selected)
            active_page = new_page_info.value
            active_page.wait_for_load_state("load")
        except Exception:
            self._click_candidate_for_article(selected)
            try:
                self.page.wait_for_load_state("load")
            except Exception:
                pass
            active_page = self.page

        article_text = ""
        for selector in ("article", "main", "[role='main']", "body"):
            try:
                article_text = active_page.locator(selector).first.inner_text(timeout=5000).strip()
            except Exception:
                article_text = ""
            if len(article_text) >= 500:
                break

        if len(article_text) < 200:
            raise ArgusStageError("article_fallback", "文章回退失败: 页面正文提取长度不足")
        if not article_text_looks_like_target_report(article_text):
            raise ArgusStageError("article_fallback", "文章回退失败: 命中了非 Asia bitumen daily 正文页面")

        artifact_path = prepare_output_path(
            self.get_target_dir() / f"Argus_Asia_Bitumen_Daily_{target_date}_article_fallback.txt"
        )
        artifact_path.write_text(article_text, encoding="utf-8")
        print(f"已保存文章回退文本: {artifact_path}")

        if active_page != self.page:
            active_page.close()

        return FetchResult(
            source_type="argus_direct_article_fallback",
            source_name="Argus Direct Article",
            source_report_date=extract_report_date_from_filename(selected["text"]),
            artifact_path=artifact_path,
            text_content=article_text,
            fallback_reason=fallback_reason,
        )

    def start_browser(self):
        """启动浏览器并进行基础设置"""
        self.p = sync_playwright().start()
        # 添加 args=['--start-maximized'] 配合 no_viewport=True 使浏览器窗口最大化（全屏）
        self.browser = self.p.chromium.launch(headless=True, args=["--start-maximized"])
        # 在 headless=True 模式下，--start-maximized 通常不起作用，因为没有实际的屏幕。
        # 所以必须强制指定一个较大的 viewport (分辨率)，让网页以桌面端布局渲染，而不是移动端/折叠态。
        self.context = self.browser.new_context(
            viewport={"width": 1920, "height": 1080}
        )
        self.page = self.context.new_page()

    def login(self):
        """执行登录操作"""
        argus_email = get_required_env("ARGUS_EMAIL")
        argus_password = get_required_env("ARGUS_PASSWORD")

        print("正在访问网站...")
        self.page.goto("https://direct.argusmedia.com/")

        # 1. 登录流程 - 输入邮箱
        self.page.wait_for_selector('input[type="email"]')
        self.page.fill('input[type="email"]', argus_email)
        self.page.get_by_role("button", name="Next").click()

        # 2. 登录流程 - 输入密码
        self.page.wait_for_selector('input[type="password"]')
        self.page.fill('input[type="password"]', argus_password)
        self.page.get_by_role("button", name="Sign in").click()

        print("登录成功，正在跳转页面...")

        # 尝试处理可能出现的 "Stay signed in?" (是否保持登录状态) 提示页面
        try:
            # 微软登录经常会问 "Stay signed in?"
            btn = self.page.locator(
                'input[type="submit"][value="Yes"], input[type="button"][value="Yes"], button:has-text("Yes"), input[value="是"], button:has-text("是")'
            ).first
            btn.wait_for(state="visible", timeout=5000)
            print("发现 '保持登录状态' 提示，正在点击确认...")
            btn.click()
        except Exception:
            pass

        # 避免使用 networkidle，因为它在有长连接或后台轮询的网站上极易超时
        # 改为等待页面加载完成即可，后续的操作会由 Playwright 的自动等待机制(auto-wait)处理
        self.page.wait_for_load_state("load")

    def get_asia_bitumen_daily(self):
        """获取亚洲沥青日报 (Argus Asia Bitumen Daily)"""
        print("准备下载: Argus Asia Bitumen Daily")
        target_date = self.current_report_date()
        fetch_result = None
        primary_error = None
        prices = None

        try:
            fetch_result = self.download_publication_pdf(target_date)
        except Exception as exc:
            primary_error = str(exc)
            self.add_warning("publications_download", primary_error)
            self.capture_debug_artifacts("publications_download", primary_error)

        if fetch_result is None:
            try:
                fetch_result = self.fetch_article_fallback(
                    target_date, primary_error or "Publications download failed"
                )
            except Exception as exc:
                self.capture_debug_artifacts("article_fallback", str(exc))
                raise

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
            raise ArgusStageError("report_generation", "最终 PDF 未生成成功")

        return final_pdf_path

    def generate_chinese_report(self, pdf_path, prices=None, fetch_result=None):
        """读取 PDF 内容并继续进入统一的中文报告生成流程。"""
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
        """将 PDF 或文章正文统一转换为结构化中文报告并导出 PDF。"""
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

        try:
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

            source_kind_prompt = (
                "最新的英文沥青日报PDF文本内容"
                if fetch_result and fetch_result.source_type == "argus_pdf"
                else "Argus Direct 文章正文内容"
            )

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
8. `support`、`resistance` 必须是中文短句，解释支撑/阻力的依据与洞察，不要只填数字价位；如果需要提到价位，请把价位写在句子里。
9. `chart.items` 固定返回 3 条，分别对应新加坡、韩国、伊朗；数值字段返回数字。
10. 报告日期使用 `{today_date}`。
11. 今日隆众沥青市场价格参考如下，请合并到 `price_strip` 中的“华东”“华南”项：{price_info}

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
    {{"label": "华东", "value": "-", "status": "-"}},
    {{"label": "华南", "value": "-", "status": "-"}}
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

            report_payload = extract_json_payload(
                response.choices[0].message.content.strip()
            )
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

            # 为 PDF 打印阶段显式注入中文字体，避免 Linux 服务器缺少系统字体导致乱码
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

            print("4. 正在使用 Playwright 将 HTML 转换为 PDF...")
            new_pdf_path = prepare_output_path(
                self.get_target_dir() / f"{base_name}_zh.pdf"
            )

            # 使用临时的无头浏览器上下文进行 PDF 打印 (因为有头模式不支持 page.pdf)
            temp_browser = self.p.chromium.launch(headless=True)
            temp_page = temp_browser.new_page()

            temp_page.goto(Path(new_html_path).resolve().as_uri())
            temp_page.wait_for_load_state("domcontentloaded")
            # 等待页面字体真正完成加载，避免打印时仍在字体回退阶段
            temp_page.wait_for_function(
                "() => document.fonts && document.fonts.status === 'loaded'"
            )

            pdf_metrics = temp_page.evaluate(
                """
                () => {
                    const container = document.getElementById('report-container') || document.body;
                    const rect = container.getBoundingClientRect();
                    return {
                        width: Math.ceil(rect.width),
                        height: Math.ceil(container.scrollHeight),
                    };
                }
                """
            )
            pdf_width, pdf_height = compute_single_page_pdf_size(
                pdf_metrics["width"], pdf_metrics["height"]
            )
            print(f"-> 单页 PDF 尺寸: width={pdf_width}, height={pdf_height}")

            # 导出为超长单页 PDF，避免 A4 分页截断
            temp_page.pdf(
                path=str(new_pdf_path),
                width=pdf_width,
                height=pdf_height,
                print_background=True,
                prefer_css_page_size=False,
                margin={"top": "0px", "right": "0px", "bottom": "0px", "left": "0px"},
            )
            temp_browser.close()

            print(f"-> 中文版 PDF 生成成功，已保存至: {new_pdf_path}")

            # 推送生成的 PDF 给指定用户
            delivered = send_pdf_to_dingtalk(str(new_pdf_path), self.target_user_id)
            if delivered is False:
                self.add_warning("delivery", "钉钉发送未成功，但 PDF 已生成")

            print("=== 处理完成 ===\n")
            return new_pdf_path

        except Exception as e:
            raise ArgusStageError("report_generation", f"大模型调用或PDF生成过程中发生错误: {e}") from e

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
        self.page.goto("https://oil.oilchem.net/oil/asphalt.shtml")

        print("2. 等待并点击顶部'能源'页签...")
        ny_link = self.page.locator("a", has_text="能源").filter(has_text="能源").first
        ny_link.hover()
        self.page.wait_for_timeout(1000)  # 等待下拉菜单动画

        print("3. 寻找并点击下拉菜单中的'沥青'链接...")
        links = self.page.locator("a", has_text="沥青").all()
        target_link = None
        for link in links:
            if link.inner_text().strip() == "沥青":
                target_link = link
                break

        if target_link:
            # 尝试点击并捕获可能的新页面
            try:
                with self.context.expect_page(timeout=3000) as new_page_info:
                    target_link.click()
                active_page = new_page_info.value
                active_page.wait_for_load_state("load")
                print("-> 已打开新页面")
            except Exception:
                # 没有触发新页面（例如 target 并非 _blank），则在当前页面继续
                active_page = self.page
                active_page.wait_for_load_state("load")
                print("-> 在当前页面加载完成")

            active_page.wait_for_timeout(2000)  # 等待 DOM 渲染

            print("4. 正在提取华东和华南价格及其变动...")
            result = {}
            try:
                # 华东数据
                huadong_el = active_page.locator("text=华东").first
                huadong_text = huadong_el.locator("xpath=..").inner_text()
                lines = huadong_text.split("\n")
                huadong_price = lines[1].strip() if len(lines) > 1 else "未知"
                huadong_change = lines[2].strip() if len(lines) > 2 else "未知"
                result["huadong"] = {"price": huadong_price, "change": huadong_change}

                # 华南数据
                huanan_el = active_page.locator("text=华南").first
                huanan_text = huanan_el.locator("xpath=..").inner_text()
                lines_hn = huanan_text.split("\n")
                huanan_price = lines_hn[1].strip() if len(lines_hn) > 1 else "未知"
                huanan_change = lines_hn[2].strip() if len(lines_hn) > 2 else "未知"
                result["huanan"] = {"price": huanan_price, "change": huanan_change}

                print(
                    f"-> 提取成功！\n【华东沥青】价格: {huadong_price}, 变动: {huadong_change}\n【华南沥青】价格: {huanan_price}, 变动: {huanan_change}"
                )

            except Exception as e:
                print(f"提取价格数据失败: {e}")

            # 如果是新打开的页面，使用完毕后可以选择关闭
            if active_page != self.page:
                active_page.close()

            print("=== 获取隆众沥青价格完成 ===\n")
            return result

        else:
            print("未找到精确匹配的'沥青'链接。")
            print("=== 获取隆众沥青价格完成 ===\n")
            return None

    def close(self):
        """关闭浏览器和上下文"""
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
        default="42706",
        help="指定要推送消息的钉钉用户ID，例如: 42706 (默认: 42706)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="指定报告输出目录；源 PDF 和生成后的 PDF 都会写入该目录",
    )

    args = parser.parse_args()

    downloader = ArgusDownloader(
        headless=args.headless,
        target_user_id=args.user_id,
        output_dir=args.output_dir,
    )

    try:
        # 1. 启动浏览器并登录
        downloader.start_browser()
        try:
            downloader.login()
        except Exception as exc:
            raise ArgusStageError("login", str(exc)) from exc

        # 2. 根据命令行参数动态调用对应的方法
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
                        "final_pdf_path": str(final_pdf_path) if final_pdf_path else None,
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
