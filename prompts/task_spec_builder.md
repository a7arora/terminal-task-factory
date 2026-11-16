# Stage F: produce a build-ready task specification

Turn the accepted idea into a complete, internally consistent handoff that a task author can implement without making new decisions about the task's objective, inputs, expected output, or grading. Preserve the idea's category, subcategory, objective, core challenge, and expected deliverable exactly. The output is a specification, not a task package: do not claim that files, fixtures, reference solutions, graders, tests, measurements, or validations already exist or have run.

Stage F has no shell, browser, repository, or calculation tools. Treat the accepted idea and Stage E report as the only evidence supplied. You may reason from that evidence and make conventional engineering choices, but do not invent scientific facts, repository behavior, data contents, reference answers, measured runtimes, or successful test results. Distinguish clearly between (a) evidence in the supplied material, (b) a design choice you are making, and (c) work an author must perform and verify.

## Authoring target

Make the spec as ready to build as the evidence permits:

- Make reasonable, low-risk engineering decisions yourself. Do not defer routine choices such as file names, serialization formats, command names, deterministic seeds, tie behavior, or error handling when they do not change the skill being tested.
- Fix every task-facing convention that affects solver behavior or grading: input schema, units, ordering, limits, output schema, tolerances, dependency versions where known, resource limits, and network policy. Use one authoritative value consistently; never leave a rule as “see README” unless the README is itself a named solver-visible file whose required contents are specified here.
- If a missing choice would materially change the benchmark, do not silently choose or fabricate evidence. State the exact author action, the artifact or measurement needed, and how its result affects the task. Put it in `open_risks`; repeat the task-facing provisional rule only when a solver needs one before calibration. Do not call the spec fully build-ready when a blocker remains.
- Keep author actions concrete and finite. “Validate the math” is too vague. Name what must be computed or inspected, the precision or acceptance criterion, the independent check, and what to change if it fails.
- Avoid speculative complexity. Keep the task bounded to the accepted objective and explain any simplification needed to make it feasible.

## Evidence and repository rules

Use the Stage E report as evidence. If a repository was selected, use only its pinned revision and the baseline evidence recorded in Stage E. Specify the starting state, relevant files, commands, expected behavior, and any required repository setup from that evidence. Do not infer unreported repository facts. If repository evidence is missing or failed, identify the exact author action and do not describe the repository task as ready to build.

If no repository was selected, design a self-contained offline environment unless the accepted idea requires otherwise. Describe how every input fixture will be created or supplied. Do not list hypothetical files as though they exist. For generated fixtures, specify the schema, generation method, seed policy, and which data or answers must remain hidden from the solver.

## Complete task contract

Before writing the JSON, settle the following in the relevant fields. State “not applicable” with a short reason when a dimension truly does not apply.

1. **Solver goal:** one clear end state, matching the accepted objective; no extra deliverable or hidden subtask.
2. **Starting environment:** operating system or container assumptions, initial files and state, available tools and versions where known, network policy, and relevant resource/time limits. Do not invent measured limits. If calibration is required, give a provisional limit and a concrete calibration action.
3. **Inputs:** exact file paths, formats, schemas, units, ordering, number/range of cases, provenance or generation plan, and any assumptions the solver may rely on.
4. **Instructions:** exact commands or interfaces where known, allowed and disallowed methods, expected runtime/resource bounds, and how to handle errors or ambiguous cases.
5. **Deliverable:** exact output paths, schema, types, normalization, coverage requirements, and examples only when examples do not leak hidden answers.
6. **Reference solution:** a stepwise implementable method, required dependencies, deterministic behavior, expected complexity, and any independent evidence needed to establish correctness. Do not give a hand-wavy method where a key algorithmic step determines feasibility.
7. **Grader:** deterministic checks tied to the objective, including schema and coverage, correctness criteria and justified tolerances, hidden/public data separation, and important failure cases. State how all checks combine into the final verdict.
8. **Validation:** how an author will show the reference solution passes, the untouched or intentionally incorrect solution fails, generated fixtures are reproducible, and time/resource limits are met. Include targeted negative or mutation checks for the most likely shortcuts and grader mistakes.

