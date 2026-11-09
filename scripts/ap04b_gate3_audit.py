"""Independent raw-evidence auditor for AP-04B Gate 3."""

from __future__ import annotations

import argparse
import hashlib
from importlib import util
import json
import math
from pathlib import Path
import shlex
import statistics
from types import ModuleType
from typing import Any


ENGINES = ("baseline", "candidate")
BLOCKS = 10
FIXTURES = 12
REPETITIONS = 5
CATEGORIES = ("tt", "capture", "killer", "other")
EXPECTED_RUNTIME_POLICY_SHA256 = (
    "a8ce1a9367d7fa7d6abd0d9ca0cd21460b9ab8db495f772b7e1b5cdfc93f731f"
)


def load_runtime_policy() -> ModuleType:
    path = Path(__file__).with_name("ap04b_runtime_policy.py")
    if hashlib.sha256(path.read_bytes()).hexdigest() != (
        EXPECTED_RUNTIME_POLICY_SHA256
    ):
        raise RuntimeError("AP04B runtime policy identity changed")
    spec = util.spec_from_file_location("ap04b_runtime_policy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load AP04B runtime policy")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME_POLICY = load_runtime_policy()


def validate_auditor_environment() -> None:
    """Reject loader influence before creating independent audit evidence."""
    RUNTIME_POLICY.validated_environment()


def validate_auditor_runtime(manifest: dict[str, Any]) -> None:
    """Admit only the interpreter identity recorded by Gate-3 freeze."""
    try:
        RUNTIME_POLICY.verify_runtime_identity(manifest["auditor_runtime"])
    except (KeyError, TypeError, RuntimeError) as exc:
        raise RuntimeError("frozen auditor interpreter changed") from exc


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def new_output(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)


def validate_coverage(coverage: Any) -> None:
    if not isinstance(coverage, dict) or set(coverage) != set(CATEGORIES):
        raise ValueError("preflight coverage is incomplete")
    for category in CATEGORIES:
        values = coverage[category]
        if (
            not isinstance(values, dict)
            or set(values) != {"selected", "cutoffs"}
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                for value in values.values()
            )
        ):
            raise ValueError("preflight coverage is incomplete")


def validate_preflight_evidence(
    preflight: dict[str, Any],
    manifest_sha256: str,
    runner_sha256: str,
) -> None:
    if (
        preflight.get("status") != "PASS"
        or preflight.get("manifest_sha256") != manifest_sha256
        or preflight.get("runner_sha256") != runner_sha256
    ):
        raise ValueError("preflight identity mismatch")
    validate_coverage(preflight.get("coverage"))
    validate_host_availability(preflight.get("host_availability"))


