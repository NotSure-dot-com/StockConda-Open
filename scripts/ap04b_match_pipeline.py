"""Identity-bound AP04B-final preflight, orientation and release pipeline."""
from __future__ import annotations

import argparse
import builtins
from decimal import Decimal
import hashlib
from importlib import util
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
from types import ModuleType
from typing import Any, cast


RESULTS = Path("/results/ap04b_cleanup_final_20260920_1")
MANIFEST = Path("/tmp/ap04b_cleanup_final_gate_20260920_1/gate3-freeze/manifest.json")
PERFORMANCE = Path("/tmp/ap04b_cleanup_final_gate_20260920_1/gate3-run/result.json")
PERFORMANCE_AUDIT = Path("/tmp/ap04b_cleanup_final_gate_20260920_1/gate3-audit/result.json")
MATCH_AUDITOR = Path(__file__).with_name("ap03_match_audit.py")
UCI_AUDITOR = Path(__file__).with_name("ap03_uci_audit.py")
TEMPLATE = Path("/results/ap03_preflight1/autosave.json")
FASTCHESS = Path("/reference/tools/fastchess/fastchess")
BOOK = Path("/reference/books/8moves_v3.pgn")
CANDIDATE_LAUNCHER = Path("/tmp/ap04b_cleanup_final_gate_20260920_1/candidate-launch.sh")
BASELINE_LAUNCHER = Path("/tmp/ap04b_cleanup_final_gate_20260920_1/baseline-launch.sh")
NAMES = ["AP04B_Final_1e96e2f", "AP04B_Baseline_a2d608e"]
STAGES = (
    ("preflight", 4, 1, 600, False),
    ("orientation", 200, 4, 3600, False),
    ("release", 2000, 4, 43200, True),
)
EXPECTED_MANIFEST = "1a4cf2098d3b210637cf2034c5cf641bc3608713064ad24cf1f1924452f1196b"
EXPECTED_PERFORMANCE = "c23314c80728b596ca1ceba7b5f50842e188162d39626650ef3060c93f057cd0"
EXPECTED_PERFORMANCE_AUDIT = "99c44be13de0f7e42da215fa672087c4f1bb0c3147ef9754cd8a916132e7012d"
AUDITOR_HASH_MODE = "sha256-crlf-to-lf-v1"
EXPECTED_MATCH_AUDITOR = "60ca7cfb77da90ec5f3f7b4a966d7db9164358b0f354e74108b787941be87273"
EXPECTED_UCI_AUDITOR = "3c414be028f299e5b49f08706eb1f25539589308fee805d4b7b8a248678e8a2f"
EXPECTED_LAUNCHER_SHA256 = {
    "candidate": "5855f257e0f7e2399d4f96351574aa2fdfd6325aeebedbc919dfbfa3dca67095",
    "baseline": "c4c61493b1bcfba846a3131dd69663188f44c43ddfd31433670864ff59cb7862",
}
EXPECTED_FASTCHESS = "4277d081b9478366d70934ad638a85958c5f006aa3cc73f3ce87ec251dbd97a7"
EXPECTED_BOOK = "5835239f88cc2c7511b177c32392a69f3ede21819cf0616f80a7f907cd21d17e"
EXPECTED_TEMPLATE = "447cdd835a5bee1a79b92146ea88a47f0c5fecc690818f0419c418a8edebfb53"
EXPECTED_STAGE_AUDIT_SHA256 = {
    "preflight": "87c48cf96434b525598c4da6d661752f7471337f548abc938b52e229767de9e1",
    "orientation": "ecdaa6b2b135294537fef107951807853ee6cad8e8a804ffba7ef63db5052da6",
    "release": "5949e1c413667eea76e9955b0af05238d08e77c1ef16a789a5691f58b7a09583",
}


def load_runtime_policy() -> ModuleType:
    path = Path(__file__).with_name("ap04b_runtime_policy.py")
    spec = util.spec_from_file_location("ap04b_runtime_policy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load AP04B runtime policy")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME_POLICY = load_runtime_policy()


