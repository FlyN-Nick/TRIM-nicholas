"""File-based cleanup experiments: prepare, reconstruct, evaluate, and resume."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import importlib.metadata
import json
import logging
import platform
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

from docker.errors import DockerException

from patch_minimizer.core.rw_lock import RWLock
from patch_minimizer.frontends.swebench.cli.common import write_json_atomic
from patch_minimizer.frontends.swebench.experiments.selection import (
    ReconstructionError,
    SelectionError,
    apply_edits,
    disposable_clone,
    git,
    manifest_hash,
    patch_list_from_manifest,
    patch_tree,
    prepare_trajectory,
    reconstruct_selection,
    safe_path,
    select_edits,
    validate_selection,
)

LOGGER = logging.getLogger(__name__)


class InfrastructureError(RuntimeError):
    """Evaluation cannot reliably run with the available infrastructure."""


class ExperimentStop(RuntimeError):
    """An evaluated candidate reaches a recorded terminal outcome."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def _source_identity() -> dict:
    package = Path(__file__).resolve().parents[3]
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(str(path.relative_to(package)).encode())
        digest.update(path.read_bytes())
    return {
        "source_sha256": digest.hexdigest(), "python": platform.python_version(),
        "revision": git(package, "rev-parse", "HEAD").strip(),
        "packages": dict(sorted(
            (d.metadata["Name"], d.version) for d in importlib.metadata.distributions()
            if d.metadata["Name"]
        )),
    }


