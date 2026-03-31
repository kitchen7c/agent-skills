---
name: brent-72h-research
description: Use when the user asks for a 72-hour Brent crude tactical report, Brent derivatives research, Brent futures-options strategy, or a structured directional oil-market note with explicit price targets, intervals, Greeks, and risk metrics. Also use when the user provides a Brent research prompt and wants it converted into a repeatable institutional workflow or skill.
---

# Brent 72H Research

## Overview

将 72 小时 Brent 原油战术衍生品研究固定为一套可重复执行的流程：先抓公开数据并做时间戳对齐，再判断 `Black-76 执行模式` 或 `BSM 代理模式`，然后用确定性的 Python 脚本统一计算区间、Greeks、VaR/CVaR、压力测试与一致性校验，最后输出中文机构化报告。

这个 skill 的目标不是替代判断，而是约束判断过程，避免把实时不可得的数据、代理指标和推断混为一谈。

## When to Use

在以下情况触发：

- 用户要写 Brent 72 小时方向研判
- 用户要求 Brent 期货与期权联合策略
- 用户要求给出明确价格目标、概率区间、Greeks、VaR/CVaR
- 用户强调不能伪造链路数据、不能伪造 GEX、Gamma Wall、OI 磁吸
- 用户要把一段原油衍生品研究提示词固化成可重复执行的 skill

以下情况不要触发：

- 用户只要泛泛的油价观点，不要量化报告
- 用户只要单一新闻摘要，没有衍生品结构
- 用户只要改写已有中文报告，而不是重新研究

## Required Workflow

严格按以下顺序执行，不要跳步：

1. 先做网页检索，不要先写结论。
2. 对 TradingView 与 Barchart 的正式数据采集一律使用 `agent-browser`，不要把普通网页浏览当作正式采集链路。
3. 用 `agent-browser` 检索并交叉验证 Brent M+2、M+3、M+4 至少两路公开价格来源。
4. 检索 OVX、期限结构、公开定位代理、72 小时跳跃催化。
5. 判断是否存在足够完整的公开 Brent 期货期权链。
6. 生成标准化输入快照。
7. 运行确定性 Python 引擎。
8. 只有在模型自检通过后，才输出最终中文报告。

## Agent Browser Workflow

对以下两个站点，正式采集时必须使用 `agent-browser`：

- `https://www.tradingview.com/symbols/ICEEUR-BRN1!/contracts/`
- `https://www.barchart.com/futures/quotes/CB*0/options?futuresOptionsView=merged`

这样做的目的是固定一条可复现采集路径，避免：

- 普通网页浏览输出不稳定
- JavaScript 渲染后的数据缺失
- 期货表与期权链在不同工具中的可见字段不一致

### TradingView 采集

- TradingView 负责主采集 `market.contracts.m2/m3/m4`
- 必须通过 `agent-browser` 读取 `contracts` 页可见合约表
- 正式链路中，不要仅凭普通网页浏览结果直接写入快照
- 应采集的字段包括：
  - contract symbol
  - month label
  - expiration
  - price
  - change
  - high
  - low
- 对同日到期合约，应跳过并从下一未到期月份开始映射 `m2/m3/m4`

示例命令：

```bash
agent-browser --session tv-brent open https://www.tradingview.com/symbols/ICEEUR-BRN1!/contracts/
agent-browser --session tv-brent wait 6000
agent-browser --session tv-brent snapshot -i -c -d 4
agent-browser --session tv-brent eval "document.querySelector('main')?.innerText ?? ''"
```

### Barchart 采集

- Barchart 负责主采集 `chain.options`
- 必须使用 `agent-browser`
- 对 Barchart，不要使用默认浏览器指纹直接采集；默认会话可能返回 `403 ERROR`
- 必须使用更接近真人浏览器的启动参数

推荐参数：

```bash
--args '--disable-blink-features=AutomationControlled'
--user-agent 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36'
```

期权价格页示例：

```bash
agent-browser --session bc-brent \
  --args '--disable-blink-features=AutomationControlled' \
  --user-agent 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36' \
  open 'https://www.barchart.com/futures/quotes/CB*0/options?futuresOptionsView=merged'
agent-browser --session bc-brent wait 8000
agent-browser --session bc-brent snapshot -i -c -d 5
```

波动率与 Greeks 页示例：

