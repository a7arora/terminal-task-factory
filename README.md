# Terminal Task Factory (prototype)

This repository contains a weighted mixed-family task generator and a staged workflow for proposing, reviewing, specifying, implementing, and verifying terminal-agent tasks. The topic sampler draws categories and subcategories from `config/terminal-bench-4.0.0-topic-distribution.json` using a reproducible seed.

The pipeline stages are:

- **A:** weighted category/subcategory draw.
- **B:** idea generation using the reference-task and GitHub MCP servers.
- **C:** clarity and consistency edit.
- **D:** novelty review against visible tasks in the sampled subcategory.
- **E:** pinned-repository baseline preflight.
- **F:** task-specification drafting.
- **G:** independent specification review, with up to three reviewer rounds.
- **H:** benchmark-value calibration; deferred.
- **I:** initial task-package implementation from a Stage G-cleared spec.
- **J:** offline Docker build and verification of an I package.
- **K:** isolated GLM solving, Claude scoring/reflection/attribution, and up to four verification rounds (with repairs after rounds 1-3).

## Topic draws (Stage A)

Draw weighted topics without calling a model:

```sh
python3 -m taskforge.cli --stage-a --seed 7 --count 10
```

`--topic-plan` is an equivalent general topic-plan view. Both use the weighted hierarchical sampler and a reproducible seed.

## Idea generation (Stage B)

Install model and MCP dependencies:

```sh
python3 -m pip install -e '.[stage-b]'
```

Set `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, and the reference-catalog environment variable. The reference server serves the local catalog through MCP; the GitHub server is configured read-only. Copy `config/stage-b-mcp.example.json` to `config/stage-b-mcp.json` and set its catalog path and token environment variables.

Generate one idea, drawing a topic if one is not supplied:

```sh
python3 -m taskforge.cli --stage-b --mcp-config config/stage-b-mcp.json --seed 7 --count 1 --output stage_b_runs
```

## Run Stages A-D

This runs the weighted draw, MCP idea generation, editor, and novelty judge in sequence. Each topic draw gets at most two ideas; if both are restarted or rejected, the workflow draws another topic up to `--max-topic-draws` (hard cap 2).

```sh
python3 -m taskforge.cli --stage-abcd --mcp-config config/stage-b-mcp.json --seed 7 --count 1 --max-topic-draws 2 --output stage_abcd_runs
```

The helper script runs the same workflow:

```sh
./scripts/run-stages-abcd.sh
```

Stage B outputs include `idea.json`, provenance, MCP tool-call records, and repository research. The accepted idea and the C/D review records are saved in the A-D workflow directory.

## Run Stages E-G

Stages E-G start from an accepted idea. Stage E records whether repository preflight passes or is not applicable; Stage F writes a structured specification; Stage G reviews and revises it, up to three times. Invalid model JSON gets one repair request with the original prompt and validation error. If the third reviewer still requests changes, the latest revised spec proceeds to Stage I for implementation, with the review-limit outcome recorded in `status.json` and `stage-g/review.json`. A reviewer request for author clarification still stops the workflow.

For a standalone accepted idea, run:

```sh
python3 -m taskforge.cli --stage-efg --idea path/to/accepted-idea.json --baseline-config config/stage-e-baseline.example.json --output stage_efg_runs
```

Omit `--baseline-config` when `repository_selection` is null. The output contains `stage-e/preflight.json`, `stage-f/task-spec.json`, per-reviewer records under `stage-g/`, and the final revised spec. To run the complete A-G workflow:

```sh
python3 -m taskforge.cli --stage-abcdefg --mcp-config config/stage-b-mcp.json --seed 7 --count 1 --max-topic-draws 2 --output stage_abcdefg_runs
```

## Run Stage I: implement a Stage G specification

Stage I uses the Claude Agent SDK (the Claude Code agent runtime) routed through a localhost accounting proxy to GLM-5.3-Flash at Z.ai's Anthropic-compatible endpoint. Put `GLM_API_KEY` or `ZAI_API_KEY` in the project `.env`; TaskForge loads it without overriding shell-exported values. The proxy binds only to `127.0.0.1`, forwards requests to Z.ai, and meters token counts from the final response event. It calculates cost at `$0.15/M` input tokens (including cached input) and `$0.50/M` output tokens by default; override with `GLM_INPUT_USD_PER_MTOK` and `GLM_OUTPUT_USD_PER_MTOK`. If any completed response lacks usage, the proxy blocks further requests rather than silently treating missing usage as free. Claude Code's own USD cap is disabled for GLM because its built-in model prices are for Claude, not Z.ai. The proxy stops new model requests after the configured GLM limit; a request already in flight may finish and exceed it. Stage I writes the complete ordered prompt, response, tool call/result, and per-provider-response cost stream to `agent-transcript.jsonl`, with a readable multi-line companion `agent-live.log`; it also prints prompt/response and live cost milestones to the run's Terminal. Stage I requires Docker and pulls `python:3.12-slim` if it is not already local. Its shell tool executes only inside a network-disabled container with CPU, memory, process, and command-time limits; TaskForge package tools remain available for role-aware file writes. GLM output is capped at 64,000 tokens per request; Stage I defaults to 32 agent turns (`GLM_STAGE_I_MAX_TURNS` can set 1-64) and one deterministic validation repair. `GLM_STAGE_I_MAX_COST_USD` and `GLM_STAGE_I_HARD_MAX_COST_USD` default to `$1.50`. To retain the prior direct GLM API harness, set `TASKFORGE_STAGE_I_PROVIDER=glm`; to use the direct Anthropic Stage I backup, set it to `claude` and provide `ANTHROPIC_API_KEY`. GLM routing can be adjusted with `GLM_ANTHROPIC_BASE_URL` and `GLM_CLAUDE_CODE_MODEL`.

```sh
python3 -m pip install -e '.[stage-i]'
python3 -m taskforge.cli \
  --stage-i \
  --spec stage_abcdefg_runs/<generation>/<task-slug>/stage-g/revised-task-spec.json \
  --approval-status stage_abcdefg_runs/<generation>/<task-slug>/status.json \
  --stage-i-output stage_abcdefg_runs/<generation>/<task-slug>/stage-i
