#!/usr/bin/env python3
"""PoC: extract Brent M+2/M+3/M+4 contract prices from TradingView via agent-browser."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime


TRADINGVIEW_CONTRACTS_URL = "https://www.tradingview.com/symbols/ICEEUR-BRN1!/contracts/"
ROW_PATTERN = re.compile(
    r"(?P<symbol>BRN[A-Z]\d{4})\n"
    r"Brent Crude Futures \((?P<label>[A-Za-z]{3} \d{4})\)\n"
    r"D\n\t(?P<expiration>\d{4}-\d{2}-\d{2})\t"
    r"(?P<price>-?\d+(?:\.\d+)?)\t"
    r"(?P<change_pct>[+\-−]?\d+(?:\.\d+)?%)\t"
    r"(?P<change>[+\-−]?\d+(?:\.\d+)?)\t"
    r"(?P<high>-?\d+(?:\.\d+)?)\t"
    r"(?P<low>-?\d+(?:\.\d+)?)\t\n"
    r"(?P<tech_rating>[A-Za-z ]+)"
)


@dataclass
class ContractRow:
    symbol: str
    label: str
    expiration: str
    price: float
    change_pct: str
    change: float
    high: float
    low: float
    tech_rating: str


def parse_contract_rows(main_text: str) -> list[ContractRow]:
    rows: list[ContractRow] = []
    for match in ROW_PATTERN.finditer(main_text):
        rows.append(
            ContractRow(
                symbol=match.group("symbol"),
                label=match.group("label"),
                expiration=match.group("expiration"),
                price=float(match.group("price")),
                change_pct=match.group("change_pct"),
                change=float(match.group("change").replace("−", "-")),
                high=float(match.group("high")),
                low=float(match.group("low")),
                tech_rating=match.group("tech_rating").strip(),
            )
        )
    return rows


def select_forward_contracts(rows: list[ContractRow], trade_date: date) -> dict[str, ContractRow]:
    active_rows = [
        row for row in rows if date.fromisoformat(row.expiration) > trade_date
    ]
    if len(active_rows) < 3:
        raise ValueError("可用的未到期 Brent 合约少于 3 个，无法映射 M+2/M+3/M+4")
    return {"m2": active_rows[0], "m3": active_rows[1], "m4": active_rows[2]}


def run_agent_browser(session: str, js_expr: str, timeout_ms: int) -> str:
    command = [
        "agent-browser",
        "--session",
        session,
        "eval",
        js_expr,
    ]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=max(30, timeout_ms // 1000 + 10),
    )
    return completed.stdout.strip()


def open_contracts_page(session: str, timeout_ms: int) -> None:
    open_command = [
        "agent-browser",
        "--session",
        session,
        "open",
        TRADINGVIEW_CONTRACTS_URL,
    ]
    wait_command = [
        "agent-browser",
        "--session",
        session,
        "wait",
        str(timeout_ms),
    ]
    subprocess.run(open_command, check=True, capture_output=True, text=True)
    subprocess.run(wait_command, check=True, capture_output=True, text=True)


def fetch_main_text(session: str, timeout_ms: int) -> str:
    open_contracts_page(session, timeout_ms)
    output = run_agent_browser(
        session,
        "document.querySelector('main')?.innerText ?? ''",
        timeout_ms=timeout_ms,
    )
    return json.loads(output)


def build_payload(
    session: str,
    trade_date: date,
    timeout_ms: int,
    source_url: str,
) -> dict[str, object]:
    main_text = fetch_main_text(session, timeout_ms)
    rows = parse_contract_rows(main_text)
    selected = select_forward_contracts(rows, trade_date)
    return {
        "trade_date": trade_date.isoformat(),
        "source_name": "TradingView contracts via agent-browser PoC",
        "source_url": source_url,
        "session": session,
        "contracts": {
            key: {
                **asdict(row),
                "reference_type": "page_table_last",
            }
            for key, row in selected.items()
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Brent M+2/M+3/M+4 contracts from TradingView using agent-browser"
    )
    parser.add_argument(
        "--trade-date",
        default=date.today().isoformat(),
        help="Trade date used to skip expired same-day contracts, format YYYY-MM-DD",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=8000,
        help="Wait time after page open before scraping visible table text",
    )
    parser.add_argument(
        "--session",
        default="brent-tv-poc",
        help="agent-browser session name",
    )
    parser.add_argument(
        "--output",
        help="Optional JSON output path; stdout is always written",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        trade_date = date.fromisoformat(args.trade_date)
    except ValueError as exc:
        parser.error(f"--trade-date 格式错误: {exc}")

    try:
        payload = build_payload(
            session=args.session,
            trade_date=trade_date,
            timeout_ms=args.timeout_ms,
            source_url=TRADINGVIEW_CONTRACTS_URL,
        )
    except FileNotFoundError:
        print("未找到 agent-browser，请先安装该 CLI。", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        print(f"agent-browser 执行失败: {stderr}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"解析 TradingView 页面失败: {exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