def configure_admission(path: Path, expected_sha256: str) -> None:
    global MANIFEST, PERFORMANCE, PERFORMANCE_AUDIT, TEMPLATE, RESULTS
    global CANDIDATE_LAUNCHER, BASELINE_LAUNCHER
    global EXPECTED_MANIFEST, EXPECTED_PERFORMANCE, EXPECTED_PERFORMANCE_AUDIT
    global EXPECTED_TEMPLATE, EXPECTED_LAUNCHER_SHA256
    global EXPECTED_STAGE_AUDIT_SHA256
    if sha(path) != expected_sha256:
        raise RuntimeError("admission identity changed")
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "manifest", "performance", "performance_audit",
        "candidate_launcher", "baseline_launcher", "template", "results",
        "stage_audit_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RuntimeError("admission identity is incomplete")

    def admitted(name: str) -> tuple[Path, str]:
        entry = value[name]
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "sha256"}
            or not isinstance(entry["path"], str)
            or not isinstance(entry["sha256"], str)
        ):
            raise RuntimeError("admission identity is incomplete: " + name)
        return Path(entry["path"]), entry["sha256"]

    MANIFEST, EXPECTED_MANIFEST = admitted("manifest")
    PERFORMANCE, EXPECTED_PERFORMANCE = admitted("performance")
    PERFORMANCE_AUDIT, EXPECTED_PERFORMANCE_AUDIT = admitted(
        "performance_audit"
    )
    TEMPLATE, EXPECTED_TEMPLATE = admitted("template")
    CANDIDATE_LAUNCHER, candidate_sha = admitted("candidate_launcher")
    BASELINE_LAUNCHER, baseline_sha = admitted("baseline_launcher")
    EXPECTED_LAUNCHER_SHA256 = {
        "candidate": candidate_sha,
        "baseline": baseline_sha,
    }
    results = value["results"]
    stage_hashes = value["stage_audit_sha256"]
    if (
        not isinstance(results, dict)
        or set(results) != {"path"}
        or not isinstance(results["path"], str)
        or not isinstance(stage_hashes, dict)
        or any(
            stage not in {item[0] for item in STAGES}
            or not isinstance(digest, str)
            for stage, digest in stage_hashes.items()
        )
    ):
        raise RuntimeError("admission identity is incomplete")
    RESULTS = Path(results["path"])
    EXPECTED_STAGE_AUDIT_SHA256 = dict(stage_hashes)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verified_auditor_source(path: Path, expected: str, label: str) -> bytes:
    # Canonicalize ONLY CRLF pairs. No decoding, whitespace stripping, bare-CR
    # conversion or AST normalization: every other source byte remains pinned.
    source = path.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(source).hexdigest() != expected:
        raise RuntimeError(label + " auditor identity changed")
    return source


def verified_match_auditor_source() -> bytes:
    return verified_auditor_source(MATCH_AUDITOR, EXPECTED_MATCH_AUDITOR, "match")


def compile_auditor(source: bytes, path: Path) -> Any:
    return compile(source, str(path), "exec", optimize=0)


def validate_match_runtime(identity: dict[str, Any]) -> None:
    RUNTIME_POLICY.validate_runtime_identity(identity)


def is_authored_source(relative: str) -> bool:
    suffix = Path(relative).suffix
    return suffix in {".py", ".pyi", ".pyx", ".pxd", ".h"} or (
        relative.endswith("cython_chess_backend.cpp")
    )


def current_authored_sources(root: Path) -> set[str]:
    sources: set[str] = set()
    for directory in ("src", "scripts"):
        source_root = root / directory
        if not source_root.is_dir():
            continue
        for path in source_root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_file() and is_authored_source(relative):
                sources.add(relative)
    return sources


