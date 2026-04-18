---
name: asia-bitumen-daily
description: Use when the user asks to fetch, download, send, summarize, or push the latest Argus Asia Bitumen Daily report, especially when the result should be delivered in chat or DingTalk.
---

# 亚洲沥青日报

## 概述

当用户要求获取最新的 Argus Asia Bitumen Daily、生成下游报告产物、推送到钉钉，或在获取后附带简要摘要时，使用本 skill。

这份 skill 只定义处理规范，包括触发条件、脚本执行路径、产物校验规则、文本输出路径和失败分支。具体实现细节以本地脚本为准，不在 skill 中重复展开。

## 何时使用

当用户提出以下类型的请求时触发：

- 获取最新亚洲沥青日报
- 获取 Argus Asia Bitumen Daily
- 下载或发送最新的 bitumen daily
- 把日报推送到钉钉
- 获取日报并附带一段简要摘要

常见表达包括：

- “获取亚洲沥青日报”
- “发我最新的 Argus Asia Bitumen Daily”
- “把今天的 bitumen daily 推到钉钉”
- “下载最新亚洲沥青日报并发给我”

## 何时不要使用

以下情况不应触发本 skill：

- 用户只是询问一般性的沥青市场观点，没有要求获取报告
- 用户要的是非 Argus 来源的报告
- 用户要求检索历史某一期报告，但并未明确是最新日报流程
- 用户已经提供了报告内容，只是要求做摘要或解读

## 执行路径

按以下顺序处理：

1. 先判断用户意图：
   - 仅获取报告
   - 获取并发送
   - 获取并摘要
   - 获取、发送并摘要

2. 再确定投递目标：
   - 如果当前通道能解析出一个或多个钉钉用户标识，使用这些标识
   - 如果用户要求推送到钉钉，但当前上下文无法解析任何用户标识，停止发送流程并明确报缺少投递目标
   - 不要把硬编码默认工号当作 skill 规范

3. 再解析输出目录：
   - 如果当前上下文明确给出了目标目录路径，把它视为输出根目录；最终产物目录固定使用“运行当天日期”命名
   - 如果传入的目录本身已经是运行当天日期目录，则直接使用该目录
   - 只解析“目录路径”，不要把任意字符串误判成路径
   - 如果目录不存在，可先创建目录；创建失败则按“产物生成失败”处理
   - 如果上下文没有给出目录，则回退到脚本默认路径

4. 执行脚本前，先给用户一条简短进度提示。

5. 通过统一入口执行脚本：

```bash
bash scripts/run_hnxcl_uv.sh --method get_asia_bitumen_daily --user_id "<resolved_user_ids>" --output-dir "<resolved_output_dir>"
```

或直接使用：

```bash
uv run --python 3.11 scripts/hnxcl.py --method get_asia_bitumen_daily --user_id "<resolved_user_ids>" --output-dir "<resolved_output_dir>"
```

如果上下文未解析出目录，则省略 `--output-dir` 参数。

如果当前请求只是“获取”而不是“发送”，但现有实现仍要求走同一入口完成报告生成，则继续使用该入口。

如果用户要求发送到钉钉，还要遵守以下发送内容约束：

- 默认发送最终生成的中文版 JPEG 图片，不要把源 PDF 当成最终投递物
- 若当前场景是脚本主动投递，默认让脚本发送最终 JPEG
- 钉钉发送最终 JPEG 时必须使用图片消息方式，上传媒体类型为 `image`，消息类型为 `sampleImageMsg`，不要使用 `sampleFile` 附件消息
- 若当前场景是在钉钉会话中直接回复，则先给一句简短说明，再附最终图片
- 说明文案保持简洁，例如“最新一期 Argus Asia Bitumen Daily 已整理完成，见下图。”
- 不要把本地路径、调试文件或源 PDF 链接直接暴露给最终用户

运行入口约束：

