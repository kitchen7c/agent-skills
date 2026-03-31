#!/usr/bin/env python3
"""Deterministic quantitative engine for the Brent 72H research skill."""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


ACT_365 = 365.0
TRADING_DAYS = 252.0
DEFAULT_PATHS = 150000
DEFAULT_SEED = 20260331


def cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black76_price(
    F: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> float:
    if T <= 0.0:
        intrinsic = max(F - K, 0.0) if option_type == "call" else max(K - F, 0.0)
        return intrinsic
    sigma = max(sigma, 1e-12)
    root_t = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    df = math.exp(-r * T)
    if option_type == "call":
        return df * (F * cdf(d1) - K * cdf(d2))
    return df * (K * cdf(-d2) - F * cdf(-d1))


def black76_delta(
    F: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> float:
    sigma = max(sigma, 1e-12)
    root_t = math.sqrt(max(T, 1e-12))
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * max(T, 1e-12)) / (sigma * root_t)
    df = math.exp(-r * max(T, 0.0))
    return df * cdf(d1) if option_type == "call" else -df * cdf(-d1)


def black76_gamma(F: float, K: float, T: float, r: float, sigma: float) -> float:
    sigma = max(sigma, 1e-12)
    root_t = math.sqrt(max(T, 1e-12))
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * max(T, 1e-12)) / (sigma * root_t)
    return math.exp(-r * max(T, 0.0)) * pdf(d1) / (F * sigma * root_t)


def black76_vega(F: float, K: float, T: float, r: float, sigma: float) -> float:
    sigma = max(sigma, 1e-12)
    root_t = math.sqrt(max(T, 1e-12))
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * max(T, 1e-12)) / (sigma * root_t)
    return math.exp(-r * max(T, 0.0)) * F * pdf(d1) * root_t


def black76_theta_per_day(
    F: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> float:
    next_t = max(T - 1.0 / ACT_365, 1e-12)
    return black76_price(F, K, next_t, r, sigma, option_type) - black76_price(
        F, K, T, r, sigma, option_type
    )


def bsm_price(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> float:
    if T <= 0.0:
        intrinsic = max(S - K, 0.0) if option_type == "call" else max(K - S, 0.0)
        return intrinsic
    sigma = max(sigma, 1e-12)
    root_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    if option_type == "call":
        return S * cdf(d1) - K * math.exp(-r * T) * cdf(d2)
    return K * math.exp(-r * T) * cdf(-d2) - S * cdf(-d1)


def bsm_delta(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> float:
    sigma = max(sigma, 1e-12)
    root_t = math.sqrt(max(T, 1e-12))
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * max(T, 1e-12)) / (
        sigma * root_t
    )
    return cdf(d1) if option_type == "call" else cdf(d1) - 1.0


def bsm_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    sigma = max(sigma, 1e-12)
    root_t = math.sqrt(max(T, 1e-12))
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * max(T, 1e-12)) / (
        sigma * root_t
    )
    return pdf(d1) / (S * sigma * root_t)


def bsm_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    sigma = max(sigma, 1e-12)
    root_t = math.sqrt(max(T, 1e-12))
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * max(T, 1e-12)) / (
        sigma * root_t
    )
    return S * pdf(d1) * root_t