class EvaluationServices:
    """Real Docker-backed services; replace as a unit for offline orchestration checks."""

    def __init__(self, instance_id: str, timeout: int):
        self.instance_id = instance_id
        self.timeout = timeout

    def preflight(self, instance_id: str) -> dict:
        """Inspect Docker and an existing local image; never pull or build."""
        import docker

        try:
            with docker.from_env() as client:
                client.ping()
                image = client.images.get(f"sweb.eval.x86_64.{instance_id}:latest")
                return {"image_id": image.id, "docker": client.version()["Version"]}
        except docker.errors.DockerException as exc:
            raise InfrastructureError(
                f"Docker access and an existing sweb.eval.x86_64.{instance_id}:latest "
                f"image are required; no image was pulled or built: {exc}"
            ) from exc

    def _environment(self, commands: list, test_files: dict):
        from patch_minimizer.frontends.swebench.environment import SWEBenchEnvironment
        from patch_minimizer.strategies.environments.feedback_types import PatchFeedback

        timeout = self.timeout

        class CompletePatchEnvironment(SWEBenchEnvironment):
            # AIDEV-NOTE: Include build-file edits that the usual environment strips.
            def _apply_patch(self, container, patch: str) -> bool:
                self._copy_patch_to_container(container, patch)
                command = "git checkout -- . && git clean -fd"
                if patch.strip():
                    command += " && git apply /tmp/candidate.patch"
                code, _ = self._exec(
                    container, command,
                )
                return code == 0

            def _exec(self, container, cmd, timeout=None):
                code, output = super()._exec(container, cmd, timeout)
                if code in (124, 137):
                    raise InfrastructureError(
                        f"Container command timed out or was killed: {cmd}"
                    )
                if "pip install" in cmd and code != 0:
                    raise InfrastructureError(f"Container dependency setup failed: {output}")
                return code, output

            def get_feedback(self, patch, edits, edits_being_dropped=None):
                self.test_executions = getattr(self, "test_executions", 0)
                # AIDEV-NOTE: A newline forces the same clean/reset path for an empty patch.
                result = super().get_feedback(patch or "\n", edits, edits_being_dropped)
                if self.last_feedback:
                    self.test_executions += len(self.last_feedback.results)
                if result.status == PatchFeedback.BUILD_FAILED:
                    raise ReconstructionError("Patch failed to apply inside the instance image")
                return result

        # Validate injected paths before the underlying tar helper handles them.
        for name in test_files:
            safe_path(Path("/testbed"), name)
        return CompletePatchEnvironment(
            instance_id=self.instance_id, feedback_commands=copy.deepcopy(commands),
            test_files=copy.deepcopy(test_files), logger=LOGGER, timeout=timeout,
        )

    def freeze(self, manifest: dict) -> dict:
        """Validate commands independently on the original, then freeze that configuration."""
        from patch_minimizer.strategies.environments.feedback_types import PatchFeedback

        accepted, rejected = [], []
        executions = 0
        from patch_minimizer.frontends.swebench.utils.diff_utils import patch_file_paths

        edited = {str(PurePosixPath(e["filename"]))
                  for node in manifest["nodes"] for e in node}
        edited.update(str(PurePosixPath(p)) for p in patch_file_paths(manifest["submission"]))
        overlap = edited.intersection(str(PurePosixPath(p)) for p in manifest["test_files"])
        if overlap:
            raise InfrastructureError(
                f"Cannot freeze proxy files overlapping selectable edits: {sorted(overlap)}"
            )
        # AIDEV-NOTE: Validate on fresh original states; candidate failures never prune checks.
        for entry in manifest["feedback_commands"]:
            env = self._environment([entry], manifest["test_files"])
            try:
                outcome = env.get_feedback(manifest["submission"], [])
            finally:
                env._client.close()
            executions += env.test_executions
            (accepted if outcome.status == PatchFeedback.NO_CRASH else rejected).append(entry)
        if not accepted:
            raise InfrastructureError("No proxy commands passed on the original submission")
        return {
            "commands": accepted, "rejected": rejected,
            "test_files": manifest["test_files"], "test_executions": executions,
        }

    def proxy(self, patch: str, edits: list, frozen: dict) -> dict:
        """Evaluate the full patch with the frozen proxy tests."""
        from patch_minimizer.strategies.environments.feedback_types import PatchFeedback

        env = self._environment(frozen["commands"], frozen["test_files"])
        try:
            outcome = env.get_feedback(patch, edits)
        finally:
            env._client.close()
        return {
            "passed": outcome.status == PatchFeedback.NO_CRASH,
            "results": env.last_feedback.results, "test_executions": env.test_executions,
        }

    def minimize(self, repo: Path, base: str, selected: list, frozen: dict) -> dict:
        """Run existing TRIM algorithms with strict replay and a frozen environment."""
        from patch_minimizer.strategies.algorithms.edit.edit_minimizer import EditMinimizer
        from patch_minimizer.strategies.environments import FeedbackReceiver, GitDiffEnvironment
        from patch_minimizer.strategies.minimize_core import minimize_edit_list
        from patch_minimizer.strategies.repo_patch_manager import RepoPatchManager

        if not selected:
            return {"retained_edit_ids": [], "feedback_stats": {}, "test_executions": 0}
        from patch_minimizer.strategies.environments.feedback_types import PatchFeedback

        env = self._environment(frozen["commands"], frozen["test_files"])
        try:
            if env.get_feedback("", []).status == PatchFeedback.NO_CRASH:
                return {"retained_edit_ids": [], "feedback_stats": {},
                        "test_executions": env.test_executions}

            class StrictRepo(RepoPatchManager):
                # AIDEV-NOTE: Strict replay also applies to best-effort minimizer edits.
                def apply_single_patch(self, patch, use_lock=False, created_files=None):
                    if use_lock:
                        with self.code_dir_lock.w_locked_nowait():
                            apply_edits(Path(self.code_dir), patch, self.code_dir_lock)
                    else:
                        apply_edits(Path(self.code_dir), patch, self.code_dir_lock)

                def check_patch_range(
                    self, parent_diff, patch_range, leave_patches=False, use_lock=True,
                ):
                    if use_lock:
                        with self.code_dir_lock.w_locked_nowait():
                            return self.check_patch_range(
                                parent_diff, patch_range, leave_patches, False,
                            )
                    self.clean_repo()
                    try:
                        if parent_diff:
                            raise ReconstructionError(
                                "Parent snapshots are not selectable edits"
                            )
                        for patch in patch_range:
                            if patch:
                                self.apply_single_patch(patch)
                        return True
                    except ReconstructionError:
                        return False
                    finally:
                        if not leave_patches:
                            self.clean_repo()

            with disposable_clone(repo, base) as clone:
                lock = RWLock()
                minimizer = EditMinimizer(str(clone), lock, base, LOGGER, swebench_mode=True)
                strict = StrictRepo(str(clone), lock, base, LOGGER, swebench_mode=False)
                minimizer.repo = strict
                minimizer.patch_tester.repo = strict
                result = minimize_edit_list(
                    self.instance_id, copy.deepcopy(selected), minimizer,
                    FeedbackReceiver(env, git_diff_env=GitDiffEnvironment()),
                )
            def flatten(value):
                if hasattr(value, "global_edit_idx"):
                    return [value.global_edit_idx]
                return [i for item in value for i in flatten(item)]
            return {
                "retained_edit_ids": flatten(result["short_path"]),
                "feedback_stats": result["feedback_stats"],
                "test_executions": getattr(env, "test_executions", 0),
            }
        finally:
            client = getattr(env, "_client", None)
            if client is not None:
                client.close()

    def held_out(self, patch: str, row: dict, run_id: str, label: str) -> dict:
        """Run the harness on an explicitly supplied dataset row, without downloading data."""
        import docker
        from swebench.harness.constants import LOG_REPORT, RUN_EVALUATION_LOG_DIR
        from swebench.harness.run_evaluation import run_instance
        from swebench.harness.test_spec.test_spec import make_test_spec

        if row.get("instance_id") != self.instance_id:
            raise SelectionError("Held-out row instance ID does not match manifest")
        try:
            spec = make_test_spec(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise SelectionError(f"Invalid held-out dataset row: {exc}") from exc
        with docker.from_env() as client:
            client.images.get(spec.instance_image_key)
            run_instance(
                test_spec=spec,
                pred={"instance_id": self.instance_id, "model_patch": patch,
                      "model_name_or_path": label},
                rm_image=False, force_rebuild=False, client=client,
                run_id=run_id, timeout=self.timeout,
            )
        report = RUN_EVALUATION_LOG_DIR / run_id / label / self.instance_id / LOG_REPORT
        if not report.is_file():
            raise InfrastructureError(f"Held-out harness produced no report: {report}")
        return json.loads(report.read_text())[self.instance_id]


def _run_experiment(
    manifest: dict, selection: dict, repo: Path, base: str, output: Path, *,
    services: Any = None, timeout: int = 300, held_out_row: dict | None = None,
) -> dict:
    """Evaluate or resume an exact-input experiment, recording each completed stage."""
    repo, output = repo.resolve(), output.resolve()
    if output.is_relative_to(repo):
        raise SelectionError("Output must be outside the supplied repository")
    if timeout <= 0:
        raise SelectionError("Timeout must be positive")
    output.mkdir(parents=True, exist_ok=True)
    record = {"schema_version": 1, "status": "incomplete", "stages": {}}
    path = output / "result.json"
    identity_confirmed = False

    def save():
        # Preserve existing successful records if preflight fails or inputs are malformed.
        destination = path if identity_confirmed else output / "attempt.json"
        record["record_path"] = str(destination)
        write_json_atomic(destination, record)

    def stage(name, fn):
        prior = record["stages"].get(name, {})
        if prior.get("state") == "completed":
            return prior["value"]
        record["stages"][name] = {"state": "running"}
        save()
        started = time.monotonic()
        value = fn()
        record["stages"][name] = {
            "state": "completed", "value": value, "seconds": time.monotonic() - started,
        }
        save()
        return value

    try:
        patch_list = patch_list_from_manifest(manifest)
        ids = validate_selection(manifest, selection)
        selected = select_edits(patch_list, ids)
        services = services or EvaluationServices(manifest["instance_id"], timeout)
        preflight = services.preflight(manifest["instance_id"])
        identity = {
            "manifest": manifest_hash(manifest), "retained_edit_ids": ids,
            "base_commit": git(repo, "rev-parse", "--verify", "--end-of-options",
                               f"{base}^{{commit}}").strip(),
            "runtime": preflight, "timeout": timeout, "held_out_row": held_out_row,
            "software": _source_identity(),
        }
        # AIDEV-NOTE: Full input/image/software identity prevents cross-patch report reuse.
        run_id = "cleanup_" + manifest_hash(identity)
        if path.exists():
            previous = json.loads(path.read_text())
            if previous.get("run_id") != run_id:
                raise SelectionError(
                    "Output belongs to different inputs; choose a new output directory"
                )
            record = previous
        identity_confirmed = True
        record.update(run_id=run_id, identity=identity, status="incomplete")
        record.pop("error", None)
        save()
        write_json_atomic(output / "selection.json", {
            "manifest_sha256": manifest_hash(manifest), "retained_edit_ids": ids,
        })
        write_json_atomic(output / "manifest.json", manifest)
        base = identity["base_commit"]
        frozen = stage("freeze", lambda: services.freeze(manifest))
        if not frozen["commands"]:
            raise InfrastructureError("Frozen proxy feedback is empty")
        original = stage("original_proxy", lambda: services.proxy(
            manifest["submission"], [e for _, es in patch_list for e in es], frozen,
        ))
        if not original["passed"]:
            raise ExperimentStop(
                "original_failed", "Original submission failed frozen proxy checks"
            )
        try:
            replay = stage("replay", lambda: reconstruct_selection(repo, base, patch_list))
            equal = stage("replay_comparison", lambda: (
                patch_tree(repo, base, replay) == patch_tree(repo, base, manifest["submission"])
            ))
        except (ReconstructionError, subprocess.CalledProcessError) as exc:
            raise ExperimentStop("replay_mismatch", str(exc)) from exc
        if not equal:
            raise ExperimentStop(
                "replay_mismatch", "Keep-all replay differs from original submission"
            )
        (output / "original.patch").write_text(manifest["submission"])
        replay_proxy = stage("replay_proxy", lambda: services.proxy(replay, [], frozen))
        if not replay_proxy["passed"]:
            raise ExperimentStop(
                "replay_mismatch", "Keep-all replay failed frozen proxy checks"
            )
        empty_proxy = stage("empty_proxy", lambda: services.proxy("", [], frozen))
        if empty_proxy["passed"]:
            raise InfrastructureError(
                "Proxy checks pass on the unmodified base; no bug-sensitive reward is available"
            )
        candidate = stage("candidate", lambda: reconstruct_selection(repo, base, selected))
        (output / "candidate.patch").write_text(candidate)
        candidate_proxy = stage("candidate_proxy", lambda: services.proxy(
            candidate, [e for _, es in selected for e in es], frozen,
        ))
        if not candidate_proxy["passed"]:
            raise ExperimentStop("invalid_candidate", "Selection failed frozen proxy checks")
        minimized = stage("minimize", lambda: services.minimize(repo, base, selected, frozen))
        try:
            final_ids = validate_selection(manifest, {
                "manifest_sha256": manifest_hash(manifest),
                "retained_edit_ids": minimized["retained_edit_ids"],
            })
        except SelectionError as exc:
            raise InfrastructureError(f"TRIM returned invalid edit IDs: {exc}") from exc
        if not set(final_ids).issubset(ids):
            raise InfrastructureError("TRIM returned edits outside the selected sequence")
        final = stage("final_replay", lambda: reconstruct_selection(
            repo, base, select_edits(patch_list, final_ids),
        ))
        (output / "trim.patch").write_text(final)
        final_proxy = stage("final_proxy", lambda: services.proxy(final, [], frozen))
        if not final_proxy["passed"]:
            raise InfrastructureError("Strictly replayed TRIM result failed frozen checks")
        def changed_lines(patch):
            count, in_hunk = 0, False
            for line in patch.splitlines():
                if line.startswith("diff --git "):
                    in_hunk = False
                elif line.startswith("@@"):
                    in_hunk = True
                elif in_hunk and line.startswith(("+", "-")):
                    count += 1
            return count
        record["metrics"] = {
            "original_edits": sum(len(es) for _, es in patch_list),
            "selected_edits": len(ids), "trim_edits": len(final_ids),
            "residual_edits": len(ids) - len(final_ids),
            "residual_edit_fraction": (len(ids) - len(final_ids)) / len(ids) if ids else None,
            "changed_lines": {
                "original": changed_lines(manifest["submission"]),
                "candidate": changed_lines(candidate), "trim": changed_lines(final),
            },
            "test_executions": sum(s["value"].get("test_executions", 0)
                                   for s in record["stages"].values()
                                   if isinstance(s.get("value"), dict)),
        }
        if held_out_row is not None:
            for label, patch in (("original", manifest["submission"]),
                                 ("candidate", candidate), ("trim", final)):
                stage(f"held_out_{label}", lambda p=patch, name=label: services.held_out(
                    p, held_out_row, run_id, name,
                ))
        record["status"] = "completed"
    except ExperimentStop as exc:
        record.update(status=exc.status, error=str(exc))
    except ReconstructionError as exc:
        running = {name for name, value in record["stages"].items()
                   if value.get("state") == "running"}
        status = "invalid_candidate"
        if running.intersection({"freeze", "original_proxy"}):
            status = "original_failed"
        elif running.intersection({"minimize", "final_replay", "final_proxy"}):
            status = "infrastructure_error"
        record.update(status=status, error=str(exc))
    except SelectionError as exc:
        # A mismatched output directory must never overwrite the earlier experiment.
        if path.exists() and record.get("run_id") is None:
            raise
        record.update(status="invalid_selection", error=str(exc))
    except (OSError, RuntimeError, subprocess.CalledProcessError, DockerException) as exc:
        record.update(status="infrastructure_error", error=f"{type(exc).__name__}: {exc}")
    save()
    return record


def run_experiment(
    manifest: dict, selection: dict, repo: Path, base: str, output: Path, *,
    services: Any = None, timeout: int = 300, held_out_row: dict | None = None,
) -> dict:
    """Run with exclusive ownership of the output directory and its resume record."""
    if output.resolve().is_relative_to(repo.resolve()):
        raise SelectionError("Output must be outside the supplied repository")
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InfrastructureError("Another process owns this experiment output") from exc
        return _run_experiment(
            manifest, selection, repo, base, output, services=services,
            timeout=timeout, held_out_row=held_out_row,
        )


def main(argv: list[str] | None = None) -> int:
    """Run a file-based cleanup experiment without making model requests."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser(
        "prepare", help="Export original edits and a keep-all control offline"
    )
    prepare.add_argument("--traj", type=Path, required=True)
    prepare.add_argument("--bug-id", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--issue-file", type=Path,
                         help="Optional issue text when the trajectory does not include it")
    for command in ("reconstruct", "evaluate"):
        child = sub.add_parser(command, help=f"{command.capitalize()} a file-based selection")
        child.add_argument("--manifest", type=Path, required=True)
        child.add_argument("--selection", type=Path, required=True)
        child.add_argument("--repo", type=Path, required=True)
        child.add_argument("--base-commit", required=True)
        child.add_argument("--output", type=Path, required=True)
        if command == "evaluate":
            child.add_argument("--timeout", type=int, default=300)
            child.add_argument(
                "--held-out-row", type=Path,
                help="Opt-in held-out evaluation using a local SWE-bench row JSON",
            )
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = prepare_trajectory(args.traj, args.bug_id)
            if args.issue_file:
                manifest["issue"] = args.issue_file.read_text()
                manifest["manifest_sha256"] = manifest_hash(manifest)
            args.output.mkdir(parents=True, exist_ok=True)
            write_json_atomic(args.output / "manifest.json", manifest)
            model_nodes = [[{k: v for k, v in e.items()
                             if k not in ("expected_diff", "edit_diff")}
                            for e in node] for node in manifest["nodes"]]
            write_json_atomic(args.output / "model-input.json", {
                "schema_version": 1, "instance_id": args.bug_id, "issue": manifest["issue"],
                "manifest_sha256": manifest_hash(manifest), "nodes": model_nodes,
            })
            write_json_atomic(args.output / "keep-all.json", {
                "manifest_sha256": manifest_hash(manifest),
                "retained_edit_ids": [
                    e["global_edit_idx"] for n in manifest["nodes"] for e in n
                ],
            })
            print(f"Prepared {args.bug_id}: {args.output}")
            return 0
        manifest = json.loads(args.manifest.read_text())
        selection = json.loads(args.selection.read_text())
        if args.output.resolve().is_relative_to(args.repo.resolve()):
            raise SelectionError("Output must be outside the supplied repository")
        if args.command == "reconstruct":
            patch_list = patch_list_from_manifest(manifest)
            ids = validate_selection(manifest, selection)
            patch = reconstruct_selection(
                args.repo, args.base_commit, select_edits(patch_list, ids),
            )
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / "candidate.patch").write_text(patch)
            print(f"Reconstructed {len(ids)} edits: {args.output / 'candidate.patch'}")
            return 0
        row = json.loads(args.held_out_row.read_text()) if args.held_out_row else None
        result = run_experiment(
            manifest, selection, args.repo, args.base_commit, args.output,
            timeout=args.timeout, held_out_row=row,
        )
        print(json.dumps({"status": result["status"], "error": result.get("error"),
                          "result": result["record_path"]}))
        return 0 if result["status"] == "completed" else 1
    except (SelectionError, InfrastructureError, OSError,
            subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
