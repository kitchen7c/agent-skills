import os
import argparse
import requests
import json
import base64
import mimetypes
import html
import re
from datetime import datetime
from pathlib import Path
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
        blocks.append(
            "\n".join(
                [
                    '<div class="price-item">',
                    f'    <div class="item-label">{html.escape(item.get("label", "-"))}</div>',
                    f'    <div class="item-val">{html.escape(str(item.get("value", "-")))}</div>',
                    f'    <div class="item-status">{html.escape(item.get("status", "-"))}</div>',
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
        blocks.append(
            "\n".join(
                [
                    "<div>",
                    f'    <div class="fc-col-title">{html.escape(item.get("title", "-"))}</div>',
                    f'    <div class="fc-price">{html.escape(item.get("price_range", "-"))}</div>',
                    f'    <div class="fc-desc"><span class="fc-tag-green">支撑:</span> {_format_inline_text(item.get("support", "-"))}</div>',
                    f'    <div class="fc-desc"><span class="fc-tag-red">阻力:</span> {_format_inline_text(item.get("resistance", "-"))}</div>',
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
        "main_price_status": report_data.get("main_price_status", ""),
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
    }


def send_pdf_to_dingtalk(file_path, target_user_id="42706"):
    """将文件推送到指定的钉钉用户"""
    client_id = "dingbjo5gjxnh0a3y4ti"
    client_secret = "fKG-5M86zrJ7Wu23eZlaJ4Ki1TBHBRiLuBTOegZC9gQ60EVDkbnna6_KHwy1Uy6V"

    try:
        print(f"开始推送文件到钉钉，目标用户: {target_user_id}...")

        # 1. 获取 OAPI Access Token
        oapi_url = f"https://oapi.dingtalk.com/gettoken?appkey={client_id}&appsecret={client_secret}"
        oapi_res = requests.get(oapi_url).json()
        oapi_token = oapi_res.get("access_token")
        if not oapi_token:
            print(f"获取 OAPI Token 失败: {oapi_res}")
            return

        # 2. 获取 New API Access Token
        new_api_url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        new_api_res = requests.post(
            new_api_url, json={"appKey": client_id, "appSecret": client_secret}
        ).json()
        new_token = new_api_res.get("accessToken")
        if not new_token:
            print(f"获取 New API Token 失败: {new_api_res}")
            return

        # 3. 上传文件到钉钉媒体库
        upload_url = f"https://oapi.dingtalk.com/media/upload?access_token={oapi_token}&type=file"
        with open(file_path, "rb") as f:
            files = {"media": (os.path.basename(file_path), f, "application/pdf")}
            upload_res = requests.post(upload_url, files=files).json()

        media_id = upload_res.get("media_id")
        if not media_id:
            print(f"文件上传失败: {upload_res}")
            return
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
        else:
            print(f"文件推送响应异常: {send_res}")

    except Exception as e:
        print(f"推送文件到钉钉时发生错误: {e}")


class ArgusDownloader:
    def __init__(self, headless=True, target_user_id="42706", output_dir=None):
        self.headless = headless
        self.target_user_id = target_user_id
        self.output_dir = ensure_output_dir(output_dir)
        self.p = None
        self.browser = None
        self.context = None
        self.page = None

    @staticmethod
    def current_report_date():
        return datetime.now().strftime("%Y%m%d")

    def find_today_report_download(self, target_date):
        rows = (
            self.page.locator("div, li, tr")
            .filter(has_text="Argus Asia Bitumen Daily")
            .filter(has_text="PDF")
        )
        count = rows.count()
        for index in range(count - 1, -1, -1):
            row = rows.nth(index)
            row_text = row.inner_text()
            if not filename_matches_target_date(row_text, target_date):
                continue
            with self.page.expect_download() as download_info:
                row.get_by_text("PDF", exact=True).first.click()
            return download_info.value
        return None

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
        print("正在访问网站...")
        self.page.goto("https://direct.argusmedia.com/")

        # 1. 登录流程 - 输入邮箱
        self.page.wait_for_selector('input[type="email"]')
        self.page.fill('input[type="email"]', "wangjiali001@sdic.com.cn")
        self.page.get_by_role("button", name="Next").click()

        # 2. 登录流程 - 输入密码
        self.page.wait_for_selector('input[type="password"]')
        self.page.fill('input[type="password"]', "1985Py--")
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
        
        # 3. 点击 Publications 页签展开下拉菜单
        print("正在点击 Publications 页签...")
        self.page.locator("text=Publications").first.click()
        self.page.wait_for_selector("text=Argus Asia Bitumen Daily")

        with self.page.expect_download() as download_info:
            row = (
                self.page.locator("div, li, tr")
                .filter(has_text="Argus Asia Bitumen Daily")
                .filter(has_text="PDF")
                .last
            )
            row.get_by_text("PDF", exact=True).first.click()

        download = download_info.value

        # 5. 保存文件到目标目录；未指定时回退到当前路径
        file_name = download.suggested_filename
        target_dir = self.output_dir or os.getcwd()
        download_path = prepare_output_path(os.path.join(target_dir, file_name))
        download.save_as(str(download_path))

        print(f"『Argus Asia Bitumen Daily』下载完成！文件已保存至: {download_path}")

        # 获取隆众沥青价格
        prices = self.get_oilchem_asphalt_price()

        # 新增功能：将PDF内容配合模板，通过大模型生成中文HTML并转为PDF，并附带价格信息
        self.generate_chinese_report(download_path, prices)

    def generate_chinese_report(self, pdf_path, prices=None):
        """读取PDF内容，使用大模型翻译并结合HTML模板生成中文版报告，最后转为PDF"""
        print("\n=== 开始生成中文版报告 ===")
        print("1. 正在提取PDF文本内容...")
        source_report_date = extract_report_date_from_filename(os.path.basename(pdf_path))
        try:
            doc = fitz.open(pdf_path)
            pdf_text = ""
            for page in doc:
                pdf_text += page.get_text()
        except Exception as e:
            print(f"读取PDF失败: {e}")
            return

        print("2. 正在读取 HTML 模板...")
        template_path = os.path.join(os.path.dirname(__file__), "hnxcl.html")
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                html_template = f.read()
        except Exception as e:
            print(f"读取HTML模板失败: {e}")
            return

        print("3. 正在调用大模型提取结构化中文内容 (这可能需要几十秒)...")
        # 此处配置 OpenAI 兼容的地址和 Key，请替换为您实际使用的配置
        # 也可以通过环境变量读取: os.environ.get("OPENAI_API_KEY")
        api_key = os.environ.get(
            "OPENAI_API_KEY", "sk-1yagyFC2iWQD06Y00Bfnx6VjLpLROnEbmai69JpZOa3AWYVs"
        )
        base_url = os.environ.get(
            "OPENAI_BASE_URL", "https://agi-prod.chambroad.com/v1"
        )  # 请修改为您的gemini兼容代理地址
        model_name = os.environ.get(
            "MODEL_NAME", "gemini-3.1-flash-lite-preview"
        )  # 模型名称

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

            prompt = f"""
你是一个专业的沥青行业分析师。
我将提供一份【最新的英文沥青日报PDF文本内容】。请你提取核心数据，并输出一份严格符合指定 schema 的 JSON。

注意：
1. 只输出 JSON 对象，不要输出 markdown 代码块，不要输出任何解释。
2. 所有字段都必须返回；若原文没有明确信息，请填 "-" 或空数组。
3. `market_summary`、`trade_dynamics` 为字符串数组，每项是一段中文。
4. `news` 固定返回 3 条；`accent` 只能是 `red`、`orange`、`blue`。
5. `advice`、`warnings` 固定返回 3 条，每条包含 `title` 和 `desc`。
6. `forecasts` 固定返回 3 条，每条包含 `title`、`price_range`、`support`、`resistance`。
7. `chart.items` 固定返回 3 条，分别对应新加坡、韩国、伊朗；数值字段返回数字。
8. 报告日期使用 `{today_date}`。
9. 今日隆众沥青市场价格参考如下，请合并到 `price_strip` 中的“华东”“华南”项：{price_info}

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
    {{"title": "新加坡 (ABX 1)", "price_range": "-", "support": "-", "resistance": "-"}},
    {{"title": "韩国离岸 (ABX 2)", "price_range": "-", "support": "-", "resistance": "-"}},
    {{"title": "伊朗离岸 (FOB)", "price_range": "-", "support": "-", "resistance": "-"}}
  ],
  "footer_date": "{today_date}"
}}

【最新英文PDF文本】：
{pdf_text}
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
            report_payload["argus_source_date_note"] = f"引用 Argus 日报日期: {source_report_date}"
            new_html_content = render_report_template(html_template, report_payload)

            # 为 PDF 打印阶段显式注入中文字体，避免 Linux 服务器缺少系统字体导致乱码
            embedded_fonts = resolve_chinese_font_paths()
            font_css = build_embedded_font_css(embedded_fonts)
            new_html_content = inject_pdf_font_styles(
                new_html_content.strip(), font_css
            )

            if embedded_fonts:
                print(
                    f"-> 已注入内置中文字体: {', '.join(sorted(embedded_fonts.keys()))}"
                )
            else:
                print("-> 未找到内置中文字体文件，将依赖系统字体回退链")

            base_name = build_generated_report_stem(today_date)
            new_html_path = prepare_output_path(
                os.path.join(os.path.dirname(pdf_path), f"{base_name}_zh.html")
            )

            with open(new_html_path, "w", encoding="utf-8") as f:
                f.write(new_html_content.strip())
            print(f"-> 中文版 HTML 生成成功，已保存至: {new_html_path}")

            print("4. 正在使用 Playwright 将 HTML 转换为 PDF...")
            new_pdf_path = prepare_output_path(
                os.path.join(os.path.dirname(pdf_path), f"{base_name}_zh.pdf")
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
            send_pdf_to_dingtalk(str(new_pdf_path), self.target_user_id)

            print("=== 处理完成 ===\n")

        except Exception as e:
            print(f"大模型调用或PDF生成过程中发生错误: {e}")

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
        downloader.login()

        # 2. 根据命令行参数动态调用对应的方法
        method_name = args.method
        if hasattr(downloader, method_name) and callable(
            getattr(downloader, method_name)
        ):
            print(f"正在执行方法: {method_name}")
            # 获取方法并执行
            func = getattr(downloader, method_name)
            func()
        else:
            print(f"错误: 找不到指定的方法 '{method_name}'。请检查方法名是否正确。")

    except Exception as e:
        print(f"执行过程中发生错误: {e}")
    finally:
        downloader.close()


if __name__ == "__main__":
    main()
