import argparse
import json
from pathlib import Path

from hnxcl import (
    ArgusDownloader,
    prepare_output_path,
    render_html_image_via_agent_browser,
)

def verify_html_to_image(html_path, image_path=None, keep_debug_html=True):
    source_html_path = Path(html_path).resolve()
    if not source_html_path.exists():
        raise FileNotFoundError(f"HTML 文件不存在: {source_html_path}")

    if image_path:
        output_image_path = prepare_output_path(Path(image_path).resolve())
    else:
        output_image_path = prepare_output_path(
            source_html_path.with_name(f"{source_html_path.stem}.verified.jpg")
        )

    debug_html_path = prepare_output_path(
        output_image_path.with_suffix(".debug.html")
    )
    diagnostics_path = prepare_output_path(
        output_image_path.with_suffix(".diagnostics.json")
    )

    runner = ArgusDownloader()
    try:
        metrics = render_html_image_via_agent_browser(
            runner=runner.run_agent_browser,
            html_path=source_html_path,
            output_image_path=output_image_path,
            session_name=f"verify-html-image-{source_html_path.stem}",
        )
        debug_html_path.write_text(source_html_path.read_text(encoding="utf-8"), encoding="utf-8")
        image_width = metrics["imageSize"]["width"]
        image_height = metrics["imageSize"]["height"]
    except Exception as exc:
        raise RuntimeError(
            "agent-browser/Chromium 启动失败。请先确认已安装浏览器依赖，并在非受限环境下运行。"
        ) from exc
    finally:
        runner.close()

    diagnostics = {
        "input_html": str(source_html_path),
        "debug_html": str(debug_html_path),
        "output_image": str(output_image_path),
        "diagnostics_json": str(diagnostics_path),
        "page_metrics": metrics,
        "image_size": {"width": image_width, "height": image_height},
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
    parser = argparse.ArgumentParser(description="验证 HTML 转 JPEG 的渲染情况")
    parser.add_argument("--html", required=True, help="待验证的 HTML 文件路径")
    parser.add_argument("--image", default=None, help="输出 JPEG 路径，默认在 HTML 同目录生成")
    parser.add_argument(
        "--delete-debug-html",
        action="store_true",
        help="验证完成后删除注入字体后的中间 HTML",
    )
    return parser


def main():
    args = build_parser().parse_args()
    diagnostics = verify_html_to_image(
        html_path=args.html,
        image_path=args.image,
        keep_debug_html=not args.delete_debug_html,
    )

    print("HTML 转 JPEG 验证完成")
    print(f"图片: {diagnostics['output_image']}")
    print(f"诊断: {diagnostics['diagnostics_json']}")
    print(f"字体状态: {diagnostics['page_metrics']['fontStatus']}")
    print(f"中文字体检查: {diagnostics['page_metrics']['sampleTextCheck']}")
    print(f"正文字体: {diagnostics['page_metrics']['bodyFontFamily']}")
    print(f"图片尺寸: {diagnostics['image_size']}")


if __name__ == "__main__":
    main()