def validate_host_availability(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("host availability mismatch")
    cpu_count = value.get("logical_cpu_count")
    affinity = value.get("available_affinity")
    if (
        cpu_count != 22
        or not isinstance(affinity, list)
        or any(
            not isinstance(cpu, int) or isinstance(cpu, bool)
            for cpu in affinity
        )
        or not {0, 1}.issubset(affinity)
    ):
        raise ValueError("host availability mismatch")


def validate_auditor_identity(
    manifest: dict[str, Any], auditor_sha256: str
) -> None:
    if manifest.get("auditor_sha256") != auditor_sha256:
        raise ValueError("auditor identity mismatch")
    if manifest.get("runtime_policy_sha256") != EXPECTED_RUNTIME_POLICY_SHA256:
        raise ValueError("runtime policy identity mismatch")


def starts_stockconda_module(command: str, args: str) -> bool:
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    executable = Path(tokens[0]).name.lower() if tokens else command.lower()
    if not executable.startswith("python"):
        return False
    return any(
        token == "-m" and tokens[index + 1] == "chess_engine.stockconda"
        for index, token in enumerate(tokens[:-1])
    )


def conflicting_processes(snapshot: str) -> list[str]:
    patterns = (
        "fastchess", "stockconda.py", "pytest", "cprofile", "py-spy",
        "perf record", "setup.py build_ext",
    )
    conflicts: list[str] = []
    for line in snapshot.splitlines()[1:]:
        fields = line.strip().split(maxsplit=4)
        if len(fields) != 5:
            continue
        lowered = line.lower()
        if any(pattern in lowered for pattern in patterns) or (
            starts_stockconda_module(fields[3], fields[4])
        ):
            conflicts.append(line)
    return conflicts


def validate_process_tool_identity(identity: dict[str, str]) -> None:
    path = identity.get("path")
    digest = identity.get("sha256")
    if (
        set(identity) != {"path", "sha256"}
        or not isinstance(path, str)
        or not Path(path).is_absolute()
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("process tool identity mismatch")


def audit_host_snapshots(
    root: Path,
    claimed: list[dict[str, str]],
    expected_process_tool: dict[str, str],
) -> None:
    validate_process_tool_identity(expected_process_tool)
    expected = {
        f"host-block-{block:02d}-{phase}.json"
        for block in range(BLOCKS)
        for phase in ("before", "after")
    }
    paths = sorted(root.glob("host-block-*.json"))
    actual = [{"name": path.name, "sha256": sha(path)} for path in paths]
    if {path.name for path in paths} != expected or actual != claimed:
        raise ValueError("host snapshot identity mismatch")
    for path in paths:
        state = json.loads(path.read_text(encoding="utf-8"))
        loadavg = state.get("loadavg")
        processes = state.get("processes")
        if state.get("process_tool") != expected_process_tool:
            raise ValueError("process tool identity mismatch")
        if (
            state.get("logical_cpu_count") != 22
            or state.get("affinity") != [1]
        ):
            raise ValueError("host snapshot capability mismatch")
        if (
            not isinstance(loadavg, list)
            or not loadavg
            or not isinstance(loadavg[0], (int, float))
            or isinstance(loadavg[0], bool)
            or not math.isfinite(loadavg[0])
            or loadavg[0] > 5.5
            or not isinstance(processes, str)
            or conflicting_processes(processes)
        ):
            raise ValueError("host snapshot gate mismatch")


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"malformed raw JSONL at line {line_number}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"malformed raw JSONL at line {line_number}"
                )
            rows.append(value)
    return rows


def relative_mad(values: list[float]) -> float:
    median = statistics.median(values)
    return statistics.median(abs(value - median) for value in values) / median


def _ratios(
    rows: list[dict[str, Any]],
) -> tuple[list[float], list[list[float]]]:
    expected = {
        (block, fixture, engine, repetition)
        for block in range(BLOCKS)
        for fixture in range(FIXTURES)
        for engine in ENGINES
        for repetition in range(REPETITIONS)
    }
    samples: dict[tuple[int, int, str, int], int] = {}
    for row in rows:
        try:
            key = (
                row["block"],
                row["fixture"],
                row["engine"],
                row["repetition"],
            )
            duration = row["elapsed_ns"]
        except KeyError as exc:
            raise ValueError("invalid measurement grid row") from exc
        if (
            key not in expected
            or key in samples
            or not isinstance(duration, int)
            or isinstance(duration, bool)
            or duration <= 0
        ):
            raise ValueError("invalid measurement grid row")
        samples[key] = duration
    if set(samples) != expected:
        raise ValueError("incomplete measurement grid")
    totals = {
        (block, fixture, engine): sum(
            samples[block, fixture, engine, repetition]
            for repetition in range(REPETITIONS)
        )
        for block in range(BLOCKS)
        for fixture in range(FIXTURES)
        for engine in ENGINES
    }
    aggregate = [
        sum(totals[block, fixture, "candidate"] for fixture in range(FIXTURES))
        / sum(totals[block, fixture, "baseline"] for fixture in range(FIXTURES))
        for block in range(BLOCKS)
    ]
    fixtures = [
        [
            totals[block, fixture, "candidate"]
            / totals[block, fixture, "baseline"]
            for block in range(BLOCKS)
        ]
        for fixture in range(FIXTURES)
    ]
    return aggregate, fixtures


def audit_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate, fixtures = _ratios(rows)
    aggregate_median = statistics.median(aggregate)
    fixture_medians = [statistics.median(series) for series in fixtures]
    dispersion = [relative_mad(series) for series in [aggregate, *fixtures]]
    if not all(
        math.isfinite(value) and value > 0
        for series in [aggregate, *fixtures]
        for value in series
    ):
        status, reason = "FAIL", "technical, identity, parity or incomplete sample"
    elif max(dispersion) > 0.03:
        status, reason = "HOLD", "relative MAD exceeds 3%"
    elif aggregate_median > 1.00 or max(fixture_medians) > 1.03:
        status, reason = "FAIL", "stable regression exceeds frozen threshold"
    elif aggregate_median <= 0.97:
        status, reason = (
            "PASS",
            "stable aggregate gain at least 3%, fixture bounds satisfied",
        )
    else:
        status, reason = "HOLD", "stable aggregate gain below 3%"
    return {
        "status": status,
        "reason": reason,
        "samples": len(rows),
        "aggregate_median": aggregate_median,
        "fixture_medians": fixture_medians,
        "relative_mad": dispersion,
        "aggregate_ratios": aggregate,
        "fixture_ratios": fixtures,
    }


def compare_runner_result(
    recomputed: dict[str, Any],
    claimed: dict[str, Any],
) -> None:
    keys = (
        "status",
        "reason",
        "samples",
        "aggregate_median",
        "fixture_medians",
        "relative_mad",
        "aggregate_ratios",
        "fixture_ratios",
    )
    if any(recomputed.get(key) != claimed.get(key) for key in keys):
        raise ValueError("runner result mismatch")


def audit_evidence(
    manifest: dict[str, Any],
    preflight: dict[str, Any],
    rows: list[dict[str, Any]],
    claimed: dict[str, Any],
    *,
    manifest_sha256: str,
    preflight_sha256: str,
    raw_sha256: str,
) -> dict[str, Any]:
    validate_auditor_identity(manifest, sha(Path(__file__)))
    if (
        claimed.get("manifest_sha256") != manifest_sha256
        or claimed.get("preflight_sha256") != preflight_sha256
        or claimed.get("raw_sha256") != raw_sha256
        or claimed.get("runner_sha256") != manifest.get("runner_sha256")
    ):
        raise ValueError("runner evidence hash mismatch")
    runner_sha256 = manifest.get("runner_sha256")
    if not isinstance(runner_sha256, str):
        raise ValueError("runner identity mismatch")
    validate_preflight_evidence(preflight, manifest_sha256, runner_sha256)
    validate_host_availability(claimed.get("host_availability"))
    if claimed.get("host_availability") != preflight.get("host_availability"):
        raise ValueError("host availability changed between phases")
    positions = manifest.get("positions")
    expected_semantics = preflight.get("expected")
    if not isinstance(positions, list) or len(positions) != FIXTURES:
        raise ValueError("manifest fixture mismatch")
    if not isinstance(expected_semantics, dict) or len(expected_semantics) != FIXTURES:
        raise ValueError("preflight semantic grid mismatch")
    identities = {
        label: hashlib.sha256(
            canonical(manifest["engines"][label]["identity"])
        ).hexdigest()
        for label in ENGINES
    }
    expected_order = [
        (block, fixture, engine, repetition)
        for block in range(BLOCKS)
        for fixture in range(FIXTURES)
        for engine in (
            ENGINES if block % 2 == 0 else ("candidate", "baseline")
        )
        for repetition in range(REPETITIONS)
    ]
    if len(rows) != len(expected_order):
        raise ValueError("raw row count mismatch")
    for index, row in enumerate(rows):
        actual_key = (
            row.get("block"),
            row.get("fixture"),
            row.get("engine"),
            row.get("repetition"),
        )
        if actual_key != expected_order[index]:
            raise ValueError("raw row order mismatch")
        fixture = row["fixture"]
        engine = row["engine"]
        if row.get("label") != positions[fixture]["label"]:
            raise ValueError("raw row fixture mismatch")
        if row.get("semantic") != expected_semantics[str(fixture)]:
            raise ValueError("raw row semantic mismatch")
        if row.get("identity_sha256") != identities[engine]:
            raise ValueError("raw row identity mismatch")
        if row.get("options") != {"hash_bytes": 16777216, "book": False}:
            raise ValueError("raw row option mismatch")
        if row.get("worker_affinity") != [0] or row.get("worker_exit_code") != 0:
            raise ValueError("raw row process mismatch")
    recomputed = audit_rows(rows)
    compare_runner_result(recomputed, claimed)
    return {
        **recomputed,
        "manifest_sha256": manifest_sha256,
        "preflight_sha256": preflight_sha256,
        "raw_sha256": raw_sha256,
        "runner_sha256": runner_sha256,
        "auditor_sha256": manifest["auditor_sha256"],
        "host_snapshot_index": claimed["host_snapshot_index"],
        "audit": "PASS",
    }


def execute_audit(args: argparse.Namespace) -> dict[str, Any]:
    validate_auditor_environment()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    validate_auditor_runtime(manifest)
    new_output(args.output)
    report: dict[str, Any] = {"audit": "RUNNING"}
    write_json(args.output / "start.json", report)
    try:
        preflight = json.loads(args.preflight.read_text(encoding="utf-8"))
        claimed = json.loads(args.result.read_text(encoding="utf-8"))
        rows = read_rows(args.raw)
        host_snapshot_index = claimed.get("host_snapshot_index")
        if not isinstance(host_snapshot_index, list):
            raise ValueError("host snapshot identity mismatch")
        process_tool = manifest.get("process_tool")
        if not isinstance(process_tool, dict):
            raise ValueError("process tool identity mismatch")
        audit_host_snapshots(
            args.result.parent,
            host_snapshot_index,
            process_tool,
        )
        report = audit_evidence(
            manifest,
            preflight,
            rows,
            claimed,
            manifest_sha256=sha(args.manifest),
            preflight_sha256=sha(args.preflight),
            raw_sha256=sha(args.raw),
        )
    except BaseException as exc:
        report = {"audit": "FAIL", "error": repr(exc)}
    write_json(args.output / "result.json", report)
    print(json.dumps(report, sort_keys=True), flush=True)
    return report


def main() -> int:
    RUNTIME_POLICY.require_controlled_startup()
    validate_auditor_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return 0 if execute_audit(args).get("audit") == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
