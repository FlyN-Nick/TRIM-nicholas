"""Immutable edit selection and strict replay in disposable repositories."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Iterator

from patch_minimizer.core.edit import Edit, EditType, MatchMode
from patch_minimizer.core.edit_applier import EditApplier
from patch_minimizer.core.rw_lock import RWLock


class SelectionError(ValueError):
    """A selection or its source manifest violates the experiment contract."""


class ReconstructionError(SelectionError):
    """Selected edits cannot be replayed faithfully on the original base."""


def manifest_hash(manifest: dict) -> str:
    """Hash canonical JSON, excluding its optional embedded digest."""
    if not isinstance(manifest, dict):
        raise SelectionError("Manifest must be a JSON object")
    payload = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def prepare_trajectory(traj: Path, instance_id: str) -> dict:
    """Prepare an evaluator manifest without Docker or dataset access."""
    from patch_minimizer.frontends.swebench.cli.traj_cli import minimize_traj

    data = json.loads(traj.read_text())
    prepared = minimize_traj(
        traj_path=traj, instance_id=instance_id, save_dir=Path("."),
        repo_dir="", only_parse=True, logger=logging.getLogger(__name__),
    )
    if prepared is None:
        raise SelectionError("Trajectory has no usable submitted edits")
    manifest = {
        "schema_version": 1, "instance_id": instance_id,
        "trajectory_sha256": hashlib.sha256(traj.read_bytes()).hexdigest(),
        "issue": data.get("info", {}).get("problem_statement", ""),
        "nodes": [[asdict(e) for e in edits] for _, edits in prepared["patch_list"]],
        "submission": data.get("info", {}).get("submission", ""),
        "test_files": prepared["test_files"],
        "feedback_commands": prepared["feedback_commands"],
    }
    manifest["manifest_sha256"] = manifest_hash(manifest)
    patch_list_from_manifest(manifest)
    return manifest


def patch_list_from_manifest(manifest: dict) -> list:
    """Load original edit semantics, checking schema, integrity, and unique IDs."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise SelectionError("Unsupported manifest schema")
    if manifest.get("manifest_sha256") != manifest_hash(manifest):
        raise SelectionError("Manifest digest mismatch")
    for key, kind in (("instance_id", str), ("submission", str),
                      ("feedback_commands", list), ("test_files", dict), ("nodes", list)):
        if not isinstance(manifest.get(key), kind):
            raise SelectionError(f"Manifest {key} must be {kind.__name__}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", manifest["instance_id"]):
        raise SelectionError(
            "Instance ID must contain only lowercase letters, digits, _, -, or ."
        )
    if any(not isinstance(c, dict) or not isinstance(c.get("command"), str)
           or not c["command"].strip() for c in manifest["feedback_commands"]):
        raise SelectionError("Proxy commands must be nonempty command objects")
    if any(not isinstance(k, str) or not isinstance(v, str)
           for k, v in manifest["test_files"].items()):
        raise SelectionError("Proxy test files must map paths to text")
    try:
        nodes = []
        ids = []
        for node in manifest["nodes"]:
            if not isinstance(node, list):
                raise SelectionError("Each manifest node must be an edit list")
            edits = []
            for raw in node:
                values = dict(raw)
                values["edit_type"] = EditType(values["edit_type"])
                values["match_mode"] = MatchMode(values["match_mode"])
                edit = Edit(**values)
                if any(not isinstance(getattr(edit, k), str)
                       for k in ("filename", "before", "after", "explanation")):
                    raise SelectionError(
                        "Edit paths, contents, and explanations must be strings"
                    )
                if type(edit.global_edit_idx) is not int or edit.global_edit_idx < 0:
                    raise SelectionError("Every original edit needs a nonnegative integer ID")
                ids.append(edit.global_edit_idx)
                edits.append(edit)
            if edits:
                nodes.append((None, edits))
        if not ids or len(ids) != len(set(ids)):
            raise SelectionError("Manifest must contain edits with unique IDs")
        return nodes
    except (KeyError, TypeError, ValueError) as exc:
        raise SelectionError(f"Invalid manifest: {exc}") from exc


def validate_selection(manifest: dict, selection: dict) -> list[int]:
    """Validate a model response and return IDs in original execution order."""
    if not isinstance(selection, dict) or set(selection) != {
        "manifest_sha256", "retained_edit_ids",
    }:
        raise SelectionError(
            "Selection must contain only manifest_sha256 and retained_edit_ids"
        )
    if selection["manifest_sha256"] != manifest_hash(manifest):
        raise SelectionError("Selection refers to a different manifest")
    ids = selection["retained_edit_ids"]
    if not isinstance(ids, list) or any(type(i) is not int for i in ids):
        raise SelectionError("retained_edit_ids must be a list of integers (not booleans)")
    original = [e["global_edit_idx"] for node in manifest["nodes"] for e in node]
    if len(ids) != len(set(ids)) or not set(ids).issubset(original):
        raise SelectionError("Duplicate or unknown retained edit IDs")
    return [i for i in original if i in set(ids)]


