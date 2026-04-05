# Input Schema

`assets/market_snapshot.template.json` 是最小可运行模板。

## 顶层字段

| 字段 | 含义 | 说明 |
| --- | --- | --- |
| `trade_date` | 交易日期 | `YYYY-MM-DD` |
| `as_of_utc` | 报告统一时间戳 | `YYYY-MM-DDTHH:MM:SSZ` |
| `report_horizon_hours` | 报告时长 | 默认 `72` |
| `market` | 市场观测 | 必填 |
| `history` | 历史价格序列 | 必填 |
| `forecast` | 主观判断输入 | 必填 |
| `strategies` | 期货与期权策略定义 | 必填 |
| `chain` | 公开期权链 | 可选，只有满足 Mode A 覆盖要求时才进入 Black-76 |
| `proxy_option_surface` | 代理波动率面 | Mode B 必填 |

## `market`

### `market.contracts`

必须包含 `m2`、`m3`、`m4` 三个键。每个键包含：

- `label`
- `symbol`
- `used_price`
- `used_source_name`
- `used_source_url`
- `used_timestamp_utc`
- `used_reference_type`
- `cross_checks`

其中 `cross_checks` 至少 2 条，用于 0.30% 差异校验。

### `market.ovx`

- 若使用 OVX，必须标注 `proxy: true`
- `label` 建议写：`OVX（WTI-linked，代理指标）`

### `market.positioning`

- 若非 Brent 原生定位，必须标 `proxy: true`
- 无法取得时可填写：
  - `value_text: "公开源不可得"`

### `market.jump_catalysts`

数组，每条至少包含：

- `date_utc`
- `label`
- `why_it_matters`
- `source_name`
- `source_url`

## `history`

### `history.m2_daily_closes`

至少提供 21 个收盘点，用于 20 日 HV。

格式：

```json
{"date":"2026-03-02","close":72.41}
```

## `forecast`

| 字段 | 含义 |
| --- | --- |
| `direction` | `偏多` / `中性偏震荡` / `偏空` |
| `median_72h_target` | 72 小时中位目标 | 用作 72 小时分布锚，报告中必须标注为 `推断` |
| `event_risk_regime` | `low` / `moderate` / `elevated` |
| `narrow_band_width` | 可选，若不填则脚本按波动率自动生成 |
| `thesis` | 结论摘要 |

## `strategies.futures`

支持两种类型：

### `outright`

```json
{
  "name": "Brent M+2 单边多头",
  "type": "outright",
  "contract_key": "m2",
  "side": "long",
  "entry_zone": [72.8, 73.4],
  "invalidation": 71.9,
  "targets": [74.9, 76.2],
  "pnl_ladder_levels": [71.9, 74.9, 76.2]
}
```

### `calendar_spread`

```json
{
  "name": "Brent M+2/M+4 多近空远",
  "type": "calendar_spread",
  "near_key": "m2",
  "far_key": "m4",
  "side": "long_spread",
  "entry_zone": [1.05, 1.25],
  "invalidation": 0.75,
  "targets": [1.45, 1.80],
  "pnl_ladder_levels": [0.75, 1.45, 1.80]
}
```

## `strategies.options`

策略腿格式：

```json
{
  "name": "72H 看涨价差",
  "expiry": "2026-05-29",
  "structure_type": "vertical_call_spread",
  "legs": [
    {"label": "买入 73C", "side": "long", "option_type": "call", "strike": 73},
    {"label": "卖出 76C", "side": "short", "option_type": "call", "strike": 76}
  ]
}
```

Mode B 下，这些 strike 默认解释为：

`模型化执行位，不代表实时可成交盘口`

## Mode A / Mode B

### `chain`

若存在 `chain.options`，并且在同一到期上满足以下覆盖要求：

- `strike`
- `option_type`
- `expiry`
- `price` 或 `iv`
- 至少 2 个不同 strike
- 至少 2 个完整双边 strike（同一 strike 同时有 call 与 put）

只有满足以上条件，脚本才会进入 `Black-76 执行模式`。若 `strategies.options.expiry` 已填写，则必须与该到期对齐。

### `proxy_option_surface`

若 `chain` 不可用，或 `chain` 覆盖不足以支撑逐 strike 分析，则必须提供：

- `expiry`
- `atm_iv`
- `skew_proxy.call25_iv`
- `skew_proxy.put25_iv`

并由脚本进入 `BSM 代理模式`。
