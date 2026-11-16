from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from dotenv import load_dotenv

from .distribution import TopicDraw, draw_topic_batch, load_distribution
from .families import generate_weighted_batch
from .pipeline import package_task
from .prompting import render_idea_prompt, render_novelty_prompt
from .stage_b import run_stage_b
from .stage_f import StageFSpecValidationError
from .stage_g import StageGReviewValidationError
from .stage_i import run_stage_i
from .stage_j import run_stage_j
from .stage_k import run_stage_k
from .progress import ProgressReporter
from .workflow import run_stage_abcd
from .workflow_abci import run_stage_abci
from .workflow_abcdefg import run_stage_abcdefg
from .workflow_efg import run_stage_efg
from .workflow_full import run_stage_full


def main() -> None:
    # Load local credentials/config without overriding values already exported by the shell.
    project_env = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(project_env, override=False)
    parser = argparse.ArgumentParser(description="Generate terminal-agent task packages")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("generated_tasks"))
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--weights", type=Path, help="JSON object mapping task-family names to relative weights")
    parser.add_argument("--topic-plan", action="store_true", help="draw benchmark topics only")
    parser.add_argument("--stage-a", action="store_true", help="draw weighted category/subcategory topics only")
    parser.add_argument("--stage-b", action="store_true", help="generate an idea for a supplied or sampled topic using MCP tools")
    parser.add_argument("--stage-abcd", action="store_true", help="run topic draw A, idea generation B, editor C, and novelty review D")
    parser.add_argument("--stage-abcdefg", action="store_true", help="run the full A-G topic, idea, review, preflight, specification, and review workflow")
    parser.add_argument("--stage-efg", action="store_true", help="run repository preflight E, spec drafting F, and independent review G for an accepted idea")
    parser.add_argument("--stage-i", action="store_true", help="implement a task package with Claude Code + GLM (default), direct GLM, or Claude backup")
    parser.add_argument("--stage-j", action="store_true", help="build and verify a Stage I task package in an isolated offline Docker container")
    parser.add_argument("--stage-k", action="store_true", help="run Claude Code + GLM-5.3-Flash solver and Claude curation for up to four verification rounds")
    parser.add_argument("--stage-abci", action="store_true", help="run A-G and then package implementation I, skipping the deferred Stage H calibrator")
    parser.add_argument("--stage-full", action="store_true", help="create a batch of questions end-to-end through A-G, I, J, and K with live terminal progress")
    parser.add_argument("--spec", type=Path, help="Stage G task-spec.json for --stage-i")
    parser.add_argument("--approval-status", type=Path, help="Stage G status.json allowing the spec to proceed to Stage I")
    parser.add_argument("--stage-i-output", type=Path, help="new directory for Stage I artifacts and generated task package")
    parser.add_argument("--stage-j-output", type=Path, help="directory for Stage J verification artifacts; defaults to <stage-i-output>/stage-j")
    parser.add_argument("--stage-k-output", type=Path, help="directory for Stage K execution and curation artifacts; defaults to <stage-i-output>/stage-k")
    parser.add_argument("--lineage-memory", type=Path, help="optional shared JSON lineage-memory file for Stage K")
    parser.add_argument("--max-i-candidates", type=int, default=25, help="maximum fresh Stage G-cleared candidates to try for --stage-abci")
    parser.add_argument("--max-candidates", type=int, default=25, help="maximum end-to-end candidate tasks to try for --stage-full")
    parser.add_argument("--hide-artifacts", action="store_true", help="show stage progress without printing idea/spec artifact contents")
    parser.add_argument("--max-topic-draws", type=int, default=2, help="maximum topic draws per requested idea (hard cap: 2)")
    parser.add_argument("--mcp-config", type=Path, help="MCP server JSON config for Stage B idea generation")
    parser.add_argument("--topic-draw", type=Path, help="fixed topic draw JSON, used for a same-topic Stage B revision")
    parser.add_argument("--draw-index", type=int, default=1, help="stable output/provenance index for a fixed topic draw retry")
    parser.add_argument("--difficulty-retry-summary", type=Path, help="brief prior too-easy task summary for a Stage B hardening retry")
    parser.add_argument("--idea-prompt", action="store_true", help="draw a topic and render the idea-generator prompt")
    parser.add_argument("--novelty-prompt", action="store_true", help="render the separate novelty-judge prompt")
    parser.add_argument("--candidate", type=Path, help="JSON task idea for novelty review")
    parser.add_argument("--references", type=Path, help="JSON array of same-subcategory reference task cards")
    parser.add_argument("--revision-feedback", type=Path, help="feedback from a previous novelty review")
    parser.add_argument("--previous-idea", type=Path, help="previous rejected idea, supplied on a retry")
    parser.add_argument("--idea", type=Path, help="accepted idea JSON input for --stage-efg")
    parser.add_argument("--baseline-config", type=Path, help="JSON containing a local Docker image and offline build/test commands")
    args = parser.parse_args()

    distribution_path = Path(__file__).resolve().parents[1] / "config" / "terminal-bench-4.0.0-topic-distribution.json"
    distribution = load_distribution(distribution_path)

    if args.stage_full:
        incompatible = args.stage_k or args.stage_j or args.stage_i or args.stage_efg or args.stage_abcdefg or args.stage_abcd or args.stage_a or args.stage_b or args.stage_abci
        if incompatible:
            parser.error("--stage-full is a complete A-G-I-J-K campaign; do not combine it with another stage workflow")
        if args.mcp_config is None or not args.mcp_config.is_file():
            parser.error("--stage-full requires an existing --mcp-config")
        if args.baseline_config is not None and not args.baseline_config.is_file():
            parser.error(f"baseline config does not exist: {args.baseline_config}")
        if args.max_candidates < args.count:
            parser.error("--max-candidates must be at least --count")
        reporter = ProgressReporter(args.output, total=args.count, show_artifacts=not args.hide_artifacts)
        print(f"Starting full task campaign: target={args.count}, seed={args.seed}, candidate_limit={args.max_candidates}", flush=True)
        print(f"Output directory: {args.output.resolve()}", flush=True)
        try:
            csv_path = asyncio.run(run_stage_full(
                args.seed, args.count, args.max_topic_draws, args.max_candidates,
                distribution, args.output, args.mcp_config, args.baseline_config,
                args.lineage_memory, progress=reporter,
            ))
        except (StageFSpecValidationError, StageGReviewValidationError) as exc:
            parser.error(str(exc))
        status_path = args.output / "campaign-status.json"
        status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {"status": "unknown"}
        print(f"\nBatch status: {status['status']}")
        print(f"Results CSV: {csv_path}")
        print(f"Progress log: {args.output / 'progress.jsonl'}")
        print(f"Current status: {args.output / 'progress-status.json'}")
        if status["status"] != "completed":
            raise SystemExit(1)
        return

    if args.stage_k:
        incompatible = args.stage_j or args.stage_i or args.stage_efg or args.stage_abcdefg or args.stage_abcd or args.stage_a or args.stage_b or args.stage_abci
        if incompatible:
            parser.error("--stage-k runs independently; do not combine it with another stage workflow")
        if args.stage_i_output is None:
            parser.error("--stage-k requires --stage-i-output pointing to a completed Stage I output")
        report_path = run_stage_k(args.stage_i_output, args.stage_k_output, args.lineage_memory)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        print(f"Stage K {report['status']}: {report_path}")
        if report["status"] in {"failed", "discarded", "unsolved"}:
            raise SystemExit(1)
        return

    if args.stage_j:
        if args.stage_i or args.stage_efg or args.stage_abcdefg or args.stage_abcd or args.stage_a or args.stage_b or args.stage_abci:
            parser.error("--stage-j runs independently; do not combine it with another stage workflow")
        if args.stage_i_output is None:
            parser.error("--stage-j requires --stage-i-output pointing to a completed Stage I output")
        report_path = run_stage_j(args.stage_i_output, args.stage_j_output)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        print(f"Stage J {report['status']}: {report_path}")
        if report["status"] == "failed":
            raise SystemExit(1)
        return

    if args.stage_abci:
        if args.stage_i or args.stage_efg or args.stage_abcdefg or args.stage_abcd or args.stage_a or args.stage_b:
            parser.error("--stage-abci is a complete campaign; do not combine it with another stage workflow")
        if args.mcp_config is None or not args.mcp_config.is_file():
            parser.error("--stage-abci requires an existing --mcp-config")
        if args.baseline_config is not None and not args.baseline_config.is_file():
            parser.error(f"baseline config does not exist: {args.baseline_config}")
        if args.max_i_candidates < args.count:
            parser.error("--max-i-candidates must be at least --count")
        try:
            manifests = asyncio.run(run_stage_abci(args.seed, args.count, args.max_topic_draws,
                args.max_i_candidates, distribution, args.output, args.mcp_config, args.baseline_config))
        except (StageFSpecValidationError, StageGReviewValidationError) as exc:
            parser.error(str(exc))
        for manifest in manifests:
            print(f"Stage I task package: {manifest}")
        return

    if args.stage_i:
        if args.stage_j or args.stage_efg or args.stage_abcdefg or args.stage_abcd or args.stage_a or args.stage_b or args.stage_abci:
            parser.error("--stage-i runs independently; do not combine it with another stage workflow")
        if args.spec is None or args.approval_status is None or args.stage_i_output is None:
            parser.error("--stage-i requires --spec, --approval-status, and --stage-i-output")
        manifest = asyncio.run(run_stage_i(args.spec, args.approval_status, args.stage_i_output))
        print(f"Stage I result: {manifest}")
        return

    if args.stage_efg and args.stage_abcdefg:
        parser.error("choose either --stage-efg or --stage-abcdefg")
    if args.baseline_config is not None and not args.baseline_config.is_file():
        parser.error(f"baseline config does not exist: {args.baseline_config}")

    if args.stage_efg:
        if args.idea is None:
            parser.error("--stage-efg requires --idea pointing to an accepted idea JSON file")
        try:
            path = asyncio.run(run_stage_efg(args.idea, args.output, args.baseline_config))
        except (StageFSpecValidationError, StageGReviewValidationError) as exc:
            parser.error(str(exc))
        print(f"Stage G revised task specification: {path}")
        return

    if args.stage_abcdefg:
        if args.mcp_config is None or not args.mcp_config.is_file():
            parser.error("--stage-abcdefg requires an existing --mcp-config")
        try:
            paths = asyncio.run(run_stage_abcdefg(args.seed, args.count, args.max_topic_draws,
                distribution, args.output, args.mcp_config, args.baseline_config))
        except (StageFSpecValidationError, StageGReviewValidationError) as exc:
            parser.error(str(exc))
        for path in paths:
            print(f"Stage G revised task specification: {path}")
        return

    if args.stage_abcd:
        if args.mcp_config is None or not args.mcp_config.is_file():
            parser.error("--stage-abcd requires an existing --mcp-config")
        paths = asyncio.run(run_stage_abcd(args.seed, args.count, args.max_topic_draws,
            distribution, args.output, args.mcp_config))
        for path in paths:
            print(f"accepted Stage A-D idea: {path}")
        return

    if args.stage_b:
        if args.mcp_config is None or not args.mcp_config.is_file():
            parser.error("--stage-b requires an existing --mcp-config")
        hardening = args.difficulty_retry_summary.read_text(encoding="utf-8") if args.difficulty_retry_summary else None
        if args.topic_draw:
            draw_data = json.loads(args.topic_draw.read_text(encoding="utf-8"))
            topics = [TopicDraw(**draw_data.get("topic_draw", draw_data))]
        else:
            if hardening:
                parser.error("--difficulty-retry-summary requires --topic-draw to keep the original topic fixed")
            topics = draw_topic_batch(args.seed, args.count, distribution)
        feedback = args.revision_feedback.read_text(encoding="utf-8") if args.revision_feedback else None
        prior = json.loads(args.previous_idea.read_text(encoding="utf-8")) if args.previous_idea else None
        paths = asyncio.run(run_stage_b(topics, args.seed, args.output, args.mcp_config,
            difficulty_retry_summary=hardening, revision_feedback=feedback, prior_idea=prior,
            draw_index_start=args.draw_index))
        for path in paths:
            print(f"created Stage B idea: {path}")
        return

    if args.idea_prompt:
        topic = draw_topic_batch(args.seed, 1, distribution)[0]
        feedback = args.revision_feedback.read_text(encoding="utf-8") if args.revision_feedback else None
        prior = json.loads(args.previous_idea.read_text(encoding="utf-8")) if args.previous_idea else None
        print(render_idea_prompt(topic, feedback, prior))
        return
    if args.novelty_prompt:
        if args.candidate is None or args.references is None:
            parser.error("--novelty-prompt requires --candidate and --references JSON files")
        topic = draw_topic_batch(args.seed, 1, distribution)[0]
        idea = json.loads(args.candidate.read_text(encoding="utf-8"))
        references = json.loads(args.references.read_text(encoding="utf-8"))
        if not isinstance(references, list):
            parser.error("--references must contain a JSON array")
        print(render_novelty_prompt(topic, idea, references))
        return
    if args.topic_plan or args.stage_a:
        print(json.dumps([draw.to_dict() for draw in draw_topic_batch(args.seed, args.count, distribution)], indent=2))
        return

    weights = json.loads(args.weights.read_text()) if args.weights else None
    for task in generate_weighted_batch(args.seed, args.count, weights):
        path = package_task(task, args.output)
        print(f"released {task.task_id}: {path}")


if __name__ == "__main__":
    main()
