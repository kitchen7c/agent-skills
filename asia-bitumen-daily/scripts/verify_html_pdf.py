import argparse
import json
from pathlib import Path

import fitz
from playwright.sync_api import sync_playwright
from playwright._impl._errors import Error as PlaywrightError

from hnxcl import (
    build_embedded_font_css,
    compute_single_page_pdf_size,
    inject_pdf_font_styles,
    prepare_output_path,
    resolve_chinese_font_paths,
)


def collect_page_metrics(page):
    return page.evaluate(
        """
        () => {
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
        }
        """
    )


def extract_pdf_text(pdf_path):
    doc = fitz.open(pdf_path)
    try:
        page_count = doc.page_count
        text = "".join(page.get_text() for page in doc)
    finally:
        doc.close()
    return {"page_count": page_count, "text": text}


def verify_html_to_pdf(html_path, pdf_path=None, keep_debug_html=True):
    source_html_path = Path(html_path).resolve()
    if not source_html_path.exists():
        raise FileNotFoundError(f"HTML 文件不存在: {source_html_path}")

    if pdf_path:
        output_pdf_path = prepare_output_path(Path(pdf_path).resolve())
    else:
        output_pdf_path = prepare_output_path(
            source_html_path.with_name(f"{source_html_path.stem}.verified.pdf")
        )

    debug_html_path = prepare_output_path(
        output_pdf_path.with_suffix(".debug.html")
    )
    diagnostics_path = prepare_output_path(
        output_pdf_path.with_suffix(".diagnostics.json")
    )
    text_dump_path = prepare_output_path(
        output_pdf_path.with_suffix(".extracted.txt")
    )

    html_content = source_html_path.read_text(encoding="utf-8")
    font_css = build_embedded_font_css(resolve_chinese_font_paths())
    debug_html = inject_pdf_font_styles(html_content, font_css)
    debug_html_path.write_text(debug_html, encoding="utf-8")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(debug_html_path.resolve().as_uri())
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_function("() => document.fonts && document.fonts.status === 'loaded'")

            metrics = collect_page_metrics(page)
            pdf_width, pdf_height = compute_single_page_pdf_size(
                metrics["containerWidth"], metrics["containerHeight"]
            )
            page.pdf(
                path=str(output_pdf_path),
                width=pdf_width,
                height=pdf_height,
                print_background=True,
                prefer_css_page_size=False,
                margin={"top": "0px", "right": "0px", "bottom": "0px", "left": "0px"},
            )
            browser.close()
    except PlaywrightError as exc:
        raise RuntimeError(
            "Playwright/Chromium 启动失败。请先确认已安装浏览器依赖，并在非受限环境下运行。"
        ) from exc

    pdf_info = extract_pdf_text(output_pdf_path)
    text_dump_path.write_text(pdf_info["text"], encoding="utf-8")

    diagnostics = {
        "input_html": str(source_html_path),
        "debug_html": str(debug_html_path),
        "output_pdf": str(output_pdf_path),
        "diagnostics_json": str(diagnostics_path),
        "extracted_text": str(text_dump_path),
        "pdf_page_count": pdf_info["page_count"],
        "pdf_text_preview": pdf_info["text"][:500],
        "page_metrics": metrics,
        "pdf_size": {"width": pdf_width, "height": pdf_height},
    }
    diagnostics_path.write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not keep_debug_html and debug_html_path.exists():
        debug_html_path.unlink()
        diagnostics["debug_html"] = None
        diagnostics_path.write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return diagnostics


def build_parser():
    parser = argparse.ArgumentParser(description="验证 HTML 转 PDF 的字体与文本抽取情况")
    parser.add_argument("--html", required=True, help="待验证的 HTML 文件路径")
    parser.add_argument("--pdf", default=None, help="输出 PDF 路径，默认在 HTML 同目录生成")
    parser.add_argument(
        "--delete-debug-html",
        action="store_true",
        help="验证完成后删除注入字体后的中间 HTML",
    )
    return parser


def main():
    args = build_parser().parse_args()
    diagnostics = verify_html_to_pdf(
        html_path=args.html,
        pdf_path=args.pdf,
        keep_debug_html=not args.delete_debug_html,
    )

    print("HTML 转 PDF 验证完成")
    print(f"PDF: {diagnostics['output_pdf']}")
    print(f"诊断: {diagnostics['diagnostics_json']}")
    print(f"抽取文本: {diagnostics['extracted_text']}")
    print(f"页数: {diagnostics['pdf_page_count']}")
    print(f"字体状态: {diagnostics['page_metrics']['fontStatus']}")
    print(f"中文字体检查: {diagnostics['page_metrics']['sampleTextCheck']}")
    print(f"正文字体: {diagnostics['page_metrics']['bodyFontFamily']}")
    print(f"PDF 预览文本前 200 字: {diagnostics['pdf_text_preview'][:200]}")


if __name__ == "__main__":
    main()
