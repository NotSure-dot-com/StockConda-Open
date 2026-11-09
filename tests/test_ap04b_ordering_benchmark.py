"""Decision and evidence contracts for the frozen AP-04B Gate-3 run."""

from __future__ import annotations

from importlib import util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import chess


ROOT = Path(__file__).parents[1]


def load_module(relative: str, name: str) -> ModuleType:
    path = ROOT / relative
    if not path.exists():
        pytest.fail(f"missing Gate-3 component: {relative}")
    spec = util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runner() -> ModuleType:
    return load_module("scripts/ap04b_ordering_benchmark.py", "ap04b_benchmark")


def auditor() -> ModuleType:
    return load_module("scripts/ap04b_gate3_audit.py", "ap04b_audit")


def rows(
    *,
    candidate_time: int = 95,
    baseline_time: int = 100,
) -> list[dict[str, Any]]:
    return [
        {
            "block": block,
            "fixture": fixture,
            "engine": engine,
            "repetition": repetition,
            "elapsed_ns": (
                baseline_time if engine == "baseline" else candidate_time
            ),
        }
        for block in range(10)
        for fixture in range(12)
        for engine in ("baseline", "candidate")
        for repetition in range(5)
    ]


@pytest.mark.parametrize(
    ("aggregate", "fixtures", "valid", "expected"),
    [
        ([0.90] * 10, [[0.90] * 10] * 12, False, "FAIL"),
        ([0.80, 1.20] * 5, [[0.90] * 10] * 12, True, "HOLD"),
        ([0.90] * 10, [[0.80, 1.20] * 5] * 12, True, "HOLD"),
        ([1.001] * 10, [[0.95] * 10] * 12, True, "FAIL"),
        ([0.90] * 10, [[1.031] * 10] + [[0.90] * 10] * 11, True, "FAIL"),
        ([0.971] * 10, [[0.98] * 10] * 12, True, "HOLD"),
        ([1.00] * 10, [[1.00] * 10] * 12, True, "HOLD"),
        ([0.97] * 10, [[1.03] * 10] + [[0.90] * 10] * 11, True, "PASS"),
        ([0.95] * 9, [[0.95] * 10] * 12, True, "FAIL"),
        ([0.95] * 10, [[0.95] * 10] * 11, True, "FAIL"),
        ([float("nan")] * 10, [[0.95] * 10] * 12, True, "FAIL"),
        ([0.95] * 10, [[0.0] * 10] * 12, True, "FAIL"),
    ],
)
def test_decision_precedence_is_frozen(
    aggregate: list[float],
    fixtures: list[list[float]],
    valid: bool,
    expected: str,
) -> None:
    """Catches applying regression thresholds before technical/MAD gates."""

    assert runner().decision(aggregate, fixtures, valid=valid)["status"] == expected


def test_ratios_require_exact_grid_and_weight_long_fixtures() -> None:
    """Catches accepting partial samples or averaging fixture ratios equally."""

    sample = rows()
    aggregate, fixture_ratios = runner().ratios(sample)
    assert aggregate == pytest.approx([0.95] * 10)
    assert all(series == pytest.approx([0.95] * 10) for series in fixture_ratios)

    weighted = rows(candidate_time=100, baseline_time=100)
    for row in weighted:
        if row["fixture"] == 0:
            row["elapsed_ns"] = 500 if row["engine"] == "candidate" else 1000
    aggregate, fixture_ratios = runner().ratios(weighted)
    assert aggregate == pytest.approx([16 / 21] * 10)
    assert fixture_ratios[0] == pytest.approx([0.5] * 10)

    for invalid in (
        sample[:-1],
        sample + [dict(sample[0])],
        [*sample[:-1], {**sample[-1], "elapsed_ns": 0}],
        [*sample[:-1], {**sample[-1], "elapsed_ns": True}],
    ):
        with pytest.raises(ValueError, match="measurement grid"):
            runner().ratios(invalid)


def test_relative_mad_is_unscaled() -> None:
    """Catches reporting percentage points instead of a relative fraction."""

    assert runner().relative_mad([0.96, 1.04] * 5) == pytest.approx(0.04)


def test_existing_output_is_rejected_without_modification(tmp_path: Path) -> None:
    """Catches evidence overwrite or accidental resume."""

    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("retained", encoding="utf-8")
    with pytest.raises(FileExistsError):
        runner().new_output(output)
    assert sentinel.read_text(encoding="utf-8") == "retained"


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("missing-hashseed", None),
        ("wrong-hashseed", "random"),
        ("optimization", "1"),
    ],
)
def test_freeze_cli_rejects_invalid_parent_runtime_before_output(
    tmp_path: Path,
    mutation: str,
    value: str | None,
) -> None:
    """Catches freeze persisting a manifest from an inadmissible parent."""

    module = runner()
    environment = module.RUNTIME_POLICY.runtime_environment(
        ROOT,
        {"PATH": str(Path(sys.executable).parent)},
    )
    if mutation == "missing-hashseed":
        environment.pop("PYTHONHASHSEED")
    elif mutation == "wrong-hashseed":
        assert value is not None
        environment["PYTHONHASHSEED"] = value
    else:
        assert value is not None
        environment["PYTHONOPTIMIZE"] = value
    output = tmp_path / "freeze-output"
    missing = tmp_path / "missing"
    command = module.RUNTIME_POLICY.controlled_python_command(
        ROOT / "scripts" / "ap04b_ordering_benchmark.py",
        "freeze",
        "--fixtures",
        str(missing),
        "--baseline",
        str(tmp_path / "baseline"),
        "--baseline-archive",
        str(missing),
        "--candidate",
        str(tmp_path / "candidate"),
        "--candidate-archive",
        str(missing),
        "--gate2-manifest",
        str(missing),
        "--gate2-result",
        str(missing),
        "--gate2-counter",
        str(missing),
        "--output",
        str(output),
    )

    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "runtime controls are not enforced" in completed.stderr
    assert not output.exists()


