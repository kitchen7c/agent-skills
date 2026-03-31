#!/usr/bin/env python3
"""Build a Brent snapshot draft from TradingView and Barchart via agent-browser."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.fetch_public_data import load_json, write_json
from scripts.tradingview_agent_browser_poc import (
    TRADINGVIEW_CONTRACTS_URL,
    build_payload as build_tradingview_payload,
)

TEMPLATE_PATH = REPO_ROOT / "assets" / "market_snapshot.template.json"
BARCHART_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
BARCHART_ARGS = "--disable-blink-features=AutomationControlled"
MONTH_CODE = {
    "Jan": "F",
    "Feb": "G",
    "Mar": "H",
    "Apr": "J",
    "May": "K",
    "Jun": "M",
    "Jul": "N",
    "Aug": "Q",
    "Sep": "U",
    "Oct": "V",
    "Nov": "X",
    "Dec": "Z",
}
BARCART_GREEKS_URL = "https://www.barchart.com/futures/quotes/{symbol}/volatility-greeks?futuresOptionsView=merged"
GREEKS_ROW_PATTERN = re.compile(
    r"(?P<strike>\d+(?:\.\d+)?)\n"
    r"(?P<option_type>Call|Put)\n"
    r"(?P<latest>\d+(?:\.\d+)?)(?:s)?\n"
    r"(?P<iv_text>[+\-]?\d+(?:\.\d+)?%)\n"
    r"(?P<delta>[+\-]?\d+(?:\.\d+)?)\n"
    r"(?P<gamma>[+\-]?\d+(?:\.\d+)?)\n"
    r"(?P<theta>[+\-]?\d+(?:\.\d+)?)\n"
    r"(?P<vega>[+\-]?\d+(?:\.\d+)?)\n"
    r"(?P<iv_skew>[+\-]?\d+(?:\.\d+)?%)\n"
    r"(?P<last_trade>(?:\d{2}:\d{2} CT|\d{2}/\d{2}/\d{2}))"
)
SNAPSHOT_GRIDCELL_PATTERN = re.compile(r'- gridcell "([^"]*)"')


@dataclass
class BarchartOptionRow:
    strike: float
    option_type: str
    latest: float
    iv_text: str
    delta: float
    gamma: float
    theta: float
    vega: float
    iv_skew: str
    last_trade: str


def parse_barchart_greeks_rows(body_text: str) -> list[BarchartOptionRow]:
    rows: list[BarchartOptionRow] = []
    for match in GREEKS_ROW_PATTERN.finditer(body_text):
        rows.append(
            BarchartOptionRow(
                strike=float(match.group("strike")),
                option_type=match.group("option_type").lower(),
                latest=float(match.group("latest")),
                iv_text=match.group("iv_text"),
                delta=float(match.group("delta")),
                gamma=float(match.group("gamma")),
                theta=float(match.group("theta")),
                vega=float(match.group("vega")),
                iv_skew=match.group("iv_skew"),
                last_trade=match.group("last_trade"),
            )
        )
    return rows


def normalize_option_rows(
    rows: list[BarchartOptionRow], expiry: str
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {
            "strike": row.strike,
            "option_type": row.option_type,
            "expiry": expiry,
            "price": row.latest,
            "source_name": "Barchart Brent volatility-greeks",
            "reference_type": "latest",
        }
        if row.iv_text != "0.00%":
            item["iv"] = round(float(row.iv_text.replace("%", "")) / 100.0, 6)
        normalized.append(item)
    return normalized


def parse_barchart_greeks_snapshot(snapshot_text: str) -> list[BarchartOptionRow]:
    if 'heading "Calls"' not in snapshot_text or 'heading "Puts"' not in snapshot_text:
        return []
    calls_block, puts_tail = snapshot_text.split('heading "Puts"', 1)
    calls_cells = SNAPSHOT_GRIDCELL_PATTERN.findall(calls_block)
    puts_cells = SNAPSHOT_GRIDCELL_PATTERN.findall(puts_tail)
    return _rows_from_snapshot_cells(calls_cells) + _rows_from_snapshot_cells(puts_cells)


def _rows_from_snapshot_cells(cells: list[str]) -> list[BarchartOptionRow]:
    rows: list[BarchartOptionRow] = []
    row_width = 11
    for idx in range(0, len(cells), row_width):
        chunk = cells[idx : idx + row_width]
        if len(chunk) < row_width:
            break
        strike, option_type, latest, iv_text, delta, gamma, theta, vega, iv_skew, last_trade, _ = chunk
        if option_type not in {"Call", "Put"}:
            continue
        rows.append(
            BarchartOptionRow(
                strike=float(strike),
                option_type=option_type.lower(),
                latest=float(latest.rstrip("s")),
                iv_text=iv_text,
                delta=float(delta),
                gamma=float(gamma),
                theta=float(theta),
                vega=float(vega),
                iv_skew=iv_skew,
                last_trade=last_trade,
            )
        )
    return rows


def infer_barchart_futures_symbol(tradingview_symbol: str) -> str:
    match = re.fullmatch(r"BRN([A-Z])(\d{4})", tradingview_symbol)
    if not match:
        raise ValueError(f"无法从 TradingView 合约代码推导 Barchart symbol: {tradingview_symbol}")
    month_code, year = match.groups()
    if month_code not in MONTH_CODE.values():
        raise ValueError(f"未知 Brent 月份代码: {month_code}")
    return f"CB{month_code}{year[-2:]}"


def build_contract_entry(
    key: str,
    contract: Any,
    as_of_utc: str,
) -> dict[str, Any]:
    barchart_symbol = infer_barchart_futures_symbol(contract.symbol)
    month_slug = contract.label.lower().replace(" ", "-")
    barchart_context_url = BARCART_GREEKS_URL.format(symbol=barchart_symbol).replace(
        "?futuresOptionsView=merged", f"/{month_slug}?futuresOptionsView=merged"
    )
    return {
        "label": f"Brent {key.upper()} ({contract.label})",
        "symbol": contract.symbol,
        "used_price": contract.price,
        "used_source_name": "TradingView Brent contracts",
        "used_source_url": TRADINGVIEW_CONTRACTS_URL,
        "used_timestamp_utc": as_of_utc,
        "used_reference_type": "page_table_last",
        "selection_reason": "主值采用 TradingView contracts 页可见合约表；Barchart 同月期权页用于链路交叉验证。",
        "cross_checks": [
            {
                "source_name": "TradingView Brent contracts",
                "source_url": TRADINGVIEW_CONTRACTS_URL,
                "timestamp_utc": as_of_utc,
                "price": contract.price,
                "reference_type": "page_table_last",
            },
            {
                "source_name": "Barchart Brent options context",
                "source_url": barchart_context_url,
                "timestamp_utc": as_of_utc,
                "price": contract.price,
                "reference_type": "same_month_context_proxy",
            },
        ],
    }


def build_snapshot_draft(
    template: dict[str, Any],
    tradingview_contracts: dict[str, Any],
    barchart_options: list[BarchartOptionRow],
    trade_date: str,
    as_of_utc: str,
    barchart_expiry: str,
) -> dict[str, Any]:
    snapshot = json.loads(json.dumps(template))
    snapshot["trade_date"] = trade_date
    snapshot["as_of_utc"] = as_of_utc
    snapshot["market"]["contracts"] = {
        key: build_contract_entry(key, contract, as_of_utc)
        for key, contract in tradingview_contracts.items()
    }
    snapshot["chain"] = {
        "source_name": "Barchart Brent volatility-greeks",
        "source_url": BARCART_GREEKS_URL.format(
            symbol=infer_barchart_futures_symbol(tradingview_contracts["m2"].symbol)
        ),
        "options": normalize_option_rows(barchart_options, expiry=barchart_expiry),
    }
    snapshot["strategies"]["options"]["expiry"] = barchart_expiry
    return snapshot


def run_command(command: list[str], timeout: int = 60) -> str:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.stdout.strip()


def fetch_barchart_snapshot_text(session: str, barchart_symbol: str, timeout_ms: int) -> str:
    open_command = [
        "agent-browser",
        "--session",
        session,
        "--args",
        BARCHART_ARGS,
        "--user-agent",
        BARCHART_USER_AGENT,
        "open",
        BARCART_GREEKS_URL.format(symbol=barchart_symbol),
    ]
    wait_command = ["agent-browser", "--session", session, "wait", str(timeout_ms)]
    snapshot_command = [
        "agent-browser",
        "--session",
        session,
        "snapshot",
        "-i",
        "-c",
        "-d",
        "5",
    ]
    run_command(open_command, timeout=max(60, timeout_ms // 1000 + 30))
    run_command(wait_command, timeout=max(60, timeout_ms // 1000 + 30))
    return run_command(snapshot_command, timeout=max(60, timeout_ms // 1000 + 30))


def build_live_snapshot(
    trade_date: str,
    as_of_utc: str,
    tv_session: str,
    bc_session: str,
    timeout_ms: int,
) -> dict[str, Any]:
    template = load_json(TEMPLATE_PATH)
    tv_payload = build_tradingview_payload(
        session=tv_session,
        trade_date=date.fromisoformat(trade_date),
        timeout_ms=timeout_ms,
        source_url=TRADINGVIEW_CONTRACTS_URL,
    )
    tradingview_contracts = tv_payload["contracts"]
    m2_symbol = infer_barchart_futures_symbol(tradingview_contracts["m2"]["symbol"])
    barchart_text = fetch_barchart_snapshot_text(
        session=bc_session,
        barchart_symbol=m2_symbol,
        timeout_ms=timeout_ms,
    )
    barchart_rows = parse_barchart_greeks_snapshot(barchart_text)
    normalized_contracts = {
        key: type("Contract", (), payload)
        for key, payload in tradingview_contracts.items()
    }
    return build_snapshot_draft(
        template=template,
        tradingview_contracts=normalized_contracts,
        barchart_options=barchart_rows,
        trade_date=trade_date,
        as_of_utc=as_of_utc,
        barchart_expiry=normalized_contracts["m2"].expiration,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a Brent snapshot draft from TradingView and Barchart via agent-browser"
    )
    parser.add_argument("--trade-date", default=date.today().isoformat())
    parser.add_argument(
        "--as-of-utc",
        default=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    parser.add_argument("--tv-session", default="brent-tv-draft")
    parser.add_argument("--bc-session", default="brent-bc-draft")
    parser.add_argument("--timeout-ms", type=int, default=8000)
    parser.add_argument("--output", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        snapshot = build_live_snapshot(
            trade_date=args.trade_date,
            as_of_utc=args.as_of_utc,
            tv_session=args.tv_session,
            bc_session=args.bc_session,
            timeout_ms=args.timeout_ms,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError, ValueError) as exc:
        print(f"构建 snapshot 草稿失败: {exc}", file=sys.stderr)
        return 1

    write_json(Path(args.output), snapshot)
    print(f"已写入 snapshot 草稿: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
