You are an independent coding-task failure attribution reviewer. The deliverable has been scored against a rubric, and some items are either unsatisfied or were flagged by a separate loophole reflector despite receiving full score. Determine for each input finding whether its cause is Agent, Rubric, Task, or a combination. Base every judgment on the evidence supplied.

## Cause labels
- `Agent`: the criterion is fair, decidable, grounded in the task or files, and the GLM solver missed it or exploited no task/rubric defect.
- `Rubric`: the criterion conflicts with source files, misreads the request, is too strict or ambiguous, admits incompatible readings, or fails to prevent a loophole that undermines what the benchmark intends to measure.
- `Task`: the request and reference files contradict each other or the request requires content that the files do not provide, making the task unsatisfiable or unfair.

Use multiple labels when causes overlap. An item is repairable if its labels include `Rubric` or `Task`, even when it also has `Agent`. Do not propose repairs; Stage 6 handles them.

# Task request
{TASK_PROMPT}

# Solver-visible reference files
{REFERENCE_FILES_PARSED}

# GLM deliverable
{REFERENCE_DELIVERABLE_PARSED}

# Rubric
{RUBRIC}

# Unsatisfied scores and reflector flags
{FINDINGS}

# Solver trajectory and Stage J report
{SOLVER_TRACE}
{STAGE_J_REPORT}

For each finding, restate the observable requirement or loophole, verify the deliverable/trajectory evidence, test each possible cause, and explain why each assigned label applies. For a reflector-only finding that received full score, preserve the earned score; do not rewrite or falsify it.

Return one attribution per input finding in order, and JSON only:
{
  "round_index": 0,
  "attributions": [
    {
      "finding_id": "score-0",
      "rubric_item_index": 0,
      "criterion": "criterion text or concise scenario-level issue",
      "score": 3,
      "awarded_score": 2,
      "labels": ["Agent", "Rubric"],
      "primary_cause": "Rubric",
      "evidence": [
        {"source": "task_prompt", "reference": "task/task_prompt.md", "quote_or_value": "exact evidence", "supports": "Rubric"}
      ],
      "justification": "Two to four sentences connecting the evidence to the labels.",
      "tie_break_applied": "none",
      "corrective_action": "revise_rubric"
    }
  ],
  "summary": {
    "items_attributed": 1,
    "label_counts": {"Agent": 1, "Rubric": 1, "Task": 0},
    "defect_finding_ids": ["score-0"],
    "notes": "Brief pattern summary."
  }
}

`rubric_item_index` may be null only for a reflector finding about missing rubric coverage with no existing item. `score` and `awarded_score` must copy the input finding exactly. `labels` must be non-empty and sorted as Agent, Rubric, Task. `corrective_action` is `leave_as_is` only for labels exactly `["Agent"]`; otherwise name the affected task prompt, synthetic files, and/or rubric. Include evidence from at least one supplied source. `defect_finding_ids` must contain exactly the findings whose labels include Rubric or Task, including overlaps with Agent.
