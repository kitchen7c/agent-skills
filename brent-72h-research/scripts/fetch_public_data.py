#!/usr/bin/env python3
"""Create and validate normalized public-data snapshots for the Brent 72H skill."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "assets" / "market_snapshot.template.json"
PLACEHOLDER_HOSTS = {
    "example.com",
    "www.example.com",
    "example.org",
    "www.example.org",
    "example.net",
    "www.example.net",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
}
MODE_A_MIN_STRIKES = 2


def evaluate_mode_a_chain(
    chain: dict | None, preferred_expiry: str | None = None
) -> tuple[bool, list[str], str | None]:
    if not chain:
        return False, ["未提供 chain"], None

    options = chain.get("options", [])
    if not options:
        return False, ["chain.options 为空，无法进入 Mode A"], None

    valid_by_expiry: dict[str, list[dict]] = {}
    structural_errors: list[str] = []
    for idx, item in enumerate(options):
        missing = [
            field
            for field in ["strike", "option_type", "expiry"]
            if field not in item or item[field] in ("", None)
        ]
        if missing:
            structural_errors.append(
                f"chain.options[{idx}] 缺少字段 {', '.join(missing)}"
            )
            continue
        if "price" not in item and "iv" not in item:
            structural_errors.append(
                f"chain.options[{idx}] 必须至少包含 price 或 iv"
            )
            continue
        expiry = str(item["expiry"])
        valid_by_expiry.setdefault(expiry, []).append(item)

    if structural_errors:
        return False, structural_errors, None

    expiries = [preferred_expiry] if preferred_expiry else sorted(valid_by_expiry)
    if preferred_expiry and preferred_expiry not in valid_by_expiry:
        return (
            False,
            [f"chain.options 缺少与策略到期 {preferred_expiry} 对齐的记录，无法进入 Mode A"],
            None,
        )

    best_reason = "chain.options 不足以支撑 Mode A strike-level analysis"
    for expiry in expiries:
        items = valid_by_expiry.get(expiry, [])
        strikes: dict[float, set[str]] = {}
        for item in items:
            strike = float(item["strike"])
            option_type = str(item["option_type"]).lower()
            strikes.setdefault(strike, set()).add(option_type)

        strike_count = len(strikes)
        fully_paired_strikes = sum(
            1 for option_types in strikes.values() if {"call", "put"} <= option_types
        )
        option_types = set().union(*strikes.values()) if strikes else set()

        if (
            strike_count >= MODE_A_MIN_STRIKES
            and fully_paired_strikes >= MODE_A_MIN_STRIKES
            and {"call", "put"} <= option_types
        ):
            return True, [], expiry

        best_reason = (
            f"chain.options 在到期 {expiry} 仅覆盖 {strike_count} 个 strike，"
            f"其中完整双边 strike 为 {fully_paired_strikes} 个，"
            "不足以支撑 Mode A strike-level analysis"
        )

    return False, [best_reason], None


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def percent_diff(a: float, b: float) -> float:
    if a == 0 and b == 0:
        return 0.0
    midpoint = (abs(a) + abs(b)) / 2.0
    if midpoint == 0:
        return math.inf
    return abs(a - b) / midpoint


def is_placeholder_url(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value)
    host = parsed.netloc.lower().split("@")[-1]
    if ":" in host:
        host = host.split(":", 1)[0]
    return host in PLACEHOLDER_HOSTS


def record_source_issue(
    url: object,
    label: str,
    errors: list[str],
    warnings: list[str],
    strict_sources: bool,
) -> None:
    if not is_placeholder_url(url):
        return
    message = (
        f"{label}: source_url 使用占位域名 {url}；调试模板可保留，正式报告前必须替换为真实来源"
    )
    if strict_sources:
        errors.append(message)
    else:
        warnings.append(message)


def validate_contract(
    contract_key: str,
    payload: dict,
    errors: list[str],
    warnings: list[str],
    strict_sources: bool,
) -> None:
    required = [
        "label",
        "symbol",
        "used_price",
        "used_source_name",
        "used_source_url",
        "used_timestamp_utc",
        "used_reference_type",
        "cross_checks",
    ]
    for field in required:
        if field not in payload:
            errors.append(f"{contract_key}: 缺少字段 {field}")
    record_source_issue(
        payload.get("used_source_url"),
        f"{contract_key}.used_source_url",
        errors,
        warnings,
        strict_sources,
    )

    cross_checks = payload.get("cross_checks", [])
    if len(cross_checks) < 2:
        errors.append(f"{contract_key}: cross_checks 至少需要 2 条公开来源")
        return

    for idx, item in enumerate(cross_checks):
        record_source_issue(
            item.get("source_url"),
            f"{contract_key}.cross_checks[{idx}].source_url",
            errors,
            warnings,
            strict_sources,
        )

    prices = [item.get("price") for item in cross_checks if isinstance(item.get("price"), (int, float))]
    if len(prices) < 2:
        errors.append(f"{contract_key}: cross_checks 中至少需要两条数值价格")
        return

    for idx in range(len(prices)):
        for jdx in range(idx + 1, len(prices)):
            diff = percent_diff(prices[idx], prices[jdx])
            if diff > 0.003:
                warnings.append(
                    f"{contract_key}: 公开源价差 {diff * 100:.3f}% 超过 0.30%，"
                    "最终报告必须同时披露两个数值并解释采用理由"
                )


def validate_snapshot(
    snapshot: dict, strict_sources: bool = False
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for field in ["trade_date", "as_of_utc", "market", "history", "forecast", "strategies"]:
        if field not in snapshot:
            errors.append(f"缺少顶层字段 {field}")

    market = snapshot.get("market", {})
    contracts = market.get("contracts", {})
    for key in ["m2", "m3", "m4"]:
        if key not in contracts:
            errors.append(f"market.contracts 缺少 {key}")
        else:
            validate_contract(key, contracts[key], errors, warnings, strict_sources)

    ovx = market.get("ovx", {})
    if ovx and not ovx.get("proxy", False):
        warnings.append("OVX 未标记为代理指标；若使用 OVX，报告中应写明其为 WTI-linked 代理指标")
    record_source_issue(
        ovx.get("source_url"),
        "market.ovx.source_url",
        errors,
        warnings,
        strict_sources,
    )

    positioning = market.get("positioning", {})
    record_source_issue(
        positioning.get("source_url"),
        "market.positioning.source_url",
        errors,
        warnings,
        strict_sources,
    )

    for idx, catalyst in enumerate(market.get("jump_catalysts", [])):
        record_source_issue(
            catalyst.get("source_url"),
            f"market.jump_catalysts[{idx}].source_url",
            errors,
            warnings,
            strict_sources,
        )

    history = snapshot.get("history", {})
    closes = history.get("m2_daily_closes", [])
    if len(closes) < 21:
        errors.append("history.m2_daily_closes 至少需要 21 个收盘点")

    forecast = snapshot.get("forecast", {})
    if forecast.get("direction") not in {"偏多", "中性偏震荡", "偏空"}:
        errors.append("forecast.direction 必须为 偏多 / 中性偏震荡 / 偏空")
    if "median_72h_target" not in forecast:
        errors.append("forecast.median_72h_target 缺失")
    elif not isinstance(forecast.get("median_72h_target"), (int, float)):
        errors.append("forecast.median_72h_target 必须为数值")

    strategies = snapshot.get("strategies", {})
    if "futures" not in strategies:
        errors.append("缺少 strategies.futures")
    if "options" not in strategies:
        errors.append("缺少 strategies.options")

    chain = snapshot.get("chain")
    proxy_surface = snapshot.get("proxy_option_surface")
    if not chain and not proxy_surface:
        errors.append("必须至少提供 chain 或 proxy_option_surface 之一，用于模式判定")

    if chain:
        preferred_expiry = strategies.get("options", {}).get("expiry")
        chain_supported, chain_issues, _ = evaluate_mode_a_chain(
            chain,
            preferred_expiry=str(preferred_expiry) if preferred_expiry else None,
        )
        if not chain_supported:
            if proxy_surface:
                warnings.extend(chain_issues)
                warnings.append("chain 不满足 Mode A 覆盖要求，本次将回退到 Mode B")
            else:
                errors.extend(chain_issues)

    if not chain or (
        chain
        and not evaluate_mode_a_chain(
            chain,
            preferred_expiry=(
                str(strategies.get("options", {}).get("expiry"))
                if strategies.get("options", {}).get("expiry")
                else None
            ),
        )[0]
    ):
        atm_iv = proxy_surface.get("atm_iv") if proxy_surface else None
        if not isinstance(atm_iv, (int, float)):
            errors.append("Mode B 需要 proxy_option_surface.atm_iv")
        skew = proxy_surface.get("skew_proxy", {}) if proxy_surface else {}
        if "call25_iv" not in skew or "put25_iv" not in skew:
            errors.append("Mode B 需要 proxy_option_surface.skew_proxy.call25_iv 与 put25_iv")

    return errors, warnings


def command_template(args: argparse.Namespace) -> int:
    payload = load_json(TEMPLATE_PATH)
    write_json(Path(args.output), payload)
    print(f"已写入模板: {args.output}")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    snapshot = load_json(Path(args.input))
    errors, warnings = validate_snapshot(snapshot, strict_sources=args.strict_sources)
    if warnings:
        print("校验警告:")
        for item in warnings:
            print(f"- {item}")
    if errors:
        print("校验失败:")
        for item in errors:
            print(f"- {item}")
        return 1
    print("校验通过")
    return 0


def command_agent_browser_draft(args: argparse.Namespace) -> int:
    from scripts.agent_browser_snapshot_builder import build_live_snapshot

    payload = build_live_snapshot(
        trade_date=args.trade_date,
        as_of_utc=args.as_of_utc,
        tv_session=args.tv_session,
        bc_session=args.bc_session,
        timeout_ms=args.timeout_ms,
    )
    write_json(Path(args.output), payload)
    print(f"已写入 agent-browser snapshot 草稿: {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Brent 72H Research public-data helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    template_parser = subparsers.add_parser("template", help="write snapshot template")
    template_parser.add_argument("--output", required=True)
    template_parser.set_defaults(func=command_template)

    validate_parser = subparsers.add_parser("validate", help="validate snapshot json")
    validate_parser.add_argument("--input", required=True)
    validate_parser.add_argument(
        "--strict-sources",
        action="store_true",
        help="treat placeholder source_url values as errors",
    )
    validate_parser.set_defaults(func=command_validate)

    draft_parser = subparsers.add_parser(
        "agent-browser-draft",
        help="build snapshot draft from TradingView and Barchart via agent-browser",
    )
    draft_parser.add_argument("--trade-date", required=True)
    draft_parser.add_argument("--as-of-utc", required=True)
    draft_parser.add_argument("--tv-session", default="brent-tv-draft")
    draft_parser.add_argument("--bc-session", default="brent-bc-draft")
    draft_parser.add_argument("--timeout-ms", type=int, default=8000)
    draft_parser.add_argument("--output", required=True)
    draft_parser.set_defaults(func=command_agent_browser_draft)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
