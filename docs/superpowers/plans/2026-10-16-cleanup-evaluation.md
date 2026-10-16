# Edit-selection cleanup evaluation implementation plan

**Goal:** Evaluate selections of immutable trajectory edits before connecting a model or trainer.

**Architecture:** Add an experiment package under the SWE-bench frontend. Reuse trajectory
preparation, edit application, proxy feedback, and minimization. Reconstruct in disposable
clones; never reset or clean a supplied repository. Record held-out evaluation separately.

**Tech stack:** Python 3.12, stdlib dataclasses/JSON/subprocess, existing TRIM SWE-bench extra.

**Design:** The in-chat design approved on 2026-10-16: select original edit IDs only; preserve
contents and execution order; separate invalid candidates from infrastructure errors.

## Global constraints

- No patch rewriting, new model edits, or model-controlled test changes.
- No modifications to tests or spec files. Exercise temporary fixtures outside tests/.
- No shared-software changes, dataset downloads, image pulls/builds, or model calls.
- No commits unless requested; never include AI attribution or co-author trailers.
- Existing core, strategies, and SWE-bench CLI behavior must remain unchanged.
- Execute inline in the existing rl-experiments checkout; no parallel branch work is needed.

## Files and interfaces

- Create `src/patch_minimizer/frontends/swebench/experiments/selection.py`: preparation,
  input validation, immutable selection, strict reconstruction, typed exceptions.
- Create `src/patch_minimizer/frontends/swebench/experiments/cleanup.py`: CLI, artifacts,
  staged evaluation, resume identity, cost records, and exit statuses.
- Create `src/patch_minimizer/frontends/swebench/experiments/AGENTS.md`: usage and invariants.
- Update `src/patch_minimizer/frontends/swebench/AGENTS.md` with the experiment entry point.
- Expected implementation: four source/documentation files, approximately 500–800 lines.
  Obtain the repository's G-5 confirmation before implementing this scope.

## Review focus

1. Boolean, duplicate, unknown, reordered, and stale edit selections.
2. Dependent edits, empty selections, new/deleted files, and silently skipped edits.
3. Caller repositories with existing work, symlinks/path escape, and missing base commits.
4. Stale evaluation reports and changed image/configuration/source inputs on resume.
5. Empty or weak proxy tests, timeouts, and infrastructure errors mistaken for model failures.

## Task 1: Prepare and reconstruct selections

- [x] Read applicable frontend/core guidance and anchors before editing.
- [x] Define `SelectionError(ValueError)` and `ReconstructionError(SelectionError)`.
- [x] Implement `prepare_trajectory(traj: Path, instance_id: str) -> dict` using
  `minimize_traj(only_parse=True)`; export original stamped IDs and all edit fields.
- [x] Produce a versioned manifest with source SHA-256, instance ID, ordered edits,
  issue text when available, original submission, frozen test files, and proxy commands.
  Export model-facing inputs separately; exclude tests, gold patches, and TRIM choices.
- [x] Accept exactly `{"manifest_sha256": "...", "retained_edit_ids": [0, 2]}`.
  Reject extra keys, non-integer IDs including bool, duplicates, unknown IDs, and stale
  manifest hashes. Normalize a valid selection into original execution order.
- [x] Implement `select_edits(patch_list: list, ids: list[int]) -> list` with deep copies;
  preserve original IDs and nodes, omit empty nodes, and never apply parent snapshots
  that could reintroduce dropped edits.
- [x] Implement `reconstruct_selection(repo: Path, base_commit: str, patch_list: list) -> str`.
  Create a temporary local clone at the requested commit. Validate edit/source paths stay
  inside it, including COPY_FILE sources and symlink resolution. Use the existing applier
  under RWLock on copies; require every selected operation to apply, including best-effort
  edits. Track new files in generated diffs and delete only the owned temporary directory.
- [x] Check the keep-all replay against the original submission's resulting file contents
  on a clean base. Record unsupported replay mismatches; do not call them cleanup failures.
- [x] Verify with temporary repositories: dependent edits, reordered IDs, empty selection,
  create/delete, unknown/duplicate/bool IDs, stale hashes, path escape, immutable inputs,
  and an untouched dirty caller repository. Do not add or modify repository tests.

