# Cleanup experiments

This package evaluates selections of existing SWE-agent edits. Models may return only edit
IDs to retain; they cannot replace edit contents, reorder execution, or introduce edits.
Run commands from the repository root in the isolated environment:

```bash
source .trim-env/venv/bin/activate
python -m patch_minimizer.frontends.swebench.experiments.cleanup --help
```

## Files and invariants

| File | Responsibility |
|---|---|
| `selection.py` | Manifest integrity, selection validation, strict ordered replay in temporary clones |
| `cleanup.py` | CLI, Docker preflight, frozen proxy evaluation, TRIM adapter, staged records/resume |

- IDs are stamped by the existing parser before submission-path filtering; gaps are normal.
- Parent diffs are snapshots and must never be replayed to restore dropped edits.
- Replay uses exact paths and the original edit semantics. Unsupported, missing, and
  best-effort operations that cannot apply fail explicitly. Symlink paths are unsupported.
- Caller repositories are read only. All checkout/reset/clean operations happen in owned
  temporary clones; outputs must be outside the supplied target repository.
- Test/spec files are human-owned. Temporary verification fixtures belong outside tests/.

## Prepare and select (offline)

```bash
python -m patch_minimizer.frontends.swebench.experiments.cleanup prepare \
  --traj tests/parsing_benchmark_tests/swe_bench_swe_agent/trajs/django__django-13516.traj \
  --bug-id django__django-13516 --output results/cleanup-pilot/prepared
```

Use `--issue-file PATH` when the trajectory does not contain `info.problem_statement`.
Without either source, the exported issue is empty; no dataset is downloaded to fill it.

Outputs: evaluator-only `manifest.json`, `model-input.json`, and a `keep-all.json` control.
Only give the model `model-input.json`: it contains issue text and selectable edits, without
proxy commands/files, cumulative diff snapshots, TRIM choices, or held-out reports.

Copy the control and change only its retained IDs. The exact response schema is:

```json
{"manifest_sha256": "<digest from model-input.json>", "retained_edit_ids": [0, 2]}
```

Unknown, duplicate, boolean, and stale IDs are invalid. An empty list is structurally valid.
Response order is normalized to original execution order. Manifest hashes identify immutable
inputs; they are integrity checks, not signatures authenticating an untrusted manifest.

## Reconstruct and evaluate

```bash
python -m patch_minimizer.frontends.swebench.experiments.cleanup reconstruct \
  --manifest results/cleanup-pilot/prepared/manifest.json \
  --selection results/cleanup-pilot/prepared/keep-all.json \
  --repo /path/to/django-clone --base-commit ORIGINAL_BASE_COMMIT \
  --output results/cleanup-pilot/reconstructed
```

`reconstruct` works offline and writes `candidate.patch`. Use the same arguments with
`evaluate` and a separate output directory for proxy checks and residual TRIM minimization.
`--timeout` sets the per-command budget (default 300 seconds).

Evaluation first verifies Docker and the existing local
`sweb.eval.x86_64.<instance_id>:latest` image. It never downloads datasets, pulls images,
builds images, or makes model requests. Containers use the existing frontend's runtime
dependency installation, which can require network access within the container.

Commands are validated independently on the original submission; accepted commands and
proxy files are then frozen. The original submission and keep-all replay must agree in
resulting Git tree and pass the frozen checks. Full patches, including build files, are
evaluated. The unmodified base must fail the proxy checks; otherwise no useful reward is
reported. The selected candidate and strictly replayed TRIM result must also pass.

If proxy files overlap selectable/submitted paths, evaluation fails closed: the parser can
mistake source fragments for proxy files, so blindly reinjecting those files is unsafe.
Arbitrary discovery commands can still discover different tests when original test edits
are selected. Audit such commands/task cohorts before using results for training. Passing
proxy checks does not establish full functional correctness or global patch minimality.

## Records, resume, and held-out evaluation

- `result.json` contains stage values/timings, frozen checks, proxy logs, selections, TRIM
  feedback stats, edit counts, changed text-line counts, and proxy test-execution counts.
- `manifest.json`, `selection.json`, `original.patch`, `candidate.patch`, and `trim.patch`
  preserve inputs and reconstructed outputs. Counts exclude binary payload size.
- Resume requires an exact identity: manifest, selected IDs, base commit, source revision
  and Python-source digest, installed packages, image ID, timeout, and optional dataset row.
  Changed inputs require a new output directory. A file lock prevents concurrent writers.
- Completed stages resume; interrupted/error stages retry. Preflight/validation failures
  write `attempt.json` and never overwrite an existing `result.json`.
- Statuses are `completed`, `invalid_selection`, `invalid_candidate`, `original_failed`,
  `replay_mismatch`, `infrastructure_error`, or `incomplete`. CLI failures exit nonzero.
  `completed` means the evaluation finished and proxy gates passed, not held-out success.
- Residual edit fraction is `(selected_edits - trim_edits) / selected_edits`; it is null
  when the denominator is zero. Residual metrics are reported only after proxy gates pass.
- Held-out evaluation is opt-in with `evaluate --held-out-row PATH`, where PATH contains
  one complete local SWE-bench dataset-row JSON for the same instance. Harness reports are
  separate `held_out_*` stages and never guide selection/minimization. Inspect their
  `resolved` and test outcomes separately; proxy metrics are not a correctness guarantee.
  Logs live under `logs/run_evaluation/cleanup_<identity>/`. Required images must exist.

This is evaluation infrastructure, not a trainer. No SFT/GRPO optimizer, model provider,
automatic trajectory generation, dataset split policy, or learned reward is implemented.