def test_freeze_allows_controlled_parent_runtime_to_reach_output_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a freeze runtime gate that rejects the admitted setup."""

    module = runner()
    output = tmp_path / "freeze-output"

    class ReachedOutputBoundary(Exception):
        pass

    def reached(path: Path) -> None:
        assert path == output
        raise ReachedOutputBoundary

    monkeypatch.setattr(module, "new_output", reached)

    with pytest.raises(ReachedOutputBoundary):
        module.freeze(SimpleNamespace(output=output))


@pytest.mark.parametrize(
    ("load1", "conflicts", "expected"),
    [
        (5.5, [], "PASS"),
        (5.5001, [], "HOLD"),
        (0.1, [{"pid": 17, "args": "fastchess"}], "HOLD"),
    ],
)
def test_host_gate_never_silently_ignores_load_or_conflicts(
    load1: float,
    conflicts: list[dict[str, Any]],
    expected: str,
) -> None:
    """Catches starting a block under a forbidden host condition."""

    assert runner().host_gate(load1, conflicts)["status"] == expected


def test_process_snapshot_rejects_loader_injection_before_child_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = runner()
    monkeypatch.setenv("LD_AUDIT", "/tmp/injected.so")
    monkeypatch.setattr(
        module.subprocess,
        "check_output",
        lambda *_args, **_kwargs: pytest.fail(
            "ps started before loader-environment validation"
        ),
    )

    with pytest.raises(RuntimeError, match="dynamic loader"):
        module.process_snapshot()


@pytest.mark.parametrize(
    "name",
    ["LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH"],
)
def test_independent_auditor_rejects_loader_environment_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """Catches the auditor writing evidence after loader influence is present."""

    module = auditor()
    output = tmp_path / "audit-output"
    monkeypatch.setenv(name, "/tmp/harmless-loader-fixture.so")

    with pytest.raises(RuntimeError, match="dynamic loader"):
        module.execute_audit(SimpleNamespace(output=output))

    assert not output.exists()


@pytest.mark.parametrize("state", ["missing", "empty"])
def test_independent_auditor_allows_inactive_loader_environment(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Catches treating an absent or explicitly empty loader value as active."""

    module = auditor()
    for name in ("LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH"):
        if state == "missing":
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, "")

    module.validate_auditor_environment()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PYTHONTRACEMALLOC", "1"),
        ("PYTHONMALLOC", "debug"),
        ("PYTHONPLATLIBDIR", "unexpected-lib"),
        ("PYTHONDEBUG", "1"),
        ("PYTHONWARNDEFAULTENCODING", "1"),
    ],
)
def test_independent_auditor_rejects_python_debug_environment_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    """Catches timing/debug startup options before audit evidence is created."""

    module = auditor()
    output = tmp_path / "audit-output"
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="Python startup environment"):
        module.execute_audit(SimpleNamespace(output=output))

    assert not output.exists()