## Task 2: Add staged evaluation and records

- [x] Provide `python -m patch_minimizer.frontends.swebench.experiments.cleanup` with
  `prepare`, `reconstruct`, and `evaluate` subcommands, plus complete CLI help.
  Prepare/reconstruct must work offline without Docker or Hugging Face dataset access.
- [x] Require explicit trajectory, instance ID, output path, selection, repository, and
  base commit as appropriate. Require output locations outside the supplied repository.
- [x] On evaluation, ping Docker and inspect the required existing image before dataset
  access. Never pull/build automatically; return an actionable prerequisite error.
- [x] Validate proxy commands once against the original submission, save accepted/rejected
  commands, then freeze accepted commands/test files for all candidates. No surviving
  commands means unavailable feedback, never success.
- [x] Evaluate original and keep-all replay before the selected candidate. Use original
  replay checks to separate parser/reconstruction problems from selection failures.
- [x] Reuse `SWEBenchEnvironment` and `minimize_edit_list` for selected-candidate proxy
  evaluation and residual minimization, using a separate disposable clone. Preserve IDs
  and use strict reconstruction to audit the resulting retained selection as well.
- [x] Run held-out SWE-bench evaluation only behind an explicit evaluation flag; use the
  existing harness integration with unique run identities and no implicit image download.
  Do not use held-out outcomes to select edits or tune proxy commands.
- [x] Persist versioned atomic JSON and separate patch/log artifacts after each stage.
  Identity includes trajectory, manifest, base commit, selection, frozen tests/commands,
  source revision plus dirty-source digest, package versions, image ID, and timeouts.
  Resume only exact identities with completed stages; never cache infrastructure errors.
- [x] Record statuses: completed, invalid_selection, invalid_candidate, original_failed,
  replay_mismatch, infrastructure_error, and interrupted/incomplete stages. Keep proxy
  outcomes and held-out outcomes separate. CLI exits nonzero on invalid/error outcomes.
- [x] Record selected/original/final edit counts, changed lines, wall time per stage,
  test executions and existing minimizer feedback stats. Zero selected edits are valid
  structurally; report residual fraction as null for a zero denominator.
- [x] Verify orchestration with injected fake evaluators in temporary checks: frozen
  feedback, original failure, candidate failure, Docker errors, resume invalidation,
  empty feedback, and completed-stage recovery. Label these simulated, not Docker tests.

## Task 3: Document and verify

- [x] Document manifest/selection examples, CLI commands, status semantics, local artifact
  paths, and the distinction between proxy preservation and held-out correctness.
- [x] Add concise AIDEV-NOTE anchors at immutable-selection, strict replay, frozen-feedback,
  and evaluation-identity boundaries. Update frontend AGENTS.md with the entry point.
- [x] Run compile/import and CLI-help checks, the temporary integration checks above,
  and `bash tests/run_parsing_benchmarks.sh swe_bench_swe_agent` in the Python 3.12 env.
- [x] Review the full diff against the approved contract and verify tests/specs untouched.
  Report unavailable Docker/model verification explicitly. Do not claim a real end-to-end
  benchmark run until prerequisites are resolved and a real instance has passed.

## Completion evidence

Implemented in the approved rl-experiments checkout without commits. Four source/documentation
files comprise the implementation; this plan records the work. No tests or spec files changed.

- Fourteen temporary checks passed, including real Git replay, CLI reconstruction, the actual
  TRIM minimizer with simulated feedback, output locking, retry/resume, and adapter behavior.
- Existing SWE-bench parser regression: 333/333 matched, zero mismatches.
- Compilation, CLI help, source whitespace, and 96-character line checks passed.
- Offline prepare ran on django__django-13516; sample files are under results/cleanup-pilot/prepared.
- Docker preflight fails on account/socket permissions. Real proxy/held-out execution is unverified.
- No datasets downloaded, images pulled/built, model requests made, or training started.

Rulings from implementation/review: reject proxy-file overlap rather than risk source injection;
reject proxies that pass on the empty base rather than report a misleading reward; normalize COPY_FILE
paths and use a direct validated copy; force empty patches through the same container reset path.
Arbitrary discovery-suite variability remains an explicit limitation requiring cohort audit.