def select_edits(patch_list: list, ids: list[int]) -> list:
    """Copy selected edits in original order without reintroducing parent snapshots."""
    # AIDEV-NOTE: Never apply parent snapshots: they could restore dropped edits.
    selected = []
    for _, edits in patch_list:
        kept = [copy.deepcopy(e) for e in edits if e.global_edit_idx in ids]
        if kept:
            selected.append((None, kept))
    return selected


def git(repo: Path, *args: str, input_text: str | None = None) -> str:
    """Run Git without shell interpolation or user hooks."""
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        input=input_text, text=True, capture_output=True, check=True,
    ).stdout


@contextmanager
def disposable_clone(repo: Path, base_commit: str) -> Iterator[Path]:
    """Clone committed objects only; leave the supplied repository untouched."""
    commit = git(repo, "rev-parse", "--verify", "--end-of-options",
                 f"{base_commit}^{{commit}}").strip()
    with tempfile.TemporaryDirectory(prefix="trim-cleanup-") as temp:
        target = Path(temp) / "repo"
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "clone", "--no-hardlinks",
             "--no-checkout", "--", str(repo.resolve()), str(target)],
            capture_output=True, check=True,
        )
        git(target, "checkout", "--detach", commit)
        yield target


def safe_path(root: Path, name: str) -> Path:
    """Reject traversal, Git metadata access, and symlink-based edit redirection."""
    path = PurePosixPath(name)
    if not name or "\x00" in name or path.is_absolute() or any(
        p in ("..", ".git") for p in path.parts
    ):
        raise ReconstructionError(f"Unsafe edit path: {name!r}")
    target = root / name
    if target == root or not target.resolve().is_relative_to(root.resolve()):
        raise ReconstructionError(f"Edit path escapes repository: {name!r}")
    if any(p.is_symlink() for p in [target, *target.parents] if p.is_relative_to(root)):
        raise ReconstructionError(f"Symlink edit paths are unsupported: {name!r}")
    return target


def apply_edits(root: Path, edits: list[Edit], lock: RWLock) -> None:
    """Apply every operation strictly; never silently skip best-effort edits."""
    applier = EditApplier()
    # AIDEV-NOTE: The normal SWE-bench patch manager permits skips; experiment replay does not.
    for original in edits:
        edit = copy.deepcopy(original)
        target = safe_path(root, edit.filename)
        edit.filename = target.relative_to(root).as_posix()
        if edit.edit_type == EditType.COPY_FILE:
            source = safe_path(root, edit.before)
            edit.before = source.relative_to(root).as_posix()
            if not source.is_file():
                raise ReconstructionError(f"Missing COPY_FILE source: {edit.before}")
        elif edit.edit_type != EditType.WHOLE_FILE_ACTIONS and not target.is_file():
            raise ReconstructionError(f"Missing target for edit {edit.global_edit_idx}")
        try:
            if edit.edit_type == EditType.COPY_FILE:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                result = {"file_path": str(target)}
            else:
                result = applier.apply_edit_to_linux_code(edit, str(target), lock)
        except (OSError, ValueError, NotImplementedError) as exc:
            raise ReconstructionError(f"Edit {edit.global_edit_idx}: {exc}") from exc
        if result is None:
            raise ReconstructionError(f"Edit {edit.global_edit_idx} does not apply")
        if target.exists():
            git(root, "add", "--intent-to-add", "--force", "--", edit.filename)


def reconstruct_selection(repo: Path, base_commit: str, patch_list: list) -> str:
    """Return a binary-capable Git diff for a strict ordered replay."""
    with disposable_clone(repo, base_commit) as clone:
        lock = RWLock()
        with lock.w_locked_nowait():
            apply_edits(clone, [e for _, es in patch_list for e in es], lock)
        return git(clone, "diff", "--binary", "HEAD", "--")


def patch_tree(repo: Path, base_commit: str, patch: str) -> str:
    """Return the complete resulting Git tree for semantic replay comparison."""
    with disposable_clone(repo, base_commit) as clone:
        if patch.strip():
            git(clone, "apply", "--index", "--binary", "-", input_text=patch)
        return git(clone, "write-tree").strip()