@pytest.mark.parametrize("mutation", ["path", "version"])
def test_independent_auditor_rejects_wrong_interpreter_before_output(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Catches audit replay under a different interpreter identity."""

    module = auditor()
    frozen = json.loads(json.dumps(module.RUNTIME_POLICY.runtime_identity()))
    if mutation == "path":
        frozen["executable"] = "/wrong/python"
    else:
        frozen["python"] = "wrong-version"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"auditor_runtime": frozen}), encoding="utf-8")
    output = tmp_path / "audit-output"

    with pytest.raises(RuntimeError, match="frozen auditor interpreter"):
        module.execute_audit(SimpleNamespace(output=output, manifest=manifest))

    assert not output.exists()


def test_independent_auditor_allows_frozen_current_interpreter() -> None:
    """Catches an admission rule that rejects the intended interpreter."""

    module = auditor()
    assert hasattr(module, "validate_auditor_runtime"), (
        "auditor has no frozen-interpreter admission check"
    )
    module.validate_auditor_runtime(
        {"auditor_runtime": module.RUNTIME_POLICY.runtime_identity()}
    )


def test_process_snapshot_ignores_path_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = runner()
    marker = tmp_path / "wrapper-used"
    wrapper = tmp_path / "ps"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"printf used > {marker}\n"
        "printf 'PID PPID PSR COMMAND ARGS\\n'\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    snapshot = module.process_snapshot()

    assert not marker.exists()
    assert len(snapshot.splitlines()) > 1


def test_process_tool_identity_rejects_same_path_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = runner()
    assert hasattr(module, "process_tool_identity"), (
        "Gate 3 does not bind its process-listing tool"
    )
    tool = tmp_path / "ps"
    tool.write_bytes(b"ps-v1")
    monkeypatch.setattr(module, "PS_PATH", tool, raising=False)
    frozen = module.process_tool_identity()
    tool.write_bytes(b"ps-v2")

    with pytest.raises(RuntimeError, match="process tool identity"):
        module.verify_process_tool_identity(frozen)


@pytest.mark.parametrize(
    "command",
    [
        "python -m chess_engine.stockconda",
        "python3 -u -m chess_engine.stockconda",
        "python3 -O -X dev -m chess_engine.stockconda",
    ],
)
def test_module_started_engine_is_a_host_conflict_at_low_load(
    command: str,
) -> None:
    """Catches the launcher form used by the AP-04B engine scripts."""

    snapshot = (
        "PID PPID PSR COMMAND COMMAND\n"
        f"42 1 3 python3 {command}\n"
        "43 1 4 python3 python3 -u -m unrelated.module\n"
        "44 1 5 bash bash -c echo -m chess_engine.stockconda\n"
    )
    conflicts = runner().conflicting_processes(snapshot)

    assert [item["pid"] for item in conflicts] == [42]
    assert runner().host_gate(0.1, conflicts)["status"] == "HOLD"


def test_engine_order_alternates_by_complete_block() -> None:
    """Catches using one fixed A/B order for all measurements."""

    assert runner().engine_order(0) == ("baseline", "candidate")
    assert runner().engine_order(1) == ("candidate", "baseline")
    assert runner().engine_order(9) == ("candidate", "baseline")


def test_coverage_requires_selection_and_cutoff_for_every_category() -> None:
    """Catches timing a fixture set that does not exercise one stage."""

    complete = {
        category: {"selected": 1, "cutoffs": 1}
        for category in ("tt", "capture", "killer", "other")
    }
    runner().validate_coverage(complete)
    for field in ("selected", "cutoffs"):
        broken = {key: dict(value) for key, value in complete.items()}
        broken["killer"][field] = 0
        with pytest.raises(ValueError, match="category coverage"):
            runner().validate_coverage(broken)


def test_cutoff_coverage_requires_a_move_searched_by_the_node() -> None:
    """Catches counting an early TT return as a move-loop cutoff."""

    module = runner()
    board = chess.Board()
    tt_move = chess.Move.from_uci("e2e4")
    result = (100, tt_move)

    assert module.loop_cutoff_category(
        board, result, 50, tt_move, (None, None), set()
    ) is None
    assert module.loop_cutoff_category(
        board, result, 50, tt_move, (None, None), {tt_move.uci()}
    ) == "tt"


def test_auditor_recomputes_and_rejects_forged_runner_verdict() -> None:
    """Catches trusting result.json instead of the complete raw grid."""

    recomputed = auditor().audit_rows(rows())
    assert recomputed["status"] == "PASS"
    claimed = dict(recomputed, status="FAIL")
    with pytest.raises(ValueError, match="runner result mismatch"):
        auditor().compare_runner_result(recomputed, claimed)


def test_auditor_rejects_malformed_jsonl_line(tmp_path: Path) -> None:
    """Catches skipping a corrupt append-only raw record."""

    raw = tmp_path / "raw.jsonl"
    raw.write_text('{"block": 0}\nnot-json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="malformed raw JSONL"):
        auditor().read_rows(raw)


def test_archive_identity_and_extracted_sources_are_both_verified(
    tmp_path: Path,
) -> None:
    """Catches a right-name archive or build root with changed source bytes."""

    root = tmp_path / "build"
    source = root / "src" / "engine.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    archive = tmp_path / "engine.tar"
    with tarfile.open(archive, "w") as stream:
        stream.add(source, arcname="src/engine.py")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    assert runner().verify_archive(root, archive, digest) == {
        "src/engine.py": hashlib.sha256(source.read_bytes()).hexdigest()
    }
    with pytest.raises(RuntimeError, match="archive SHA-256"):
        runner().verify_archive(root, archive, "0" * 64)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="archive/source mismatch"):
        runner().verify_archive(root, archive, digest)

    source.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src" / "sitecustomize.py").write_text(
        "INJECTED = True\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="unexpected build-root source"):
        runner().verify_archive(root, archive, digest)


def gate2_handoff() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]
]:
    module = runner()
    positions = [
        {"label": f"fixture-{index}", "fen": f"fen-{index}"}
        for index in range(12)
    ]
    manifest = {
        "runner_sha256": "gate2-runner-sha",
        "runtime_policy_sha256": "gate2-policy-sha",
        "depths": [1, 2, 3, 4, 5],
        "positions": [
            *json.loads(json.dumps(positions)),
            *[
                {"label": f"extra-{index}", "fen": f"extra-fen-{index}"}
                for index in range(8)
            ],
        ],
        "engines": {
            "baseline": {"archive_sha256": module.BASELINE_ARCHIVE_SHA256},
            "candidate": {
                "archive_sha256": "c" * 64,
                "source": {
                    "scripts/ap04b_gate2.py": "gate2-runner-sha",
                    "scripts/ap04b_runtime_policy.py": "gate2-policy-sha",
                },
            },
        },
    }
    result = {
        "status": "PASS",
        "comparisons": 100,
        "manifest_sha256": "manifest-sha",
        "runner_sha256": "gate2-runner-sha",
    }
    counter = {
        "status": "PASS",
        "manifest_sha256": "manifest-sha",
        "runner_sha256": "gate2-runner-sha",
        "results": {
            "tt": {"captures": 0, "killers": 0, "other": 0},
            "capture": {"captures": 1, "killers": 0, "other": 0},
            "killer": {"captures": 1, "killers": 1, "other": 0},
        },
    }
    return manifest, result, counter, positions


def test_gate3_handoff_keeps_baseline_fixed_and_admits_gate2_candidate() -> None:
    module = runner()
    manifest, result, counter, positions = gate2_handoff()
    module.validate_gate2_handoff(
        manifest, result, counter, "manifest-sha", positions
    )

    changed = json.loads(json.dumps(manifest))
    changed["engines"]["baseline"]["archive_sha256"] = "f" * 64
    with pytest.raises(RuntimeError, match="Gate-2 baseline archive"):
        module.validate_gate2_handoff(
            changed, result, counter, "manifest-sha", positions
        )

    for invalid in (None, "short", "g" * 64):
        changed = json.loads(json.dumps(manifest))
        changed["engines"]["candidate"]["archive_sha256"] = invalid
        with pytest.raises(RuntimeError, match="Gate-2 candidate archive"):
            module.validate_gate2_handoff(
                changed, result, counter, "manifest-sha", positions
            )


@pytest.mark.parametrize(
    "depths",
    [None, [1, 1, 1, 1, 1], [1, 2, 3, 4], [1, 2, 3, 4, 6]],
)
def test_gate3_handoff_requires_exact_gate2_depths(
    depths: list[int] | None,
) -> None:
    module = runner()
    manifest, result, counter, positions = gate2_handoff()
    if depths is None:
        manifest.pop("depths")
    else:
        manifest["depths"] = depths

    with pytest.raises(RuntimeError, match="Gate-2 depths"):
        module.validate_gate2_handoff(
            manifest, result, counter, "manifest-sha", positions
        )


@pytest.mark.parametrize("mutation", ["changed", "reordered"])
def test_gate3_handoff_rejects_different_gate2_fixture_prefix(
    mutation: str,
) -> None:
    module = runner()
    manifest, result, counter, positions = gate2_handoff()
    if mutation == "changed":
        manifest["positions"][0]["fen"] = "different-fen"
    else:
        manifest["positions"][0], manifest["positions"][1] = (
            manifest["positions"][1],
            manifest["positions"][0],
        )

    with pytest.raises(RuntimeError, match="Gate-2 fixture prefix"):
        module.validate_gate2_handoff(
            manifest, result, counter, "manifest-sha", positions
        )


@pytest.mark.parametrize(
    "mutation",
    ["manifest-runner", "parity-runner", "counter-missing", "counter-fail",
     "counter-manifest", "counter-runner", "counter-scenarios"],
)
def test_gate3_handoff_requires_bound_parity_and_counter(
    mutation: str,
) -> None:
    module = runner()
    manifest, result, counter, positions = gate2_handoff()
    if mutation == "manifest-runner":
        manifest["runner_sha256"] = "wrong"
    elif mutation == "parity-runner":
        result["runner_sha256"] = "wrong"
    elif mutation == "counter-missing":
        counter = {}
    elif mutation == "counter-fail":
        counter["status"] = "FAIL"
    elif mutation == "counter-manifest":
        counter["manifest_sha256"] = "wrong"
    elif mutation == "counter-runner":
        counter["runner_sha256"] = "wrong"
    else:
        counter["results"].pop("killer")

    with pytest.raises(RuntimeError, match="Gate-2 .*runner|Gate-2 counter"):
        module.validate_gate2_handoff(
            manifest, result, counter, "manifest-sha", positions
        )


def test_build_roots_and_external_evidence_must_remain_distinct_and_frozen(
    tmp_path: Path,
) -> None:
    """Catches shared builds or changed Gate-2/fixture evidence."""

    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    runner().validate_distinct_roots(baseline, candidate)
    with pytest.raises(RuntimeError, match="independent"):
        runner().validate_distinct_roots(baseline, baseline)

    evidence = tmp_path / "gate2.json"
    evidence.write_text("{}\n", encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    runner().validate_evidence(evidence, digest)
    evidence.write_text('{"changed": true}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="evidence changed"):
        runner().validate_evidence(evidence, digest)


def test_preflight_semantics_require_exact_nested_equality() -> None:
    """Catches accepting equal bestmoves with different depth/score/info."""

    baseline = {"bestmove": "e2e4", "score": 17, "depth": 5, "info": [1, 2]}
    runner().compare_semantics(baseline, dict(baseline))
    with pytest.raises(RuntimeError, match="preflight semantic mismatch"):
        runner().compare_semantics(baseline, {**baseline, "score": 18})


def test_baseline_classifier_uses_node_entry_tt_capture_killer_other_order() -> None:
    """Catches coverage instrumentation assigning a move to a later bucket."""

    board = chess.Board()
    tt_move = chess.Move.from_uci("e2e4")
    killer = chess.Move.from_uci("d2d4")
    other = chess.Move.from_uci("g1f3")
    capture_board = chess.Board("7k/8/8/3p4/8/2N5/8/K7 w - - 0 1")
    capture = chess.Move.from_uci("c3d5")

    assert runner().classify_move(board, tt_move, tt_move, (killer, None)) == "tt"
    assert runner().classify_move(capture_board, capture, None, (capture, None)) == "capture"
    assert runner().classify_move(board, killer, None, (killer, None)) == "killer"
    assert runner().classify_move(board, other, None, (killer, None)) == "other"


def test_host_capabilities_are_not_silently_adapted() -> None:
    """Catches changing the 22-CPU/CPU-0-and-1 protocol to fit another host."""

    runner().validate_host_capabilities(22, set(range(22)))
    with pytest.raises(RuntimeError, match="22 logical CPUs"):
        runner().validate_host_capabilities(20, set(range(20)))
    with pytest.raises(RuntimeError, match="CPU affinity"):
        runner().validate_host_capabilities(22, set(range(2, 22)))


@pytest.mark.parametrize("entrypoint", ["preflight", "execute_run"])
@pytest.mark.parametrize(
    ("cpu_count", "affinity", "message"),
    [
        (21, set(range(21)), "22 logical CPUs"),
        (22, set(range(2, 22)), "CPU affinity"),
    ],
)
def test_replay_revalidates_host_before_pinning_or_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    cpu_count: int,
    affinity: set[int],
    message: str,
) -> None:
    """Catches replay pinning CPU 1 before validating the current topology."""

    module = runner()
    pinned: list[set[int]] = []
    monkeypatch.setattr(module.os, "cpu_count", lambda: cpu_count)
    monkeypatch.setattr(module.os, "sched_getaffinity", lambda _pid: affinity)
    monkeypatch.setattr(
        module.os,
        "sched_setaffinity",
        lambda _pid, cpus: pinned.append(set(cpus)),
    )
    monkeypatch.setattr(
        module,
        "host_state",
        lambda: pytest.fail("host observed before capability rejection"),
    )
    args = SimpleNamespace(
        output=tmp_path / "output",
        manifest=tmp_path / "manifest.json",
        preflight=tmp_path / "preflight.json",
    )
    args.manifest.write_text("{}", encoding="utf-8")
    args.preflight.write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        getattr(module, entrypoint)(args)

    assert pinned == []
    assert not args.output.exists()


def test_harness_pin_records_available_host_before_narrowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches confusing original host availability with harness affinity."""

    module = runner()
    affinity = set(range(22))
    monkeypatch.setattr(module.os, "cpu_count", lambda: 22)
    monkeypatch.setattr(module.os, "sched_getaffinity", lambda _pid: set(affinity))

    def pin(_pid: int, cpus: set[int]) -> None:
        affinity.clear()
        affinity.update(cpus)

    monkeypatch.setattr(module.os, "sched_setaffinity", pin)

    assert module.validate_and_pin_harness() == {
        "logical_cpu_count": 22,
        "available_affinity": list(range(22)),
    }
    assert affinity == {1}


def test_measurement_rejects_changed_availability_since_preflight() -> None:
    """Catches measuring after the available CPU set changes between phases."""

    preflight = {
        "host_availability": {
            "logical_cpu_count": 22,
            "available_affinity": list(range(22)),
        }
    }
    changed = {
        "logical_cpu_count": 22,
        "available_affinity": [0, 1, *range(3, 22)],
    }

    with pytest.raises(RuntimeError, match="changed since preflight"):
        runner().validate_same_host_availability(preflight, changed)


def test_measurement_plan_is_exact_complete_and_block_alternating() -> None:
    """Catches reordered, duplicated, omitted or appended timing work."""

    plan = runner().measurement_plan()
    assert len(plan) == 1200
    keys = {
        (item["block"], item["fixture"], item["engine"], item["repetition"])
        for item in plan
    }
    assert len(keys) == 1200
    assert [item["engine"] for item in plan[:10]] == ["baseline"] * 5 + [
        "candidate"
    ] * 5
    block_one = [item for item in plan if item["block"] == 1]
    assert [item["engine"] for item in block_one[:10]] == ["candidate"] * 5 + [
        "baseline"
    ] * 5


def test_timed_worker_result_binds_identity_semantics_options_and_affinity() -> None:
    """Catches accepting a fast result from the wrong build or search."""

    identity = {"imported": {"engine": "abc"}}
    semantic = {"bestmove": "e2e4", "score": 11, "completed_depth": 5}
    valid = {
        "identity": identity,
        "semantic": semantic,
        "options": {"hash_bytes": 16777216, "book": False},
        "worker_affinity": [0],
        "elapsed_ns": 123,
        "worker_exit_code": 0,
    }
    runner().validate_worker_result(valid, identity, semantic)
    mutations = (
        {**valid, "identity": {"wrong": True}},
        {**valid, "semantic": {**semantic, "score": 12}},
        {**valid, "options": {"hash_bytes": 1, "book": False}},
        {**valid, "worker_affinity": [0, 1]},
        {**valid, "elapsed_ns": 0},
        {**valid, "worker_exit_code": 1},
    )
    for changed in mutations:
        with pytest.raises(RuntimeError, match="timed worker result"):
            runner().validate_worker_result(changed, identity, semantic)


def test_measurement_requires_pass_preflight_for_same_manifest() -> None:
    """Catches timing after stale or failed coverage/parity evidence."""

    manifest_sha = "a" * 64
    runner_sha = "c" * 64
    coverage = {
        category: {"selected": 1, "cutoffs": 1}
        for category in ("tt", "capture", "killer", "other")
    }
    availability = {
        "logical_cpu_count": 22,
        "available_affinity": list(range(22)),
    }
    runner().validate_preflight(
        {
            "status": "PASS",
            "manifest_sha256": manifest_sha,
            "runner_sha256": runner_sha,
            "coverage": coverage,
            "host_availability": availability,
        },
        manifest_sha,
        runner_sha,
    )
    for invalid in (
        {"status": "FAIL", "manifest_sha256": manifest_sha,
         "runner_sha256": runner_sha, "coverage": coverage,
         "host_availability": availability},
        {"status": "PASS", "manifest_sha256": "b" * 64,
         "runner_sha256": runner_sha, "coverage": coverage,
         "host_availability": availability},
        {"status": "PASS", "manifest_sha256": manifest_sha,
         "runner_sha256": "wrong", "coverage": coverage,
         "host_availability": availability},
        {"status": "PASS", "manifest_sha256": manifest_sha,
         "runner_sha256": runner_sha, "coverage": None,
         "host_availability": availability},
        {"status": "PASS", "manifest_sha256": manifest_sha,
         "runner_sha256": runner_sha,
         "coverage": {**coverage, "killer": {"selected": 1, "cutoffs": 0}},
         "host_availability": availability},
    ):
        with pytest.raises(RuntimeError, match="preflight"):
            runner().validate_preflight(invalid, manifest_sha, runner_sha)


def test_independent_auditor_rejects_invalid_preflight_coverage() -> None:
    module = auditor()
    valid = {
        "status": "PASS",
        "manifest_sha256": "m" * 64,
        "runner_sha256": "g" * 64,
        "coverage": {
            category: {"selected": 1, "cutoffs": 1}
            for category in ("tt", "capture", "killer", "other")
        },
        "host_availability": {
            "logical_cpu_count": 22,
            "available_affinity": list(range(22)),
        },
    }
    module.validate_preflight_evidence(valid, "m" * 64, "g" * 64)
    for coverage in (None, {}, {
        category: {"selected": 1, "cutoffs": 0}
        for category in ("tt", "capture", "killer", "other")
    }):
        invalid = {**valid, "coverage": coverage}
        with pytest.raises(ValueError, match="preflight coverage"):
            module.validate_preflight_evidence(
                invalid, "m" * 64, "g" * 64
            )


@pytest.mark.parametrize(
    "availability",
    [
        None,
        {"logical_cpu_count": 20, "available_affinity": list(range(20))},
        {"logical_cpu_count": 22, "available_affinity": list(range(2, 22))},
    ],
)
def test_independent_auditor_rejects_invalid_preflight_host_availability(
    availability: object,
) -> None:
    """Catches accepting preflight evidence from a different host topology."""

    module = auditor()
    preflight = {
        "status": "PASS",
        "manifest_sha256": "m" * 64,
        "runner_sha256": "g" * 64,
        "coverage": {
            category: {"selected": 1, "cutoffs": 1}
            for category in ("tt", "capture", "killer", "other")
        },
        "host_availability": availability,
    }

    with pytest.raises(ValueError, match="host availability"):
        module.validate_preflight_evidence(preflight, "m" * 64, "g" * 64)


def test_independent_auditor_binds_its_identity_and_host_snapshot_index(
    tmp_path: Path,
) -> None:
    module = auditor()
    process_tool = {"path": "/usr/bin/ps", "sha256": "a" * 64}
    claimed: list[dict[str, str]] = []
    for block in range(10):
        for phase in ("before", "after"):
            name = f"host-block-{block:02d}-{phase}.json"
            path = tmp_path / name
            path.write_text(
                json.dumps({
                    "loadavg": [0.5, 0.5, 0.5],
                    "logical_cpu_count": 22,
                    "affinity": [1],
                    "processes": "PID PPID PSR COMMAND ARGS\n",
                    "process_tool": process_tool,
                }),
                encoding="utf-8",
            )
            claimed.append({
                "name": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    claimed.sort(key=lambda item: item["name"])

    module.validate_auditor_identity(
        {
            "auditor_sha256": "audit-sha",
            "runtime_policy_sha256": module.EXPECTED_RUNTIME_POLICY_SHA256,
        },
        "audit-sha",
    )
    module.audit_host_snapshots(tmp_path, claimed, process_tool)
    with pytest.raises(ValueError, match="auditor identity"):
        module.validate_auditor_identity(
            {
                "auditor_sha256": "wrong",
                "runtime_policy_sha256": module.EXPECTED_RUNTIME_POLICY_SHA256,
            },
            "audit-sha",
        )
    with pytest.raises(ValueError, match="runtime policy identity"):
        module.validate_auditor_identity(
            {
                "auditor_sha256": "audit-sha",
                "runtime_policy_sha256": "wrong",
            },
            "audit-sha",
        )
    (tmp_path / "host-block-00-before.json").write_text(
        '{"changed": true}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="host snapshot"):
        module.audit_host_snapshots(tmp_path, claimed, process_tool)


@pytest.mark.parametrize(
    ("field", "value"),
    [("logical_cpu_count", 20), ("affinity", [0, 1])],
)
def test_independent_host_audit_rejects_topology_or_unpinned_harness(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """Catches the auditor trusting snapshots from the wrong CPU topology."""

    module = auditor()
    process_tool = {"path": "/usr/bin/ps", "sha256": "a" * 64}
    claimed: list[dict[str, str]] = []
    for block in range(10):
        for phase in ("before", "after"):
            name = f"host-block-{block:02d}-{phase}.json"
            state: dict[str, object] = {
                "loadavg": [0.1, 0.1, 0.1],
                "logical_cpu_count": 22,
                "affinity": [1],
                "processes": "PID PPID PSR COMMAND ARGS\n",
                "process_tool": process_tool,
            }
            if block == 0 and phase == "before":
                state[field] = value
            path = tmp_path / name
            path.write_text(json.dumps(state), encoding="utf-8")
            claimed.append({
                "name": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    claimed.sort(key=lambda item: item["name"])

    with pytest.raises(ValueError, match="host snapshot capability"):
        module.audit_host_snapshots(tmp_path, claimed, process_tool)


@pytest.mark.parametrize("invalid_load", [float("nan"), float("inf"), float("-inf"), "busy"])
def test_independent_host_audit_rejects_nonfinite_or_nonnumeric_load(
    tmp_path: Path,
    invalid_load: object,
) -> None:
    module = auditor()
    process_tool = {"path": "/usr/bin/ps", "sha256": "a" * 64}
    claimed: list[dict[str, str]] = []
    for block in range(10):
        for phase in ("before", "after"):
            name = f"host-block-{block:02d}-{phase}.json"
            load = invalid_load if block == 0 and phase == "after" else 0.5
            path = tmp_path / name
            path.write_text(
                json.dumps({
                    "loadavg": [load, 0.5, 0.5],
                    "logical_cpu_count": 22,
                    "affinity": [1],
                    "processes": "PID PPID PSR COMMAND ARGS\n",
                    "process_tool": process_tool,
                }),
                encoding="utf-8",
            )
            claimed.append({
                "name": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    claimed.sort(key=lambda item: item["name"])

    with pytest.raises(ValueError, match="host snapshot gate"):
        module.audit_host_snapshots(tmp_path, claimed, process_tool)


def test_independent_host_audit_rejects_after_block_conflict(
    tmp_path: Path,
) -> None:
    module = auditor()
    process_tool = {"path": "/usr/bin/ps", "sha256": "a" * 64}
    claimed: list[dict[str, str]] = []
    for block in range(10):
        for phase in ("before", "after"):
            name = f"host-block-{block:02d}-{phase}.json"
            processes = "PID PPID PSR COMMAND ARGS\n"
            if block == 0 and phase == "after":
                processes += "42 1 3 fastchess fastchess -config injected\n"
            path = tmp_path / name
            path.write_text(
                json.dumps({
                    "loadavg": [0.1, 0.1, 0.1],
                    "logical_cpu_count": 22,
                    "affinity": [1],
                    "processes": processes,
                    "process_tool": process_tool,
                }),
                encoding="utf-8",
            )
            claimed.append({
                "name": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    claimed.sort(key=lambda item: item["name"])

    with pytest.raises(ValueError, match="host snapshot gate"):
        module.audit_host_snapshots(tmp_path, claimed, process_tool)


def test_independent_host_audit_rejects_process_tool_identity_change(
    tmp_path: Path,
) -> None:
    module = auditor()
    process_tool = {"path": "/usr/bin/ps", "sha256": "a" * 64}
    claimed: list[dict[str, str]] = []
    for block in range(10):
        for phase in ("before", "after"):
            name = f"host-block-{block:02d}-{phase}.json"
            snapshot_tool = process_tool
            if block == 0 and phase == "after":
                snapshot_tool = {**process_tool, "sha256": "0" * 64}
            path = tmp_path / name
            path.write_text(
                json.dumps({
                    "loadavg": [0.1, 0.1, 0.1],
                    "processes": "PID PPID PSR COMMAND ARGS\n",
                    "process_tool": snapshot_tool,
                }),
                encoding="utf-8",
            )
            claimed.append({
                "name": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    claimed.sort(key=lambda item: item["name"])

    with pytest.raises(ValueError, match="process tool identity"):
        module.audit_host_snapshots(tmp_path, claimed, process_tool)


def test_independent_host_audit_rejects_malformed_process_tool_identity(
    tmp_path: Path,
) -> None:
    module = auditor()
    process_tool = {"path": "ps", "sha256": "a" * 64}
    claimed: list[dict[str, str]] = []
    for block in range(10):
        for phase in ("before", "after"):
            name = f"host-block-{block:02d}-{phase}.json"
            path = tmp_path / name
            path.write_text(
                json.dumps({
                    "loadavg": [0.1, 0.1, 0.1],
                    "processes": "PID PPID PSR COMMAND ARGS\n",
                    "process_tool": process_tool,
                }),
                encoding="utf-8",
            )
            claimed.append({
                "name": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    claimed.sort(key=lambda item: item["name"])

    with pytest.raises(ValueError, match="process tool identity"):
        module.audit_host_snapshots(tmp_path, claimed, process_tool)


@pytest.mark.parametrize(
    "after_state",
    [
        {
            "loadavg": (0.1, 0.1, 0.1),
            "conflicts": [{"pid": 42, "args": "fastchess"}],
        },
        {"loadavg": (5.6, 0.1, 0.1), "conflicts": []},
        {"loadavg": (float("nan"), 0.1, 0.1), "conflicts": []},
    ],
)
def test_run_holds_after_bad_after_snapshot_before_next_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_state: dict[str, Any],
) -> None:
    module = runner()
    manifest = {
        "runner_sha256": "runner",
        "positions": [{"label": "fixture", "fen": "fixture"}],
        "engines": {
            "baseline": {"root": str(tmp_path), "identity": {"id": "base"}},
        },
    }
    manifest_path = tmp_path / "manifest.json"
    preflight_path = tmp_path / "preflight.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    preflight_path.write_text(
        json.dumps({
            "expected": {"0": {"semantic": "fixture"}},
            "host_availability": {
                "logical_cpu_count": 22,
                "available_affinity": list(range(22)),
            },
        }),
        encoding="utf-8",
    )
    healthy = {
        "loadavg": (0.1, 0.1, 0.1),
        "conflicts": [],
        "processes": "PID PPID PSR COMMAND ARGS\n",
    }
    calls = 0

    def fake_host_state() -> dict[str, Any]:
        nonlocal calls
        state = {**healthy, **after_state} if calls == 2 else healthy
        calls += 1
        return state

    monkeypatch.setattr(module, "host_state", fake_host_state)
    monkeypatch.setattr(
        module,
        "validate_and_pin_harness",
        lambda: {
            "logical_cpu_count": 22,
            "available_affinity": list(range(22)),
        },
    )
    monkeypatch.setattr(module, "validate_manifest", lambda _manifest: None)
    monkeypatch.setattr(module, "validate_preflight", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "measurement_plan",
        lambda: [
            {"block": 0, "fixture": 0, "engine": "baseline", "repetition": 0},
            {"block": 1, "fixture": 0, "engine": "baseline", "repetition": 0},
        ],
    )
    invoked: list[int] = []

    def invoke_fixture(_root: Path, payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        invoked.append(len(invoked))
        return {
            "elapsed_ns": 1,
            "semantic": {"semantic": "fixture"},
            "identity": payload["identity"],
            "options": {"hash_bytes": 16777216, "book": False},
            "worker_affinity": [0],
            "worker_exit_code": 0,
            "pid": 123,
        }

    monkeypatch.setattr(module, "invoke", invoke_fixture)
    monkeypatch.setattr(module, "validate_worker_result", lambda *_args: None)
    args = SimpleNamespace(
        output=tmp_path / "output",
        manifest=manifest_path,
        preflight=preflight_path,
    )

    result = module.execute_run(args)

    assert result["status"] == "HOLD"
    assert result["stopped_after_block"] == 0
    assert result["samples"] == 1
    assert invoked == [0]


def test_gate3_worker_identity_binds_python_chess_and_numpy_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = runner()
    chess_identity = {
        "root": "/bound/chess",
        "version": "fixture",
        "files": {"__init__.py": "a" * 64},
    }
    numpy_identity = {"root": "/bound/numpy", "version": "2.1.0"}
    monkeypatch.setattr(
        module.RUNTIME_POLICY,
        "imported_package_identity",
        lambda name: chess_identity if name == "chess" else pytest.fail(name),
        raising=False,
    )
    monkeypatch.setattr(
        module.RUNTIME_POLICY,
        "imported_package_location_identity",
        lambda name: numpy_identity if name == "numpy" else pytest.fail(name),
        raising=False,
    )
    monkeypatch.setattr(module.RUNTIME_POLICY, "runtime_identity", lambda: {})
    monkeypatch.setattr(module.RUNTIME_POLICY, "validate_runtime_identity", lambda _: None)
    monkeypatch.setattr(module, "sha", lambda _: "digest")

    def fake_import(name: str) -> SimpleNamespace:
        if name == "chess":
            return SimpleNamespace(__version__="fixture")
        suffix = ".so" if ".cython_ext." in name else ".py"
        path = ROOT / "src" / Path(*name.split("."))
        return SimpleNamespace(__file__=str(path.with_suffix(suffix)))

    monkeypatch.setattr(module.importlib, "import_module", fake_import)

    identity = module.imported_identity(ROOT)

    assert identity["packages"] == {
        "chess": chess_identity,
        "numpy": numpy_identity,
    }


def test_gate3_identity_worker_runs_without_site_and_loads_bound_native_modules() -> None:
    """Catches Gate 3 reverting to a site-started worker process."""

    identity = runner().invoke(ROOT, {"mode": "identity"})

    assert identity["runtime"]["flags"]["no_site"] == 1
    assert identity["runtime"]["startup_modules"] == {}
    assert identity["packages"]["chess"]["files"]
    assert all(
        value["path"].endswith(".so")
        for name, value in identity["imported"].items()
        if ".cython_ext." in name
    )


def test_gate3_tool_binding_includes_future_match_runner() -> None:
    module = runner()
    manifest = {
        "runner_sha256": "gate3",
        "auditor_sha256": "audit",
        "match_runner_sha256": "match",
        "runtime_policy_sha256": "policy",
        "engines": {
            "candidate": {
                "source": {
                    "scripts/ap04b_ordering_benchmark.py": "gate3",
                    "scripts/ap04b_gate3_audit.py": "audit",
                    "scripts/ap04b_match_pipeline.py": "match",
                    "scripts/ap04b_runtime_policy.py": "policy",
                }
            }
        },
    }
    module.validate_tool_binding(manifest)
    manifest["match_runner_sha256"] = "wrong"
    with pytest.raises(RuntimeError, match="match runner"):
        module.validate_tool_binding(manifest)
    manifest["match_runner_sha256"] = "match"
    manifest["runtime_policy_sha256"] = "wrong"
    with pytest.raises(RuntimeError, match="runtime policy"):
        module.validate_tool_binding(manifest)


def test_identity_invocation_is_not_polluted_by_transport_exit_code() -> None:
    """Catches freezing worker process metadata as imported module identity."""

    identity = {"imported": {"engine": "abc"}}
    assert runner().attach_exit_code("identity", identity, 0) == identity
    measured = runner().attach_exit_code("measure", identity, 0)
    assert measured == {**identity, "worker_exit_code": 0}


def test_independent_audit_binds_every_row_to_manifest_and_preflight() -> None:
    """Catches recomputing ratios from rows produced by another build/search."""

    identities = {
        "baseline": {"module": "baseline-so"},
        "candidate": {"module": "candidate-so"},
    }
    semantics = {
        str(index): {"bestmove": "e2e4", "score": index, "completed_depth": 5}
        for index in range(12)
    }
    manifest = {
        "runner_sha256": "g" * 64,
        "auditor_sha256": hashlib.sha256(
            (ROOT / "scripts" / "ap04b_gate3_audit.py").read_bytes()
        ).hexdigest(),
        "runtime_policy_sha256": auditor().EXPECTED_RUNTIME_POLICY_SHA256,
        "positions": [
            {"label": f"fixture-{index}", "fen": "unused"}
            for index in range(12)
        ],
        "engines": {
            label: {"identity": identity}
            for label, identity in identities.items()
        },
    }
    preflight = {
        "status": "PASS",
        "manifest_sha256": "m" * 64,
        "runner_sha256": "g" * 64,
        "coverage": {
            category: {"selected": 1, "cutoffs": 1}
            for category in ("tt", "capture", "killer", "other")
        },
        "expected": semantics,
        "host_availability": {
            "logical_cpu_count": 22,
            "available_affinity": list(range(22)),
        },
    }
    evidence_rows = sorted(
        rows(),
        key=lambda row: (
            row["block"],
            row["fixture"],
            (0 if row["engine"] == "baseline" else 1)
            if row["block"] % 2 == 0
            else (0 if row["engine"] == "candidate" else 1),
            row["repetition"],
        ),
    )
    for row in evidence_rows:
        row.update(
            label=f"fixture-{row['fixture']}",
            semantic=semantics[str(row["fixture"])],
            identity_sha256=hashlib.sha256(
                auditor().canonical(identities[row["engine"]])
            ).hexdigest(),
            options={"hash_bytes": 16777216, "book": False},
            worker_affinity=[0],
            worker_exit_code=0,
        )
    claimed = auditor().audit_rows(evidence_rows)
    claimed.update(
        manifest_sha256="m" * 64,
        preflight_sha256="p" * 64,
        raw_sha256="r" * 64,
        runner_sha256="g" * 64,
        host_snapshot_index=[],
        host_availability=preflight["host_availability"],
    )
    audited = auditor().audit_evidence(
        manifest,
        preflight,
        evidence_rows,
        claimed,
        manifest_sha256="m" * 64,
        preflight_sha256="p" * 64,
        raw_sha256="r" * 64,
    )
    assert audited["status"] == "PASS"

    evidence_rows[0]["identity_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="row identity"):
        auditor().audit_evidence(
            manifest,
            preflight,
            evidence_rows,
            claimed,
            manifest_sha256="m" * 64,
            preflight_sha256="p" * 64,
            raw_sha256="r" * 64,
        )


@pytest.mark.parametrize("manifest_hash", [None, "x" * 64])
def test_independent_audit_requires_exact_preflight_manifest_binding(
    manifest_hash: str | None,
) -> None:
    """Catches accepting missing or mismatched preflight identity."""

    preflight = {"status": "PASS", "expected": {}}
    if manifest_hash is not None:
        preflight["manifest_sha256"] = manifest_hash
    with pytest.raises(ValueError, match="preflight identity"):
        auditor().audit_evidence(
            {
                "positions": [],
                "engines": {},
                "runner_sha256": "g" * 64,
                "auditor_sha256": hashlib.sha256(
                    (ROOT / "scripts" / "ap04b_gate3_audit.py").read_bytes()
                ).hexdigest(),
                "runtime_policy_sha256": (
                    auditor().EXPECTED_RUNTIME_POLICY_SHA256
                ),
            },
            preflight,
            [],
            {
                "manifest_sha256": "m" * 64,
                "preflight_sha256": "p" * 64,
                "raw_sha256": "r" * 64,
                "runner_sha256": "g" * 64,
                "host_snapshot_index": [],
            },
            manifest_sha256="m" * 64,
            preflight_sha256="p" * 64,
            raw_sha256="r" * 64,
        )