```bash
agent-browser --session bc-greeks \
  --args '--disable-blink-features=AutomationControlled' \
  --user-agent 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36' \
  open 'https://www.barchart.com/futures/quotes/CB*0/volatility-greeks?futuresOptionsView=merged'
agent-browser --session bc-greeks wait 7000
agent-browser --session bc-greeks snapshot -i -c -d 5
```

月份切换示例：

```bash
agent-browser --session bc-greeks select '#bc-options-toolbar__dropdown-month' 'Jul 2026'
agent-browser --session bc-greeks wait 5000
```

### 站点角色分工

- TradingView：
  - 主采集 `market.contracts.m2/m3/m4`
  - 用于读取 Brent futures 月度合约表
- Barchart：
  - 主采集 `chain.options`
  - 用于读取期权价格、IV、Delta、Gamma、Theta、Vega、IV Skew、Bid/Ask、Volume、Open Interest

### 保留原有逻辑的字段

以下字段不由新的 `agent-browser` 采集链替换，继续沿用 skill 现有逻辑、原有公开源或人工整理流程：

- `market.ovx`
- `market.positioning`
- `market.jump_catalysts`
- `history.m2_daily_closes`
- `market.risk_free_rate`

因此当前边界应固定为：

- `agent-browser` 负责：`market.contracts.m2/m3/m4` 与 `chain.options`
- 原有逻辑继续负责：OVX、定位代理、催化剂、20 日历史收盘序列、无风险利率代理

### 页面级边界

- TradingView `news` 页可作为新闻线索发现页，但不能单独满足催化剂字段要求
- Barchart `historical-prices` 页是历史合约汇总，不等同于单一合约 20 日日线
- Barchart `historical-download` 页存在试用或权限约束，不应假设其可稳定提供匿名可用的 20 日收盘序列

## Mode Gate

### Mode A: Black-76 执行模式

仅在以下字段真实可得时使用：

- expiry
- strike
- option side
- option price 或 implied volatility

若满足，则：

- 用 `Black-76` 作为主定价模型
- 所有 Greeks、定价、模拟都锚定到同一 Brent futures expiry
- 尽量使用可观察到的真实 strike

### Mode B: BSM 代理模式

若公开源拿不到完整可用的 Brent 期货期权链，则必须使用该模式。

若触发，则：

- 使用 Brent spot proxy 或最近可验证的 Brent reference
- 用 `BSM` 做理论定价
- 明确写明这是 `代理模式`
- 所有执行位都追加：
  `模型化执行位，不代表实时可成交盘口`

## Source Rules

- 任何需要“最新”“当前”“最近”的字段，都必须用 web 检索确认。
- 价格、新闻、政策、库存、制裁、战争、日历类事件都属于时变信息，必须检索。
- TradingView 与 Barchart 的正式数据采集一律走 `agent-browser`，不要仅凭普通网页浏览结果直接写入快照。
- 若同一合约两个公开价格源差异大于 `0.30%`，必须在报告中同时披露两者，并解释最终采用哪一个。
- 无法公开验证的字段，必须显式写：
  `公开源不可得`
- 使用替代指标时，必须显式写：
  `代理指标`
- 属于逻辑外推而非直接观测时，必须显式写：
  `推断`

### 字段映射与来源优先级

- `market.contracts.m2/m3/m4`
  - 主采用值：TradingView `contracts` 页
  - 交叉验证：Barchart 同月份 Brent 页面
- `chain.options`
  - 主采用值：Barchart `options` 与 `volatility-greeks` 页
- `chain.options[].price`
  - 优先来自 Barchart `Latest`
- `chain.options[].iv`
  - 优先来自 Barchart `IV`
  - 若页面显示 `0.00%`，视为不可用，不要当真实 IV 写入
- `chain.options[].option_type`
  - 由 Barchart `Type` 映射：
    - `Call` -> `call`
    - `Put` -> `put`
- `strategies.options.legs[].strike`
  - 只能从 Barchart 可见真实 strike 中选择
- `market.jump_catalysts`
  - 不由 TradingView/Barchart 单独完成，必须回到原始新闻源或官方事件源确认
- `market.ovx` / `market.positioning` / `history.m2_daily_closes` / `market.risk_free_rate`
  - 继续沿用原有逻辑，不纳入新的 `agent-browser` 替换范围

## Input Contract

先使用 web 或其他公开来源收集信息，再把结果整理到标准化 JSON。

模板文件：

