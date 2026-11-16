# MVP plan

The MVP generates and reviews candidate ideas, prepares an implementation specification, and can produce and verify an initial task package. An approved specification is a review milestone, not empirical proof that a task is correct.

## Pipeline

1. **Stage A — Weighted topic draw.** Sample a category and subcategory reproducibly from the configured hierarchical distribution.
2. **Stage B — Idea generation.** Use the reference-task MCP server and read-only GitHub MCP server to propose an original task for the sampled topic. Record tool calls and evidence.
3. **Stage C — Clarity/editor review.** Check whether the idea is coherent and sufficiently specified. If a protected field must change, restart idea generation for the same topic.
4. **Stage D — Novelty review.** Compare the idea with visible tasks from the exact sampled subcategory. Each topic draw gets at most two ideas before the sampler moves on.
5. **Stage E — Repository preflight.** Record `not_applicable` for self-contained ideas. For repository-backed ideas, fetch the pinned commit and run configured baseline commands in an available Docker image with networking disabled.
6. **Stage F — Specification drafting.** Expand the accepted idea and Stage E report into a structured specification for a human task author.
7. **Stage G — Specification review.** Make up to three independent review calls. Reviewers may request evidence-supported changes or approve an unchanged spec. Preserve the accepted objective and flag unresolved decisions.
8. **Stage H — Benchmark-value calibration.** Deferred. It is not currently implemented.
9. **Stage I — Package implementation.** Produce an initial task package from a Stage G-approved specification. Generated code remains unexecuted.
10. **Stage J — Package verification.** Build and run the package in an offline Docker environment, record checks, and route results for human triage.
11. **Stage K — Solver evaluation and curation.** Run GLM-5.3-Flash in the Stage J image with an eight-hour wall-clock cap and ten-minute command cap. Claude Sonnet 5.5 scores the rubric, reflects on rubric-passing runs for loopholes, and attributes failures and flags. The full solve/score/reflect/attribute cycle runs up to four times; Stage 6 can repair task/rubric/synthetic-file defects after rounds 1-3. A solved task is retained and labeled `glm_solved`; unresolved defects after round 4 are discarded.

If all three Stage G reviewers request changes, the A-G workflow makes a bounded full-generation restart. The standalone `--stage-efg` command can restart the full A-G workflow after reviewer exhaustion. The campaign `--stage-abci` skips deferred Stage H and counts Stage I packages.

Stage F validates generated specs in Python and makes one repair call when parsing or schema validation fails. Stage G applies the same one-repair policy to each reviewer response. Both save raw attempts and validation errors.

## Completion and artifacts

A specification candidate is complete when Stages A-D accept an idea, Stage E passes or records `not_applicable`, Stage F writes a spec, and a Stage G reviewer approves it without changes. Approval does not mean that its scientific claims, environment, reference solution, or grader have been empirically validated.

The E-G workflow writes:

- `stage-e/preflight.json`
- `stage-f/task-spec.json` and `stage-f/provenance.json`
- `stage-g/reviewer-1/` through `reviewer-3/`, with specs, findings, changes, and provenance
- `stage-g/revised-task-spec.json`, `stage-g/review.json`, and `status.json`

`--stage-abcdefg` runs the complete A-G workflow; `--stage-efg` starts from an accepted `idea.json`. `--stage-i` implements an approved specification; `--stage-j` verifies an I package; `--stage-k` runs the solver and curation path. `--stage-abci` runs A-G and Stage I while skipping deferred Stage H.
`--stage-full --count N` runs the full A-G-I-J-K candidate path sequentially until it retains N questions or reaches `--max-candidates`. It writes `results.csv`, `progress.jsonl`, `progress-status.json`, and `campaign-status.json` in the output directory. It prints the Stage B idea before editor review and the Stage G spec before Stage I implementation.

## Timing and limits

The idea-generation prompt does not receive a solver timeout. The Stage F schema does not yet define expert-time estimates or agent timeouts. Stage G can flag missing time/resource expectations, but does not compute a limit. Do not infer solver duration from reference-solution runtime.

Stage I can draft a package but does not execute it. Stage J supports offline Docker validation, but an automated pass still requires human review for domain correctness, identifiability, leakage, and shortcut resistance. The system does not run solver trials or estimate per-task success rates.