def input_config(template: dict[str, Any], output: Path, tc: str) -> dict[str, Any]:
    config: dict[str, Any] = json.loads(json.dumps(template))
    config["stats"] = {}
    base, increment = (Decimal(value) * 1000 for value in tc.split("+"))
    if base != int(base) or increment != int(increment):
        raise ValueError("time control must have exact integer milliseconds")
    for engine, name, launcher in zip(
        config["engines"], NAMES, (CANDIDATE_LAUNCHER, BASELINE_LAUNCHER)
    ):
        engine["name"] = name
        engine["cmd"] = str(launcher)
        engine["options"] = [["Book", "false"], ["Hash", "16"]]
        engine["limit"]["tc"] = {
            "time": int(base), "increment": int(increment), "fixed_time": 0,
            "moves": 0, "timemargin": 0,
        }
    config["pgn"]["file"] = str(output / "games.pgn")
    config["pgn"]["append_file"] = False
    config["log"]["file"] = str(output / "protocol.log")
    config["log"].update(level=1, append_file=False, realtime=True, engine_coms=True)
    return config


def match_command(output: Path, games: int, concurrency: int) -> list[str]:
    return [
        str(FASTCHESS), "-config", "file=" + str(output / "input.json"), "stats=false",
        "-openings", "file=" + str(BOOK), "format=pgn", "order=sequential",
        "-srand", "90210", "-rounds", str(games // 2), "-repeat",
        "-concurrency", str(concurrency), "-use-affinity", "0-21",
        "-resign", "movecount=3", "score=600",
        "-draw", "movenumber=34", "movecount=8", "score=20",
        "-report", "penta=true", "-autosaveinterval", "20",
        "-config", "outname=" + str(output / "autosave.json"),
        "-pgnout", "file=" + str(output / "games.pgn"), "append=false",
        "-log", "file=" + str(output / "protocol.log"), "level=trace", "engine=true",
        "append=false", "realtime=true", "-strict",
    ]


def verify_identity() -> dict[str, Any]:
    runtime = RUNTIME_POLICY.runtime_identity()
    validate_match_runtime(runtime)
    verified_match_auditor_source()
    verified_auditor_source(UCI_AUDITOR, EXPECTED_UCI_AUDITOR, "UCI")
    if (sha(MANIFEST) != EXPECTED_MANIFEST or sha(PERFORMANCE) != EXPECTED_PERFORMANCE
            or sha(PERFORMANCE_AUDIT) != EXPECTED_PERFORMANCE_AUDIT):
        raise RuntimeError("frozen AP04 evidence identity changed")
    manifest = cast(dict[str, Any], json.loads(MANIFEST.read_text()))
    performance = json.loads(PERFORMANCE.read_text())
    audit = json.loads(PERFORMANCE_AUDIT.read_text())
    if (performance["status"] != "PASS" or audit["audit"] != "PASS"
            or audit["status"] != "PASS"):
        raise RuntimeError("AP04B performance gate is not admitted")
    if (performance["manifest_sha256"] != EXPECTED_MANIFEST
            or audit["manifest_sha256"] != EXPECTED_MANIFEST
            or audit["raw_sha256"] != performance["raw_sha256"]
            or audit["preflight_sha256"] != performance["preflight_sha256"]
            or audit["samples"] != performance["samples"]
            or audit["aggregate_median"] != performance["aggregate_median"]):
        raise RuntimeError("performance audit is not bound to the admitted evidence")
    try:
        candidate_sources = manifest["engines"]["candidate"]["source"]
        frozen_match_runner = candidate_sources["scripts/ap04b_match_pipeline.py"]
        frozen_policy = candidate_sources["scripts/ap04b_runtime_policy.py"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("match runner identity is missing") from exc
    if (
        manifest.get("match_runner_sha256") != frozen_match_runner
        or sha(Path(__file__)) != frozen_match_runner
    ):
        raise RuntimeError("match runner identity is not admitted by Gate 3")
    if (
        manifest.get("runtime_policy_sha256") != frozen_policy
        or sha(Path(__file__).with_name("ap04b_runtime_policy.py"))
        != frozen_policy
    ):
        raise RuntimeError("runtime policy identity is not admitted by Gate 3")
    for entry in manifest["engines"].values():
        identity = entry["identity"]
        if (
            identity.get("python") != sys.version
            or identity.get("platform") != platform.platform()
            or identity.get("executable") != runtime.get("executable")
        ):
            raise RuntimeError("frozen interpreter identity changed")
        try:
            RUNTIME_POLICY.verify_runtime_identity(identity["runtime"])
        except (KeyError, TypeError, RuntimeError) as exc:
            raise RuntimeError("frozen interpreter identity changed") from exc
        root = Path(entry["root"])
        if sha(Path(entry["archive"])) != entry["archive_sha256"]:
            raise RuntimeError("engine archive changed")
        frozen_sources = {
            relative
            for relative in entry["source"]
            if is_authored_source(relative)
        }
        unexpected_sources = sorted(current_authored_sources(root) - frozen_sources)
        if unexpected_sources:
            raise RuntimeError("unexpected engine source: " + unexpected_sources[0])
        for relative, digest in entry["source"].items():
            if sha(root / relative) != digest:
                raise RuntimeError("engine source changed: " + relative)
        RUNTIME_POLICY.verify_executable_root(root, entry["executables"])
        for imported in entry["identity"]["imported"].values():
            if sha(Path(imported["path"])) != imported["sha256"]:
                raise RuntimeError("imported engine artifact changed")
        packages = entry["identity"].get("packages")
        if not isinstance(packages, dict) or set(packages) != {"chess", "numpy"}:
            raise RuntimeError("Python package identity is missing")
        RUNTIME_POLICY.verify_imported_package("chess", packages["chess"])
        RUNTIME_POLICY.verify_imported_package_location(
            "numpy", packages["numpy"]
        )
    if sha(FASTCHESS) != EXPECTED_FASTCHESS or sha(BOOK) != EXPECTED_BOOK:
        raise RuntimeError("Fastchess or opening book identity changed")
    if sha(TEMPLATE) != EXPECTED_TEMPLATE:
        raise RuntimeError("match template identity changed")
    for label, launcher in (("candidate", CANDIDATE_LAUNCHER), ("baseline", BASELINE_LAUNCHER)):
        if not launcher.is_file() or sha(launcher) != EXPECTED_LAUNCHER_SHA256[label]:
            raise RuntimeError("engine launcher identity changed: " + label)
        entry = manifest["engines"][label]
        package_root = Path(entry["identity"]["packages"]["chess"]["root"])
        python_path = f"{entry['root']}/src:{package_root.parent}"
        expected_script = (
            "#!/bin/sh\n"
            "unset LD_PRELOAD LD_AUDIT LD_LIBRARY_PATH\n"
            "export PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 "
            "PYTHONNOUSERSITE=1 PYTHONOPTIMIZE=0 PYTHONSAFEPATH=1\n"
            f"export PYTHONPATH={python_path}\n"
            f"exec {entry['identity']['executable']} "
            "-S -s -B -P -u -m chess_engine.stockconda\n"
        ).encode("utf-8")
        if launcher.read_bytes() != expected_script:
            raise RuntimeError("engine launcher does not match manifest root/interpreter: " + label)
    return manifest


def load_match_auditor() -> Any:
    source = verified_match_auditor_source()
    helper_source = verified_auditor_source(UCI_AUDITOR, EXPECTED_UCI_AUDITOR, "UCI")
    helper = ModuleType("ap04_verified_uci_audit")
    helper.__file__ = str(UCI_AUDITOR)
    exec(compile_auditor(helper_source, UCI_AUDITOR), helper.__dict__)

    def verified_import(
        name: str, globals: Any = None, locals: Any = None,
        fromlist: Any = (), level: int = 0,
    ) -> Any:
        if name == "ap03_uci_audit" and level == 0:
            return helper
        return builtins.__import__(name, globals, locals, fromlist, level)

    spec = util.spec_from_file_location("ap04_match_audit", MATCH_AUDITOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load match auditor")
    module: Any = util.module_from_spec(spec)
    # Bind this import locally, without sys.path/sys.modules lookup or mutation.
    module.__dict__["__builtins__"] = dict(vars(builtins), __import__=verified_import)
    # Execute exactly the canonical bytes verified, never a second read or pyc.
    exec(compile_auditor(source, MATCH_AUDITOR), module.__dict__)
    module.NAMES = NAMES
    return module


def audit_match(output: Path, games: int, concurrency: int, elo: bool) -> dict[str, Any]:
    module = load_match_auditor()
    return dict(module.audit_match(output, games, "10+0.1", concurrency, elo=elo))


def run_stage(stage: str, games: int, concurrency: int, timeout: int, elo: bool) -> str:
    predecessor_audit = validate_skipped_stages(stage)
    manifest = verify_identity()
    output = RESULTS / ("ap04_" + stage + "_1")
    output.mkdir(exist_ok=False)
    template = json.loads(TEMPLATE.read_text())
    config = input_config(template, output, "10+0.1")
    config_path = output / "input.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    command = match_command(output, games, concurrency)
    report: dict[str, Any] = {
        "status": "RUNNING", "stage": stage, "games": games, "tc": "10+0.1",
        "concurrency": concurrency, "command": command, "manifest_sha256": sha(MANIFEST),
        "performance_sha256": sha(PERFORMANCE), "runner_sha256": sha(Path(__file__)),
        "performance_audit_sha256": sha(PERFORMANCE_AUDIT),
        "auditor_sha256": EXPECTED_MATCH_AUDITOR,
        "uci_auditor_sha256": EXPECTED_UCI_AUDITOR,
        "auditor_hash_mode": AUDITOR_HASH_MODE,
        "runtime": RUNTIME_POLICY.runtime_identity(),
        "input_sha256": sha(config_path), "launcher_sha256": {
            NAMES[0]: sha(CANDIDATE_LAUNCHER), NAMES[1]: sha(BASELINE_LAUNCHER)},
        "engine_commits": {name: manifest["engines"][label]["commit"]
                           for name, label in zip(NAMES, ("candidate", "baseline"))},
        "predecessor_audit": predecessor_audit,
        "outer_limit_seconds": timeout, "started_epoch": time.time(),
    }

    def save() -> None:
        temporary = output / "run.tmp"
        temporary.write_text(json.dumps(report, indent=2))
        temporary.replace(output / "run.json")

    save()
    with (output / "console.log").open("x") as console:
        process = subprocess.Popen(
            command,
            cwd=output,
            env=RUNTIME_POLICY.validated_environment(),
            stdout=console,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        report["pid"] = process.pid
        save()
        try:
            code = process.wait(timeout=timeout)
            report.update(exit_code=code, status="NEEDS_AUDIT" if code == 0 else "FAIL")
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            report.update(status="FAIL", reason="outer timeout; forced cleanup")
        finally:
            report["ended_epoch"] = time.time()
            save()
    if report["status"] != "NEEDS_AUDIT":
        return "FAIL"
    try:
        verify_identity()
    except Exception as error:
        report.update(status="FAIL", reason="identity after stage: " + repr(error))
        save()
        return "FAIL"
    try:
        audited = audit_match(output, games, concurrency, elo)
    except Exception as error:
        report.update(status="FAIL", reason="audit: " + repr(error))
        save()
        return "FAIL"
    try:
        verify_identity()
    except Exception as error:
        report.update(status="FAIL", reason="identity after audit: " + repr(error))
        save()
        return "FAIL"
    return str(audited["status"])


def stages_from(stage: str) -> tuple[tuple[str, int, int, int, bool], ...]:
    index = next(index for index, item in enumerate(STAGES) if item[0] == stage)
    return STAGES[index:]


def _validate_stage_prefix(
    count: int,
    stage_audit_sha256: dict[str, str],
) -> dict[str, str] | None:
    manifest = verify_identity()
    expected_artifacts = {
        "run.json",
        "protocol.log",
        "games.pgn",
        "console.log",
        "autosave.json",
        "input.json",
    }
    predecessor_audit: dict[str, str] | None = None
    for stage, games, concurrency, _timeout, _elo in STAGES[:count]:
        try:
            output = RESULTS / ("ap04_" + stage + "_1")
            run_path = output / "run.json"
            audit_path = output / "audit.json"
            run = json.loads(run_path.read_text())
            audit = json.loads(audit_path.read_text())
            if (
                stage not in stage_audit_sha256
                or sha(audit_path) != stage_audit_sha256[stage]
            ):
                raise RuntimeError("terminal audit identity mismatch")
            expected_run = {
                "status": "NEEDS_AUDIT",
                "stage": stage,
                "games": games,
                "concurrency": concurrency,
                "exit_code": 0,
                "manifest_sha256": EXPECTED_MANIFEST,
                "performance_sha256": EXPECTED_PERFORMANCE,
                "performance_audit_sha256": EXPECTED_PERFORMANCE_AUDIT,
                "auditor_sha256": EXPECTED_MATCH_AUDITOR,
                "uci_auditor_sha256": EXPECTED_UCI_AUDITOR,
                "runner_sha256": manifest["match_runner_sha256"],
                "runtime": RUNTIME_POLICY.runtime_identity(),
                "launcher_sha256": {
                    NAMES[0]: EXPECTED_LAUNCHER_SHA256["candidate"],
                    NAMES[1]: EXPECTED_LAUNCHER_SHA256["baseline"],
                },
                "engine_commits": {
                    name: manifest["engines"][label]["commit"]
                    for name, label in zip(
                        NAMES, ("candidate", "baseline")
                    )
                },
                "predecessor_audit": predecessor_audit,
            }
            if any(run.get(key) != value for key, value in expected_run.items()):
                raise RuntimeError("run identity or outcome mismatch")
            artifact_hashes = audit.get("sha256")
            if (
                audit.get("status") != "PASS"
                or audit.get("games") != games
                or not isinstance(artifact_hashes, dict)
                or set(artifact_hashes) != expected_artifacts
            ):
                raise RuntimeError("terminal audit is not a complete PASS")
            for name, expected_sha256 in artifact_hashes.items():
                artifact = output / name
                if not artifact.is_file() or sha(artifact) != expected_sha256:
                    raise RuntimeError("audited artifact changed: " + name)
            predecessor_audit = {
                "stage": stage,
                "sha256": stage_audit_sha256[stage],
            }
        except Exception as error:
            raise RuntimeError(
                f"skipped {stage} evidence is not admitted: {error}"
            ) from error
    return predecessor_audit


def validate_skipped_stages(start: str) -> dict[str, str] | None:
    """Admit a stage only after immutable prior-stage PASS evidence."""
    index = next(index for index, item in enumerate(STAGES) if item[0] == start)
    return _validate_stage_prefix(index, EXPECTED_STAGE_AUDIT_SHA256)


def admit_completed_stage(stage: str) -> str:
    """Verify one completed stage and admit its audit for the next stage."""
    index = next(index for index, item in enumerate(STAGES) if item[0] == stage)
    audit_path = RESULTS / ("ap04_" + stage + "_1") / "audit.json"
    digest = sha(audit_path)
    stage_hashes = dict(EXPECTED_STAGE_AUDIT_SHA256)
    stage_hashes[stage] = digest
    _validate_stage_prefix(index + 1, stage_hashes)
    EXPECTED_STAGE_AUDIT_SHA256[stage] = digest
    return digest


def main() -> int:
    RUNTIME_POLICY.require_controlled_startup()
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", choices=[item[0] for item in STAGES], default="preflight")
    parser.add_argument("--admission", type=Path)
    parser.add_argument("--admission-sha256")
    args = parser.parse_args()
    if (args.admission is None) != (args.admission_sha256 is None):
        parser.error("--admission and --admission-sha256 are required together")
    if args.admission is not None:
        configure_admission(args.admission, args.admission_sha256)
    for stage, games, concurrency, timeout, elo in stages_from(args.start):
        validate_skipped_stages(stage)
        print(f"Starting {stage}: {games} games", flush=True)
        status = run_stage(stage, games, concurrency, timeout, elo)
        print(f"{stage}: {status}", flush=True)
        if status == "PASS":
            admit_completed_stage(stage)
        if status != "PASS":
            return 3 if status == "HOLD" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