- `assets/market_snapshot.template.json`

校验脚本：

```bash
python3 scripts/fetch_public_data.py template --output /tmp/brent_snapshot.json
python3 scripts/fetch_public_data.py validate --input /tmp/brent_snapshot.json
```

量化与报告脚本：

```bash
python3 scripts/run_report.py --input /tmp/brent_snapshot.json --output /tmp/brent_72h_report.md
```

### Agent Browser 采集顺序

建议按以下顺序采集：

1. 用 TradingView `contracts` 页获取 `m2/m3/m4`
2. 用 Barchart `options` 页读取可交易 strike、Latest、Bid/Ask、Volume、Open Interest
3. 用 Barchart `volatility-greeks` 页读取 IV 与 Greeks
4. 对 Barchart 月份下拉框逐月切换，锚定与 `m2/m3/m4` 一致的期权月
5. 将 TradingView 主价格与 Barchart 期权月上下文做交叉验证
6. 对无法由这两个站点直接提供的字段，单独补充其他公开源

建议月份代码对照：

- TradingView `BRNM2026` <-> Barchart `CBM26`
- TradingView `BRNN2026` <-> Barchart `CBN26`
- TradingView `BRNQ2026` <-> Barchart `CBQ26`

## Data Collection Checklist

标准化输入中至少应包含：

- 交易日期与统一 UTC 时间戳
- Brent M+2、M+3、M+4 使用值
- 每个合约至少两路交叉验证来源
- 最终采用来源及原因
- OVX 与时间戳
- 期限结构状态：backwardation 或 contango
- 定位数据或最接近的公开定位代理
- 72 小时催化剂列表
- 20 日历史收盘序列
- 无风险利率代理
- 方向判断与 72 小时中位目标
- 期货策略定义
- 期权结构定义

## Reporting Rules

最终报告必须：

- 全文使用简体中文
- 使用绝对日期和绝对时间戳
- 包含用户要求的 9 个固定章节，顺序不可变
- 附带术语表与 Sources
- 不输出原始 Python 代码
- 不伪造期权链字段、GEX、dealer positioning、OI magnet、Gamma Wall

## Strategy Rules

- 不推荐裸卖空、裸卖权等无限风险结构。
- 若 IV 高于 HV 且事件风险可控，可考虑有限风险 short premium，但不得描述为“安全”。
- 若跳跃风险主导，优先有限风险 long gamma 或方向价差。
- 若 M+2/M+3/M+4 曲线斜率极端，必须评估跨期或曲线交易是否应成为主策略。
- 期货和期权必须写成“配对框架”，不能简单重复同一暴露。

## Quant Rules

- 使用 ACT/365
- 历史波动率优先 20 日 HV
- Monte Carlo 至少 `100000` 路径
- 随机种子固定
- 期货、期权、区间、Greeks、压力测试必须共用同一底层参考与到期锚点

## Self-Check Gate

若出现以下任一问题，不要输出成品结论，必须先修正：

- 定价、Greeks、模拟使用了不同底层参考
- 到期日、day count、乘数不一致
- 有限风险结构的 max loss 与净权利金、翼宽不一致
- 99% VaR 或 99% CVaR 超过理论最大亏损
- 压力测试结果与理论最大亏损不一致
- 把低概率窄区间误写成高概率区间

若校验失败，量化脚本会输出：

`模型自检未通过`

## Files

- `scripts/fetch_public_data.py`
  生成模板并校验标准化市场快照
- `scripts/run_report.py`
  运行统一量化引擎并生成最终 Markdown 报告
- `references/input-schema.md`
  输入字段说明与填写约束
- `assets/market_snapshot.template.json`
  市场快照模板

## Common Mistakes

- 先写方向，再反向找数据
- 对 TradingView 与 Barchart 使用普通网页浏览结果直接落快照，而不是走 `agent-browser`
- 把 spot HV 拿去替代远月合约 HV 而不标注
- 把 OVX 当成 Brent 原生波动率而不标 `代理指标`
- 链路不可得时仍假装自己有实时可成交 strike 与 Greeks
- 期货和期权分别用不同底层价格
- 把 Barchart `IV = 0.00%` 的行误当成真实隐含波动率
- 把 Barchart `historical-prices` 合约汇总页误写成单一合约 20 日日线
- 忘记把 `推断`、`代理指标`、`公开源不可得` 明确写入正文
