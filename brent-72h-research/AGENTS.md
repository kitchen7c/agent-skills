# Repository Guidelines

## Project Structure & Module Organization
This repository packages a reusable Brent 72-hour research skill rather than a full application. [`SKILL.md`](/Users/tangliang/Documents/bootcamp/agent-skills/brent-72h-research/SKILL.md) defines the workflow and operating rules. [`scripts/`](/Users/tangliang/Documents/bootcamp/agent-skills/brent-72h-research/scripts) contains the executable Python tools: `fetch_public_data.py` builds and validates normalized market snapshots, and `run_report.py` generates the Markdown report. [`assets/`](/Users/tangliang/Documents/bootcamp/agent-skills/brent-72h-research/assets) stores the JSON template, and [`references/`](/Users/tangliang/Documents/bootcamp/agent-skills/brent-72h-research/references) documents required input fields.

## Build, Test, and Development Commands
Use the repository from the root directory with `python3`.

```bash
python3 scripts/fetch_public_data.py template --output /tmp/brent_snapshot.json
python3 scripts/fetch_public_data.py validate --input /tmp/brent_snapshot.json
python3 scripts/run_report.py --input /tmp/brent_snapshot.json --output /tmp/brent_72h_report.md
```

The first command writes the baseline template, the second enforces schema and warning checks, and the third runs the deterministic quant engine to produce the final report. Run `python3 scripts/<name>.py --help` before changing CLI behavior.

## Coding Style & Naming Conventions
Follow the existing Python style: 4-space indentation, type hints on public helpers, `snake_case` for functions and variables, and `UPPER_SNAKE_CASE` for module constants. Keep CLI code small and deterministic; avoid hidden network calls or non-reproducible randomness. Preserve existing Chinese domain terminology in user-facing text and keep internal identifiers in English.

## Testing Guidelines
There is no dedicated `pytest` suite in this repository today. Validation is command-driven: changes should pass `fetch_public_data.py validate` against a representative snapshot and still allow `run_report.py` to complete successfully. If you add logic branches, include a minimal fixture under `assets/` or `references/` and document how to exercise it.

## Commit & Pull Request Guidelines
Recent history uses short imperative subjects such as `Refine Brent derivatives skill risk controls` and `rename llm api key env var`. Keep commits focused and written in present tense. Pull requests should summarize workflow impact, list changed files, note any schema or output-format changes, and include a sample command sequence or generated report excerpt when behavior changes.

## Agent-Specific Notes
Do not invent unavailable market fields. If a value is proxy-based or unverifiable, label it explicitly in the snapshot and report, consistent with [`SKILL.md`](/Users/tangliang/Documents/bootcamp/agent-skills/brent-72h-research/SKILL.md).