```

To use the preserved Claude Stage I implementation for a run, set `TASKFORGE_STAGE_I_PROVIDER=claude` before the same command. GLM Stage I writes its request/tool transcript to `agent-transcript.jsonl`, token usage, configured rates, and estimated spend to `provenance.json`, and leaves partial files plus an `implementation_failed` status if generation stops early.

## Run Stage J: verify an I package

Stage J builds an offline runtime image from pinned dependencies, runs the reference and grader checks in isolated Docker containers, records the results, and leaves the Stage I package intact. An automated pass is reported as `needs_human_review`; inspect domain correctness, identifiability, grader leakage, and shortcut resistance before treating a task as verified. The package authoring manifest and dependency example are `config/stage-j.example.json` and `config/stage-j-requirements.example.in`.

```sh
python3 -m taskforge.cli --stage-j --stage-i-output path/to/stage-i-output
```

To generate an initial package while skipping deferred Stage H calibration, use the A-G-I campaign. It counts generated packages and tries fresh candidates up to `--max-i-candidates`:

```sh
python3 -m taskforge.cli --stage-abci --mcp-config config/stage-b-mcp.json --seed 7 --count 1 --max-topic-draws 2 --max-i-candidates 5 --output stage_abci_runs
```

The campaign writes `campaign-status.json` with each candidate's result. Stage J verification remains a separate command.

## Run Stage K: solver, scoring, reflection, and curation

Stage K requires Docker, the `anthropic` Python package and `ANTHROPIC_API_KEY` for Claude scoring, plus `GLM_API_KEY` or `ZAI_API_KEY` for the solver. By default, Claude Code's agent runtime uses GLM-5.3-Flash through the localhost accounting proxy; its custom `code_exec`, `finish`, and `abandon` tools bridge to the existing network-disabled Docker workspace. The proxy meters Z.ai final response usage at the rates described above and disables Claude Code's incompatible USD estimate. GLM output is capped at 32,000 tokens per request and 32 agent turns across the complete Stage K run (`GLM_STAGE_K_MAX_TURNS` can set 1-64). The solver has an eight-hour wall-clock limit and a `$1.50` spend limit (`GLM_STAGE_K_TARGET_COST_USD` and `GLM_STAGE_K_MAX_COST_USD`); a request already in flight may finish and exceed the limit. Set `TASKFORGE_STAGE_K_SOLVER=glm` to use the preserved direct GLM harness. Claude Sonnet 5.5 still scores each rubric item, reflects on apparent full-credit passes for loopholes, and attributes findings; those Anthropic API charges are separate and are not included in the GLM cap. Stage 6 repairs findings labeled Rubric or Task (including combined Agent labels) after verification rounds 1-3. If such defects remain after round 4, the candidate is discarded. The full trajectory is saved; review prompts include every event with long command outputs clipped. Solved tasks remain in the MVP benchmark and are labeled `glm_solved`.

```sh
python3 -m pip install -e '.[stage-k]'
python3 -m taskforge.cli --stage-k \
  --stage-i-output path/to/stage-i-output \
  --stage-k-output path/to/new-stage-k-run
```

Optionally pass `--lineage-memory path/to/lineage-memory.json` to use a shared lineage file. Stage K stores per-round Stage J reports, solver traces and deliverables, scores, reflection, attribution, repairs, and a top-level `report.json`.

## Run a batch end-to-end with live progress

`--stage-full` creates questions sequentially through A-G, I, J, and K (H remains deferred). It prints each completed stage, displays the generated idea before C reviews it, and displays the final revised spec before I begins package implementation. Terminal output stays in the scrollback while later model calls run. The output directory has one `candidate-NNNN/` folder per attempt, `results.csv` (one row per attempt, including discarded candidates), `progress.jsonl`, `progress-status.json`, and `campaign-status.json`. The CSV distinguishes `glm_solved` from `benchmark_retained`.

```sh
set -a && source .env && set +a && out="full_run_$(date +%Y%m%d_%H%M%S)" && .venv/bin/python -u -m taskforge.cli --stage-full --mcp-config config/stage-b-mcp.json --seed 21 --count 5 --max-candidates 15 --output "$out"
```

`--count` is the number of retained questions requested; `--max-candidates` bounds attempts in case candidates fail or are discarded. Pass `--baseline-config` when repository-backed ideas need a Stage E baseline. Use `--hide-artifacts` to print progress and saved paths without printing idea/spec contents.

## Limits

This is an early pipeline slice, not a full benchmark harness. A Stage G-approved spec or a spec proceeding after the review limit is not evidence that its domain claims or checks are correct. Stage I shell checks execute generated code only inside the network-disabled authoring container; Stage J remains the package build and verification authority, and its automated checks still require human triage. Stage K adds one GLM solve and model-based scoring, reflection, attribution, and repair; these reviews do not independently establish domain correctness or guarantee benchmark quality.