- 优先使用 `bash scripts/run_hnxcl_uv.sh ...`
- 不要假设 runtime 中存在 `pyproject.toml` 或 `uv.lock`
- Python 依赖以 `scripts/requirements-hnxcl.txt` 为准，由启动脚本通过 `uv run --with-requirements` 解析
- 如果执行环境没有可用的 `agent-browser` 浏览器运行时，先执行 `bash scripts/bootstrap_hnxcl_uv.sh`
- `scripts/bootstrap_hnxcl_uv.sh` 负责安装 `agent-browser` 所需的 Chromium 运行时，随后再执行主流程
- Argus 外层 publication 页面加载成功但 legacy iframe `/integration/publication` 空白或出现 `requireConfig` 脚本错误时，不要继续依赖 iframe 内 DOM、PDF 按钮或文章正文回退；应复用已登录 cookies 直接调用 `/workspaces/api/publication` 下载 PDF

6. 只有同时满足以下条件，才可视为执行成功：
   - 进程正常退出
   - 获取到了可用源内容（源 PDF 或允许的正文回退）
   - 生成了预期的最终 JPEG 图片产物
   - 如果用户要求发送到钉钉，则钉钉发送成功

7. 如果脚本日志显示成功，但没有找到预期产物，按失败处理，不要直接向用户报成功。

## 产物校验

不要假设脚本执行完成就代表结果可用，必须校验产物。

- 以脚本的实际输出为准
- 执行后检查是否生成了新的最终 JPEG 图片
- 如果本次请求指定了输出目录，优先在“输出根目录/运行当天日期目录”内校验产物
- 区分两类产物：
  - 原始下载的源 PDF
  - 后处理生成的中文版 JPEG 图片
- 只有在用户请求所需的最终产物已存在时，才对外报告成功

除非用户明确要求，不要在回复中暴露本地文件路径。

## 文本输出路径

### 处理中

发送一条简短进度提示，说明正在获取报告即可，不要暴露内部实现细节。

### 成功时

根据用户请求返回对应结果：

- 仅获取：确认报告已获取
- 获取并发送：确认最终图片已通过钉钉发送
- 获取并摘要：先确认已获取，再给出简洁摘要
- 获取、发送并摘要：先确认最终图片已发送，再补充摘要

回复保持简洁，不暴露内部路径、凭证或实现细节。

### 部分成功时

如果报告产物已经生成，但投递失败：

- 明确说明报告已成功生成
- 明确说明钉钉发送失败
- 如适用，可退化为在当前对话中提供摘要

### 失败时

明确指出失败发生在哪个阶段：

- 登录或访问源站失败
- 报告下载失败
- 产物生成失败
- 钉钉发送失败
- 缺少投递目标

只要用户要求的关键步骤没有完成，就不要对外宣称成功。

## 失败处理

按以下规则处理异常：

- 如果登录失败或源站不可访问，直接报告获取失败
- 如果下载失败，不要继续假设后续生成或发送成功
- 如果源 PDF 已拿到，但图片后处理失败，明确说明“源报告已获取，但最终图片生成失败”
- 如果产物已生成但钉钉发送失败，按部分成功处理
- 如果用户要求推送到钉钉但当前没有可用的用户标识，应先补齐标识或终止发送

## 依赖项

本 skill 依赖以下本地实现和运行条件：

- `scripts/hnxcl.py`
- `scripts/run_hnxcl_uv.sh`
- `scripts/bootstrap_hnxcl_uv.sh`
- `scripts/requirements-hnxcl.txt`
- 脚本所需的浏览器自动化运行环境
- Argus 报告源访问能力
- 钉钉接口凭证和发送权限
- 下游生成流程所需的大模型接口能力

## 文件说明

- `scripts/hnxcl.py`：报告获取与生成的统一入口
- `scripts/bootstrap_hnxcl_uv.sh`：初始化 `agent-browser` 运行时
- `scripts/run_hnxcl_uv.sh`：最小可运行启动入口，不依赖根目录项目元数据
- `scripts/requirements-hnxcl.txt`：runtime 自包含 Python 依赖清单
- `scripts/hnxcl.html`：报告生成流程使用的 HTML 模板
- `scripts/ding.ts`：钉钉通道集成参考实现

## 常见错误

- 把硬编码用户工号当成正式投递规则
- 未校验产物是否存在就直接报成功
- 在用户回复中暴露本地文件路径
- 把“获取失败”和“发送失败”混成同一种错误
- 在纯市场分析请求中误触发本 skill
- 继续把 PDF 当成最终投递产物，而不是最终生成的 JPEG 图片