## Method-specific audit

Identify the task's actual reasoning or computational method, then add only the checks relevant to it. Examples: numerical tasks need precision, convergence, conditioning, and independent-reference checks; data tasks need provenance, leakage, split, and label checks; software tasks need exact interfaces, repository state, and behavioral tests; formal proof tasks need the theorem statement, assumptions, trusted axioms, and proof-check command; scientific or simulation tasks need units, governing assumptions, boundary/initial conditions, and reference validation. These are examples, not mandatory sections for every task.

For any claim that controls whether an answer is correct or whether the task is feasible, say how it will be verified. Do not treat a proposed test as evidence that the test passed. If independent verification is important, specify what makes the methods independent; rerunning the same code is not an independent check.

## Final consistency audit

Before returning the spec, check it against the following questions and fix every answerable issue:

- Can an author build the described initial environment from the named files and instructions without guessing about the task's intended behavior?
- Can a solver identify every input, perform the requested work, and produce exactly the required deliverable?
- Does every grader check have a concrete method, a pass condition, and a clear relationship to the objective?
- Are thresholds, coefficients, runtime/resource limits, schemas, and conventions fixed and consistent everywhere they appear?
- Are all claims about repositories, fixture contents, numerical results, proofs, and validation supported by supplied evidence or explicitly marked as unverified author work?
- Are important failure modes tested, including malformed outputs, incomplete coverage, plausible shortcuts, and incorrect reference/grader behavior?
- Are unresolved items specific, finite, assigned to the author, and labeled as blocking only when proceeding would risk changing the benchmark or making it unsound?

Do not optimize for a reviewer returning `approved`. Optimize for a precise, truthful handoff. Reviewers must remain free to identify substantive errors; do not hide uncertainty or pre-emptively claim approval.

## Required response format

Return only valid JSON with exactly this structure and no markdown fences:

{
  "task_id": "lowercase-kebab-case identifier",
  "title": "short task title",
  "category": "same category as the idea",
  "subcategory": "same subcategory as the idea",
  "objective": "specific end state the solver must achieve",
  "core_challenge": "copy the accepted idea's core challenge verbatim",
  "expected_deliverable": "copy the accepted idea's expected deliverable verbatim",
  "solver_instructions": "complete instructions, including exact inputs, output paths, schemas, conventions, limits, and constraints",
  "environment_plan": {
    "starting_state": "what the solver sees on entry",
    "files": [
      {"path": "relative/path", "purpose": "why it exists", "contents_or_generation_notes": "what it contains or exactly how the author creates it"}
    ],
    "dependencies": ["runtime dependency or none"],
    "network_access": false
  },
  "reference_solution_plan": "implementable reference approach and correctness evidence required",
  "grader_plan": {
    "artifacts": ["required output path"],
    "checks": [
      {"name": "check name", "method": "deterministic procedure", "pass_condition": "observable condition"}
    ],
    "important_failure_cases": ["plausible incorrect result the checks must reject"]
  },
  "validation_plan": {
    "reference_solution_passes": "how to demonstrate a correct solution passes; this is a plan, not a claim it has passed",
    "untouched_environment_fails": "how to demonstrate the initial state or an intentionally wrong submission fails",
    "environment_checks": ["reproducibility, dependency, resource, or other relevant checks"]
  },
  "open_risks": ["specific unresolved risk and author action, or an empty array if none"]
}

Each object in `environment_plan.files` describes exactly one clean, relative path from the task workspace root, such as `data/input.csv`. Do not use an absolute path, include a workspace/container root prefix, add explanatory prose to the path, or combine multiple filenames in one value. Use the same rule for every path field: one path per value and separate list items for separate artifacts.

Copy the accepted idea's `category`, `subcategory`, `objective`, `core_challenge`, and `expected_deliverable` exactly where required. Do not add a new domain, objective, deliverable, or major workflow. Do not put hidden answers or grader-only data in solver-visible files. Every `open_risks` item must state the unresolved issue and the author's next action; if no unresolved risks remain, return `[]`.

## Accepted idea

{{IDEA_JSON}}

## Stage E baseline report

{{STAGE_E_JSON}}