def bsm_theta_per_day(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> float:
    next_t = max(T - 1.0 / ACT_365, 1e-12)
    return bsm_price(S, K, next_t, r, sigma, option_type) - bsm_price(
        S, K, T, r, sigma, option_type
    )


def implied_vol_bisect(
    price: float,
    underlying: float,
    strike: float,
    T: float,
    r: float,
    option_type: str,
    model: str,
) -> float:
    lo, hi = 1e-6, 5.0
    for _ in range(250):
        mid = 0.5 * (lo + hi)
        if model == "black76":
            value = black76_price(underlying, strike, T, r, mid, option_type)
        else:
            value = bsm_price(underlying, strike, T, r, mid, option_type)
        if value > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def quantile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        raise ValueError("empty array")
    idx = (len(sorted_values) - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_values[lo]
    frac = idx - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def hdi(sorted_values: list[float], mass: float) -> tuple[float, float]:
    n = len(sorted_values)
    k = max(1, int(math.floor(mass * n)))
    best = (sorted_values[0], sorted_values[k - 1])
    best_width = best[1] - best[0]
    for idx in range(0, n - k):
        lo = sorted_values[idx]
        hi = sorted_values[idx + k]
        width = hi - lo
        if width < best_width:
            best = (lo, hi)
            best_width = width
    return best


def best_fixed_width_band(
    sorted_values: list[float], width: float
) -> tuple[tuple[float, float], float]:
    best_prob = -1.0
    best_band = (sorted_values[0], sorted_values[0] + width)
    j = 0
    n = len(sorted_values)
    for i, lo in enumerate(sorted_values):
        hi = lo + width
        while j < n and sorted_values[j] <= hi:
            j += 1
        prob = (j - i) / n
        if prob > best_prob:
            best_prob = prob
            best_band = (lo, hi)
    return best_band, best_prob


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def format_num(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def format_pct(value: float, digits: int = 2) -> str:
    return f"{value * 100:.{digits}f}%"


def is_bounded_structure(legs: list[dict[str, Any]]) -> bool:
    if len(legs) != 2:
        return False
    types = {leg["option_type"] for leg in legs}
    if len(types) != 1:
        return False
    long_count = sum(1 for leg in legs if leg["side"] == "long")
    short_count = sum(1 for leg in legs if leg["side"] == "short")
    return long_count == 1 and short_count == 1


def build_curve_text(m2: float, m3: float, m4: float) -> tuple[str, float, float]:
    spread_23 = m2 - m3
    spread_34 = m3 - m4
    if m2 > m3 > m4:
        return "backwardation", spread_23, spread_34
    if m2 < m3 < m4:
        return "contango", spread_23, spread_34
    return "mixed", spread_23, spread_34


def derive_skew_from_proxy(surface: dict[str, Any]) -> float:
    skew = surface["skew_proxy"]
    return float(skew["call25_iv"]) - float(skew["put25_iv"])


def interpolate_proxy_iv(
    strike: float, underlying: float, atm_iv: float, call25_iv: float, put25_iv: float
) -> float:
    relative_moneyness = (strike / underlying) - 1.0
    if relative_moneyness >= 0:
        wing = (call25_iv - atm_iv) / 0.10
    else:
        wing = (atm_iv - put25_iv) / 0.10
    iv = atm_iv + wing * relative_moneyness
    return max(0.05, iv)


@dataclass
class OptionLegResult:
    label: str
    strike: float
    option_type: str
    side: str
    signed_price: float
    theoretical_price: float
    iv: float
    delta: float
    gamma: float
    theta_per_day: float
    vega_per_1vol: float


class Engine:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.market = payload["market"]
        self.contracts = self.market["contracts"]
        self.forecast = payload["forecast"]
        self.strategies = payload["strategies"]
        self.history = payload["history"]
        self.multiplier = float(self.market.get("contract_multiplier", 1000))
        self.r = float(self.market["risk_free_rate"])
        self.trade_date = parse_date(payload["trade_date"])
        self.horizon_hours = int(payload.get("report_horizon_hours", 72))
        self.t_horizon = self.horizon_hours / 24.0 / ACT_365
        self.paths = int(payload.get("monte_carlo_paths", DEFAULT_PATHS))
        self.seed = int(payload.get("monte_carlo_seed", DEFAULT_SEED))
        self.mode = self.determine_mode()
        self.reference_key = self.select_reference_key()
        self.reference = float(self.contracts[self.reference_key]["used_price"])
        self.expiry = self.determine_expiry()
        expiry_date = parse_date(self.expiry)
        self.days_to_expiry = max((expiry_date - self.trade_date).days, 1)
        self.t_exp = self.days_to_expiry / ACT_365
        self.t_remain = max(
            (self.days_to_expiry - self.horizon_hours / 24.0) / ACT_365, 1e-9
        )
        self.option_model = "black76" if self.mode == "Black-76 执行模式" else "bsm"

    def determine_mode(self) -> str:
        chain = self.payload.get("chain")
        if chain and chain.get("options"):
            for item in chain["options"]:
                if (
                    "strike" in item
                    and "option_type" in item
                    and "expiry" in item
                    and ("price" in item or "iv" in item)
                ):
                    return "Black-76 执行模式"
        return "BSM 代理模式"

    def determine_expiry(self) -> str:
        if self.mode == "Black-76 执行模式":
            return str(self.payload["chain"]["options"][0]["expiry"])
        return str(self.payload["proxy_option_surface"]["expiry"])

    def select_reference_key(self) -> str:
        futures = self.strategies["futures"]
        if futures["type"] == "outright":
            return futures["contract_key"]
        return futures["near_key"]

    def compute_hv20(self) -> float:
        closes = [float(row["close"]) for row in self.history["m2_daily_closes"][-21:]]
        returns = [
            math.log(closes[idx] / closes[idx - 1]) for idx in range(1, len(closes))
        ]
        return statistics.stdev(returns) * math.sqrt(TRADING_DAYS)

    def compute_atm_iv(self) -> float:
        if self.mode == "BSM 代理模式":
            return float(self.payload["proxy_option_surface"]["atm_iv"])

        chain = self.payload["chain"]["options"]
        candidates: list[tuple[float, float]] = []
        for item in chain:
            strike = float(item["strike"])
            moneyness = abs(strike - self.reference) / self.reference
            if moneyness <= 0.02:
                iv = item.get("iv")
                if iv is None:
                    iv = implied_vol_bisect(
                        float(item["price"]),
                        self.reference,
                        strike,
                        self.t_exp,
                        self.r,
                        item["option_type"],
                        "black76",
                    )
                candidates.append((float(iv), moneyness))
        if not candidates:
            raise ValueError("Mode A 下无法构造 ATM IV")
        weights = [1.0 / (m + 1e-6) for _, m in candidates]
        weight_sum = sum(weights)
        return sum(iv * w for (iv, _), w in zip(candidates, weights)) / weight_sum

    def compute_skew_proxy(self) -> str:
        if self.mode == "BSM 代理模式":
            skew_value = derive_skew_from_proxy(self.payload["proxy_option_surface"])
            return f"{format_pct(skew_value)}（代理指标）"

        chain = self.payload["chain"]["options"]
        delta_candidates: list[tuple[float, str, float]] = []
        for item in chain:
            strike = float(item["strike"])
            option_type = item["option_type"]
            iv = (
                float(item["iv"])
                if item.get("iv") is not None
                else implied_vol_bisect(
                    float(item["price"]),
                    self.reference,
                    strike,
                    self.t_exp,
                    self.r,
                    option_type,
                    "black76",
                )
            )
            delta = item.get("delta")
            if delta is None:
                delta = black76_delta(
                    self.reference, strike, self.t_exp, self.r, iv, option_type
                )
            delta_candidates.append((abs(float(delta)), option_type, iv))

        calls = [item for item in delta_candidates if item[1] == "call"]
        puts = [item for item in delta_candidates if item[1] == "put"]
        if not calls or not puts:
            return "公开源不可得"
        call25 = min(calls, key=lambda x: abs(x[0] - 0.25))
        put25 = min(puts, key=lambda x: abs(x[0] - 0.25))
        return format_pct(call25[2] - put25[2])

    def simulate_prices(self, sigma: float) -> list[float]:
        rng = random.Random(self.seed)
        prices: list[float] = []
        for _ in range(self.paths):
            z = rng.gauss(0.0, 1.0)
            price = self.reference * math.exp(
                (self.r - 0.5 * sigma * sigma) * self.t_horizon
                + sigma * math.sqrt(self.t_horizon) * z
            )
            prices.append(price)
        prices.sort()
        return prices

    def resolve_leg_price_iv(
        self, leg: dict[str, Any], atm_iv: float
    ) -> tuple[float, float]:
        strike = float(leg["strike"])
        option_type = leg["option_type"]
        if self.mode == "Black-76 执行模式":
            matches = [
                item
                for item in self.payload["chain"]["options"]
                if float(item["strike"]) == strike
                and item["option_type"] == option_type
                and item["expiry"] == self.expiry
            ]
            if not matches:
                raise ValueError(
                    f"Mode A 缺少 strike={strike} {option_type} 的链路记录"
                )
            raw = matches[0]
            iv = (
                float(raw["iv"])
                if raw.get("iv") is not None
                else implied_vol_bisect(
                    float(raw["price"]),
                    self.reference,
                    strike,
                    self.t_exp,
                    self.r,
                    option_type,
                    "black76",
                )
            )
            return float(raw["price"]), iv

        surface = self.payload["proxy_option_surface"]
        call25_iv = float(surface["skew_proxy"]["call25_iv"])
        put25_iv = float(surface["skew_proxy"]["put25_iv"])
        iv = interpolate_proxy_iv(strike, self.reference, atm_iv, call25_iv, put25_iv)
        price = bsm_price(self.reference, strike, self.t_exp, self.r, iv, option_type)
        return price, iv

    def option_price(
        self, underlying: float, strike: float, sigma: float, option_type: str, T: float
    ) -> float:
        if self.option_model == "black76":
            return black76_price(underlying, strike, T, self.r, sigma, option_type)
        return bsm_price(underlying, strike, T, self.r, sigma, option_type)

    def option_delta(
        self, underlying: float, strike: float, sigma: float, option_type: str, T: float
    ) -> float:
        if self.option_model == "black76":
            return black76_delta(underlying, strike, T, self.r, sigma, option_type)
        return bsm_delta(underlying, strike, T, self.r, sigma, option_type)

    def option_gamma(
        self, underlying: float, strike: float, sigma: float, T: float
    ) -> float:
        if self.option_model == "black76":
            return black76_gamma(underlying, strike, T, self.r, sigma)
        return bsm_gamma(underlying, strike, T, self.r, sigma)

    def option_vega(
        self, underlying: float, strike: float, sigma: float, T: float
    ) -> float:
        if self.option_model == "black76":
            return black76_vega(underlying, strike, T, self.r, sigma)
        return bsm_vega(underlying, strike, T, self.r, sigma)

    def option_theta(
        self, underlying: float, strike: float, sigma: float, option_type: str, T: float
    ) -> float:
        if self.option_model == "black76":
            return black76_theta_per_day(
                underlying, strike, T, self.r, sigma, option_type
            )
        return bsm_theta_per_day(underlying, strike, T, self.r, sigma, option_type)

    def compute_option_results(
        self, prices_72: list[float], atm_iv: float
    ) -> tuple[list[OptionLegResult], dict[str, float], dict[str, Any]]:
        strategy = self.strategies["options"]
        legs_out: list[OptionLegResult] = []
        net_entry = 0.0
        for leg in strategy["legs"]:
            observed_price, iv = self.resolve_leg_price_iv(leg, atm_iv)
            side_sign = 1.0 if leg["side"] == "long" else -1.0
            theoretical = self.option_price(
                self.reference, float(leg["strike"]), iv, leg["option_type"], self.t_exp
            )
            t_calc = self.t_remain
            delta = (
                self.option_delta(
                    self.reference, float(leg["strike"]), iv, leg["option_type"], t_calc
                )
                * side_sign
                * self.multiplier
            )
            gamma = (
                self.option_gamma(self.reference, float(leg["strike"]), iv, t_calc)
                * side_sign
                * self.multiplier
            )
            theta = (
                self.option_theta(
                    self.reference, float(leg["strike"]), iv, leg["option_type"], t_calc
                )
                * side_sign
                * self.multiplier
            )
            vega = (
                self.option_vega(self.reference, float(leg["strike"]), iv, t_calc)
                * 0.01
                * side_sign
                * self.multiplier
            )
            signed_price = observed_price * side_sign
            net_entry += signed_price
            legs_out.append(
                OptionLegResult(
                    label=str(leg["label"]),
                    strike=float(leg["strike"]),
                    option_type=str(leg["option_type"]),
                    side=str(leg["side"]),
                    signed_price=signed_price,
                    theoretical_price=theoretical,
                    iv=iv,
                    delta=delta,
                    gamma=gamma,
                    theta_per_day=theta,
                    vega_per_1vol=vega,
                )
            )

        option_pnl: list[float] = []
        for underlying_72 in prices_72:
            value = 0.0
            for leg, result in zip(strategy["legs"], legs_out):
                side_sign = 1.0 if leg["side"] == "long" else -1.0
                value += (
                    self.option_price(
                        underlying_72,
                        result.strike,
                        result.iv,
                        result.option_type,
                        self.t_remain,
                    )
                    * side_sign
                    * self.multiplier
                )
            option_pnl.append(value - net_entry * self.multiplier)
        option_pnl.sort()

        net_greeks = {
            "Delta": sum(item.delta for item in legs_out),
            "Gamma": sum(item.gamma for item in legs_out),
            "ThetaPerDay": sum(item.theta_per_day for item in legs_out),
            "VegaPer1VolPoint": sum(item.vega_per_1vol for item in legs_out),
        }

        break_even_text = "公开源不可得"
        max_profit = "公开源不可得"
        max_loss = "公开源不可得"
        if is_bounded_structure(strategy["legs"]):
            long_leg = next(item for item in strategy["legs"] if item["side"] == "long")
            short_leg = next(
                item for item in strategy["legs"] if item["side"] == "short"
            )
            width = abs(float(short_leg["strike"]) - float(long_leg["strike"]))
            entry_points = net_entry
            if long_leg["option_type"] == "call":
                lower_strike = min(
                    float(long_leg["strike"]), float(short_leg["strike"])
                )
                break_even_text = format_num(lower_strike + entry_points)
            else:
                higher_strike = max(
                    float(long_leg["strike"]), float(short_leg["strike"])
                )
                break_even_text = format_num(higher_strike - entry_points)
            max_profit_value = (width - entry_points) * self.multiplier
            max_loss_value = entry_points * self.multiplier
            max_profit = format_num(max_profit_value)
            max_loss = format_num(max_loss_value)
        pop = sum(1 for value in option_pnl if value > 0.0) / len(option_pnl)
        var99 = min(0.0, quantile(option_pnl, 0.01))
        tail = [value for value in option_pnl if value <= var99]
        cvar99 = min(0.0, sum(tail) / len(tail))
        stress = {}
        for label, multiplier in [("+15%", 1.15), ("+10%", 1.10), ("-10%", 0.90)]:
            stress_value = 0.0
            for leg, result in zip(strategy["legs"], legs_out):
                side_sign = 1.0 if leg["side"] == "long" else -1.0
                stress_value += (
                    self.option_price(
                        self.reference * multiplier,
                        result.strike,
                        result.iv,
                        result.option_type,
                        self.t_remain,
                    )
                    * side_sign
                    * self.multiplier
                )
            stress[label] = stress_value - net_entry * self.multiplier

        stats = {
            "net_premium_points": net_entry,
            "net_premium_dollars": net_entry * self.multiplier,
            "break_even_text": break_even_text,
            "max_profit_text": max_profit,
            "max_loss_text": max_loss,
            "pop": pop,
            "var99": var99,
            "cvar99": cvar99,
            "stress": stress,
        }
        return legs_out, net_greeks, stats

    def compute_futures_ladder(self) -> list[dict[str, Any]]:
        strategy = self.strategies["futures"]
        ladder: list[dict[str, Any]] = []
        if strategy["type"] == "outright":
            entry_ref = float(self.contracts[strategy["contract_key"]]["used_price"])
            side_sign = 1.0 if strategy["side"] == "long" else -1.0
            for level in strategy["pnl_ladder_levels"]:
                pnl = (float(level) - entry_ref) * side_sign * self.multiplier
                ladder.append({"price": float(level), "pnl_per_contract": pnl})
            return ladder

        near = float(self.contracts[strategy["near_key"]]["used_price"])
        far = float(self.contracts[strategy["far_key"]]["used_price"])
        current_spread = near - far
        side_sign = 1.0 if strategy["side"] == "long_spread" else -1.0
        for level in strategy["pnl_ladder_levels"]:
            pnl = (float(level) - current_spread) * side_sign * self.multiplier
            ladder.append({"price": float(level), "pnl_per_contract": pnl})
        return ladder

    def compute_report(self) -> str:
        hv20 = self.compute_hv20()
        atm_iv = self.compute_atm_iv()
        vrp = atm_iv - hv20
        prices_72 = self.simulate_prices(atm_iv)

        sigma_move = self.reference * atm_iv * math.sqrt(self.t_horizon)
        narrow_width = float(
            self.forecast.get(
                "narrow_band_width", round(max(1.0, sigma_move * 0.85), 2)
            )
        )
        narrow_band, narrow_prob = best_fixed_width_band(prices_72, narrow_width)
        hdi50 = hdi(prices_72, 0.50)
        hdi68 = hdi(prices_72, 0.68)
        hdi80 = hdi(prices_72, 0.80)
        ci68 = (quantile(prices_72, 0.16), quantile(prices_72, 0.84))
        ci95 = (quantile(prices_72, 0.025), quantile(prices_72, 0.975))

        legs_out, greeks, option_stats = self.compute_option_results(prices_72, atm_iv)
        futures_ladder = self.compute_futures_ladder()

        curve_state, spread_23, spread_34 = build_curve_text(
            float(self.contracts["m2"]["used_price"]),
            float(self.contracts["m3"]["used_price"]),
            float(self.contracts["m4"]["used_price"]),
        )

        self.self_check(option_stats, futures_ladder)

        regime = (
            "期权偏贵"
            if vrp > 0.03
            else "期权大致公允"
            if vrp > -0.02
            else "期权偏便宜"
        )
        sentiment = (
            "panic-priced"
            if atm_iv > max(hv20 * 1.45, 0.45)
            else "fairly priced"
            if atm_iv > hv20 * 0.9
            else "complacent"
        )
        narrow_label = "真高概率窄区间" if narrow_prob >= 0.15 else "最高密度窄区间"
        skew_text = self.compute_skew_proxy()
        mode_reason = (
            "公开期权链具备 expiry、strike、option side 与价格/IV 字段，满足 Black-76 对齐条件。"
            if self.mode == "Black-76 执行模式"
            else "公开 Brent 期货期权链不足以支持逐 strike 可验证分析，改用 BSM 代理模式。"
        )

        sources = self.collect_sources()
        catalysts = self.market.get("jump_catalysts", [])
        scenarios = self.build_scenarios(option_stats)

        lines: list[str] = []
        lines.append("1. 【战术执行摘要 | 72H Tactical Summary】")
        lines.append("")
        lines.append(f"方向结论：{self.forecast['direction']}。")
        lines.append(
            f"72 小时窄区间为 {format_num(narrow_band[0])} - {format_num(narrow_band[1])}，"
            f"该区间属性为 {narrow_label}，概率质量 {format_pct(narrow_prob)}。"
        )
        lines.append(f"首选期货策略：{self.strategies['futures']['name']}。")
        lines.append(f"首选期权结构：{self.strategies['options']['name']}。")
        lines.append(f"模型模式：{self.mode}。")
        lines.append("")
        lines.append(
            "2. 【底层数据与精准时间戳对齐 | Market Microstructure Data & Timestamp Validation】"
        )
        lines.append("")
        for key in ["m2", "m3", "m4"]:
            contract = self.contracts[key]
            lines.append(
                f"- {contract['label']} 使用价 {format_num(float(contract['used_price']))}，时间戳 {contract['used_timestamp_utc']}，"
                f"来源 {contract['used_source_name']}，属性 {contract['used_reference_type']}。"
            )
            for check in contract["cross_checks"]:
                lines.append(
                    f"- 交叉验证：{contract['label']} / {check['source_name']} / {format_num(float(check['price']))} / "
                    f"{check['timestamp_utc']} / {check['reference_type']}。"
                )
            lines.append(f"- 采用理由：{contract['selection_reason']}")
        ovx = self.market["ovx"]
        lines.append(
            f"- OVX：{format_num(float(ovx['level']))}，时间戳 {ovx['timestamp_utc']}，来源 {ovx['source_name']}，"
            f"标注为 代理指标。"
        )
        positioning = self.market["positioning"]
        position_prefix = "代理指标" if positioning.get("proxy") else "原生定位"
        lines.append(
            f"- 定位：{position_prefix}，{positioning['value_text']}，时间戳 {positioning['timestamp_utc']}，"
            f"来源 {positioning['source_name']}。"
        )
        lines.append(
            f"- 期限结构：M+2-M+3 = {format_num(spread_23)}，M+3-M+4 = {format_num(spread_34)}，"
            f"曲线状态为 {curve_state}。"
        )
        for catalyst in catalysts:
            lines.append(
                f"- 72 小时催化：{catalyst['date_utc']} / {catalyst['label']} / {catalyst['why_it_matters']} / "
                f"{catalyst['source_name']}。"
            )
        lines.append(f"- 模式选择理由：{mode_reason}")
        if self.mode == "BSM 代理模式":
            lines.append(
                "- 模式限制：这是 proxy-model 报告，执行价不代表实时可成交盘口。"
            )
        lines.append("")
        lines.append(
            "3. 【多维信号交叉验证与逻辑复核 | Cross-Validation & Sanity Check】"
        )
        lines.append("")
        lines.append(f"- HV(20D)：{format_pct(hv20)}。")
        lines.append(f"- IV/IV 代理：{format_pct(atm_iv)}。")
        lines.append(f"- VRP = IV - HV：{format_pct(vrp)}。")
        lines.append(f"- 波动率定价判断：{regime}。")
        lines.append(f"- 市场情绪：{sentiment}。")
        lines.append(f"- Skew / skew proxy：{skew_text}。")
        lines.append(
            f"- 期限结构与方向：{curve_state} 说明近端供需更紧或更松，这与方向结论 {self.forecast['direction']} 的一致性为 推断。"
        )
        lines.append(
            f"- 定位与事件：定位口径为 {'代理指标' if positioning.get('proxy') else '原生数据'}，"
            f"需与跳跃催化联合解释，不能单独视作交易触发。"
        )
        lines.append(
            f"- 事件风险：{self.forecast['event_risk_regime']}。若 IV 高于 HV 但事件风险偏高，short premium 只能视作收益/跳空风险交换，不可表述为安全。"
        )
        lines.append("")
        lines.append(
            "4. 【72小时量价预测：核心打靶区与尾部置信区间 | 72H Price Projection: Target Zone & Tail Intervals】"
        )
        lines.append("")
        lines.append(
            f"- 窄区间：{format_num(narrow_band[0])} - {format_num(narrow_band[1])}，实际概率质量 {format_pct(narrow_prob)}，"
            f"定义为 {narrow_label}。"
        )
        lines.append(f"- HDI 50%：{format_num(hdi50[0])} - {format_num(hdi50[1])}。")
        lines.append(f"- HDI 68%：{format_num(hdi68[0])} - {format_num(hdi68[1])}。")
        lines.append(f"- HDI 80%：{format_num(hdi80[0])} - {format_num(hdi80[1])}。")
        lines.append(f"- 68% 置信区间：{format_num(ci68[0])} - {format_num(ci68[1])}。")
        lines.append(f"- 95% 置信区间：{format_num(ci95[0])} - {format_num(ci95[1])}。")
        lines.append(
            "- 尾部解释：原油分布具有上行地缘供给跳跃与下行宏观需求拖累的不对称性，"
            "因此基于对数正态假设的 Monte Carlo 对上尾尖峰的低估风险必须视为已知限制，属于 推断。"
        )
        lines.append(
            "- 漂移说明：本模拟采用风险中性漂移（r），窄区间宽度反映波动率驱动的随机分布，"
            "中位目标仅用于方向判断参考，不代表风险中性概率下的预期价格。"
        )
        lines.append("")
        lines.append("5. 【期货与期权联合策略 | Futures & Options Strategy】")
        lines.append("")
        futures = self.strategies["futures"]
        if futures["type"] == "outright":
            lines.append(
                f"- 首选期货：{futures['name']}，合约 {self.contracts[futures['contract_key']]['label']}，"
                f"入场区间 {format_num(float(futures['entry_zone'][0]))} - {format_num(float(futures['entry_zone'][1]))}，"
                f"失效位 {format_num(float(futures['invalidation']))}，目标位 "
                + " / ".join(format_num(float(x)) for x in futures["targets"])
                + "。"
            )
        else:
            near_label = self.contracts[futures["near_key"]]["label"]
            far_label = self.contracts[futures["far_key"]]["label"]
            lines.append(
                f"- 首选期货：{futures['name']}，结构 {near_label} 与 {far_label} 跨期价差，"
                f"入场区间 {format_num(float(futures['entry_zone'][0]))} - {format_num(float(futures['entry_zone'][1]))}，"
                f"失效位 {format_num(float(futures['invalidation']))}，目标位 "
                + " / ".join(format_num(float(x)) for x in futures["targets"])
                + "。"
            )
        lines.append(
            f"- 首选期权：{self.strategies['options']['name']}，净权利金 {format_num(option_stats['net_premium_points'])} 点，"
            f"折算 {format_num(option_stats['net_premium_dollars'])} 美元，盈亏平衡点 {option_stats['break_even_text']}，"
            f"最大收益 {option_stats['max_profit_text']} 美元，最大亏损 {option_stats['max_loss_text']} 美元，"
            f"估计盈利概率 {format_pct(option_stats['pop'])}。"
        )
        option_legs_desc = "；".join(
            f"{leg.label} ({format_num(leg.strike)} {leg.option_type})"
            for leg in legs_out
        )
        lines.append(f"- 期权腿：{option_legs_desc}。")
        lines.append(
            "- 配对框架：期货负责日内与方向确认，期权负责隔夜与跳空风险；不应简单把两者叠成同向满仓。"
        )
        lines.append(
            "- 当盘中流动性充足且方向确认强化时，优先期货；当跨夜、事件窗或跳空风险上升时，优先期权。"
        )
        lines.append(
            "- 在当前假设下最匹配的结构，是因为其方向暴露、尾部损失上限和 72 小时事件窗更一致。"
        )
        if self.mode == "BSM 代理模式":
            lines.append("- 以上执行价为模型化执行位，不代表实时可成交盘口。")
        lines.append("")
        lines.append(
            "6. 【关键情景与价值点位分析 | Scenario Analysis & Key Value Levels】"
        )
        lines.append("")
        for scenario in scenarios:
            lines.append(f"- 情景：{scenario['title']}。")
            lines.append(f"  重要性：{scenario['why']}")
            lines.append(f"  市场含义：{scenario['regime']}")
            lines.append(f"  期货表现：{scenario['futures_action']}")
            lines.append(f"  期权表现：{scenario['options_action']}")
            lines.append(f"  动作：{scenario['portfolio_action']}")
        lines.append("")
        lines.append("7. 【逐日交易指令与动态风控 | Daily Trading Directive】")
        lines.append("")
        lines.append(
            f"- 入场规则：仅当标的进入 {format_num(float(futures['entry_zone'][0]))} - {format_num(float(futures['entry_zone'][1]))} "
            f"且方向结论 {self.forecast['direction']} 未被新数据否定时执行。"
        )
        lines.append(
            f"- 止盈规则：期货第一目标 {format_num(float(futures['targets'][0]))} 先减仓 50%，第二目标 "
            f"{format_num(float(futures['targets'][-1]))} 再评估是否平仓；期权在结构价值达到理论最大收益的 70% 附近优先兑现。"
        )
        lines.append(
            f"- 止损规则：若价格触及 {format_num(float(futures['invalidation']))}，方向单立即失效。"
        )
        lines.append(
            "- 跳空应急：若开盘跳空直接穿越目标位，不追价增仓；若反向跳空并击穿失效位，则优先平期货，保留有限风险期权。"
        )
        lines.append(
            "- IV crush 应对：若事件后价格未朝目标推进且 IV 快速回落，优先减持方向价差，避免 theta 与 vega 双重回撤。"
        )
        lines.append(
            "- 失效条件：期限结构反向、核心催化兑现方向与原假设相反、或新的公开数据使中位目标与当前持仓结构不再一致。"
        )
        lines.append("")
        lines.append(
            "8. 【📊 量化引擎运算与一致性复核结果 | Quantitative Engine Output】"
        )
        lines.append("")
        lines.append("| 腿 | Strike | 理论价格 | 假设 IV | 方向 | 净权利金 |")
        lines.append("| --- | ---: | ---: | ---: | --- | ---: |")
        for item in legs_out:
            lines.append(
                f"| {item.label} | {format_num(item.strike)} | {format_num(item.theoretical_price)} | "
                f"{format_pct(item.iv)} | {item.side} | {format_num(item.signed_price)} |"
            )
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | ---: |")
        for key, value in greeks.items():
            lines.append(f"| {key} | {format_num(value)} |")
        lines.append("")
        lines.append("| 关键价位 | 单合约 PnL |")
        lines.append("| --- | ---: |")
        for item in futures_ladder:
            lines.append(
                f"| {format_num(item['price'])} | {format_num(item['pnl_per_contract'])} |"
            )
        lines.append("")
        lines.append(
            f"- 99% VaR（每标准手）：{format_num(option_stats['var99'])} 美元。"
        )
        lines.append(
            f"- 99% CVaR（每标准手）：{format_num(option_stats['cvar99'])} 美元。"
        )
        lines.append(
            "- 注：以上为单一价差结构的每手风险敞口，实际风险需乘以实际持仓手数，不适用于 paper trade 场景。"
        )
        lines.append(
            f"- Fat-Tail Geopolitical Spike（标的 +15%）压力测试：{format_num(option_stats['stress']['+15%'])} 美元。"
        )
        lines.append(
            f"- 标的 +10% 压力测试：{format_num(option_stats['stress']['+10%'])} 美元。"
        )
        lines.append(
            f"- 标的 -10% 压力测试：{format_num(option_stats['stress']['-10%'])} 美元。"
        )
        lines.append(
            "- 压力测试说明：以上情景采用静态 vol 假设（IV 不随价格变化），实际事件中价格与 IV 通常联动，"
            "该简化可能低估或高估极端情景下的真实损益。"
        )
        lines.append("")
        lines.append("9. 【附录：专业术语名词定义 | Glossary】")
        lines.append("")
        lines.extend(self.build_glossary())
        lines.append("")
        lines.append("【Sources】")
        lines.append("")
        for source in sources:
            lines.append(f"- {source['name']}: {source['url']}")
        return "\n".join(lines).strip() + "\n"

    def build_scenarios(self, option_stats: dict[str, Any]) -> list[dict[str, str]]:
        futures = self.strategies["futures"]
        lower = float(futures["invalidation"])
        upper = float(futures["targets"][0])
        return [
            {
                "title": f"失效位测试 {format_num(lower)}",
                "why": "这是方向假设最早被证伪的位置。",
                "regime": "若跌破该位，说明 72 小时内供需与事件支撑不足，市场进入防守状态。",
                "futures_action": f"期货单应按纪律止损，单合约损益参考 PnL ladder。",
                "options_action": "有限风险价差的亏损向最大亏损收敛，但不会无限扩大。",
                "portfolio_action": "减仓或平仓方向期货；若仍需保留事件敞口，改为更轻仓的有限风险结构。",
            },
            {
                "title": f"主目标位测试 {format_num(upper)}",
                "why": "这是 72 小时交易最直接的兑现区。",
                "regime": "若触及该位，说明方向判断兑现，市场进入止盈与再定价阶段。",
                "futures_action": "期货优先兑现至少一半仓位。",
                "options_action": f"期权结构理论价值上升，接近最大收益区域时应考虑锁定利润，当前 99% VaR 仍受限于 {format_num(abs(option_stats['var99']))} 美元以内。",
                "portfolio_action": "保留少量尾仓跟踪第二目标，否则平掉大部分 Delta 暴露。",
            },
        ]

    def build_glossary(self) -> list[str]:
        rows = [
            (
                "HV(20D)",
                "基于近 20 个交易日对数收益率计算的历史波动率。",
                "用于与 IV 比较，评估 VRP。",
            ),
            (
                "IV",
                "隐含波动率，来自期权价格或代理波动率假设。",
                "用于定价、模拟和 Greeks。",
            ),
            ("VRP", "IV 与 HV 的差值。", "判断期权相对偏贵或偏便宜。"),
            (
                "HDI",
                "最高密度区间，给定概率质量下最短的价格区间。",
                "用于描述 72 小时最密集分布区域。",
            ),
            (
                "68% 置信区间",
                "从模拟分布中取 16% 到 84% 分位数形成的等尾区间。",
                "用于描述常态波动范围。",
            ),
            (
                "95% 置信区间",
                "从模拟分布中取 2.5% 到 97.5% 分位数形成的等尾区间。",
                "用于描述尾部风险范围。",
            ),
            (
                "backwardation",
                "近月价格高于远月价格的期限结构。",
                "用于判断近端供需紧张程度。",
            ),
            (
                "contango",
                "远月价格高于近月价格的期限结构。",
                "用于判断库存与持有成本压力。",
            ),
            (
                "OVX",
                "WTI 相关的原油波动率指数。",
                "本报告中仅作为 代理指标 使用，不代表 Brent 原生波动率。",
            ),
            (
                "PoP",
                "基于蒙特卡洛模拟的盈利概率（Probability of Profit）。",
                "在 72 小时截面的理论价值分布中，正收益路径占比。属于理论值，未考虑实际交易中的提前止盈/止损与时间衰减。",
            ),
            (
                "Delta",
                "标的价格每变动 1 个单位，期权价格的近似变化。",
                "衡量方向暴露。基于 72 小时剩余时间（t_remain）计算。",
            ),
            (
                "Gamma",
                "Delta 对标的价格变化的敏感度。",
                "衡量凸性与追涨杀跌速度。基于 72 小时剩余时间（t_remain）计算。",
            ),
            (
                "Theta",
                "时间流逝对期权价值的影响。",
                "衡量持仓时间损耗。基于 72 小时剩余时间（t_remain）计算。",
            ),
            (
                "Vega",
                "隐含波动率每变化 1 个波动点（1 vol point = 1% absolute），对期权价值的影响。",
                "衡量波动率风险。注意：1 vol point = 1 个百分点（1% absolute），而非相对1%。",
            ),
            (
                "VaR",
                "给定置信水平下的损失分位数（每标准手）。",
                "用于描述 99% 单侧损失阈值。实际风险需乘以持仓手数。",
            ),
            (
                "CVaR",
                "超过 VaR 后尾部损失的平均值（每标准手）。",
                "用于描述更极端尾部亏损。实际风险需乘以持仓手数。",
            ),
            (
                "推断",
                "不是直接观测，而是根据公开数据推演得到。",
                "标识研究中的外推结论。",
            ),
            (
                "代理指标",
                "并非目标市场原生数据，而是用于替代参考的相关指标。",
                "标识 Brent 不可直接公开获取时的替代口径。",
            ),
        ]
        lines = ["| term | definition | role in this report |", "| --- | --- | --- |"]
        for term, definition, role in rows:
            lines.append(f"| {term} | {definition} | {role} |")
        return lines

    def collect_sources(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for contract in self.contracts.values():
            for key in ["used_source_url"]:
                url = contract.get(key)
                name = contract.get("used_source_name", "Unknown Source")
                if url and url not in seen:
                    result.append({"name": name, "url": url})
                    seen.add(url)
            for check in contract.get("cross_checks", []):
                url = check.get("source_url")
                name = check.get("source_name", "Unknown Source")
                if url and url not in seen:
                    result.append({"name": name, "url": url})
                    seen.add(url)
        for item in [self.market.get("ovx", {}), self.market.get("positioning", {})]:
            url = item.get("source_url")
            name = item.get("source_name")
            if url and url not in seen:
                result.append({"name": name, "url": url})
                seen.add(url)
        for catalyst in self.market.get("jump_catalysts", []):
            url = catalyst.get("source_url")
            name = catalyst.get("source_name")
            if url and url not in seen:
                result.append({"name": name, "url": url})
                seen.add(url)
        return result

    def self_check(
        self, option_stats: dict[str, Any], futures_ladder: list[dict[str, Any]]
    ) -> None:
        failures: list[str] = []
        if self.multiplier <= 0:
            failures.append("contract multiplier 非法")
        if self.t_exp <= 0 or self.t_horizon <= 0:
            failures.append("到期或 horizon 非法")
        max_loss_text = option_stats["max_loss_text"]
        if max_loss_text != "公开源不可得":
            max_loss = float(max_loss_text)
            if abs(option_stats["var99"]) - max_loss > 1e-6:
                failures.append("99% VaR 超过理论最大亏损")
            if abs(option_stats["cvar99"]) - max_loss > 1e-6:
                failures.append("99% CVaR 超过理论最大亏损")
            for value in option_stats["stress"].values():
                if abs(value) - max_loss > 1e-6 and value < 0:
                    failures.append("压力测试亏损超过理论最大亏损")
                    break
        for item in futures_ladder:
            if not isinstance(item["pnl_per_contract"], float):
                failures.append("期货 PnL ladder 非数值")
                break
        if failures:
            raise ValueError(
                "模型自检未通过\n" + "\n".join(f"- {item}" for item in failures)
            )


def write_output(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Brent 72H quant engine")
    parser.add_argument(
        "--input", required=True, help="normalized market snapshot json"
    )
    parser.add_argument("--output", required=True, help="markdown report output path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = load_json(Path(args.input))
    try:
        report = Engine(payload).compute_report()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    write_output(Path(args.output), report)
    print(f"已生成报告: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
