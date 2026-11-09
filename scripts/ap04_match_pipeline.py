"""Identity-bound AP04 preflight, orientation and release match pipeline."""
from __future__ import annotations

import argparse
import builtins
from decimal import Decimal
import hashlib
from importlib import util
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from types import ModuleType
from typing import Any, cast


RESULTS = Path("/results")
MANIFEST = RESULTS / "ap04_bits_identity_1/manifest.json"
PERFORMANCE = RESULTS / "ap04_bits_performance_1/result.json"
PERFORMANCE_AUDIT = RESULTS / "ap04_bits_performance_1/independent-audit.json"
MATCH_AUDITOR = Path(__file__).with_name("ap03_match_audit.py")
UCI_AUDITOR = Path(__file__).with_name("ap03_uci_audit.py")
TEMPLATE = RESULTS / "ap03_preflight1/autosave.json"
FASTCHESS = Path("/reference/tools/fastchess/fastchess")
BOOK = Path("/reference/books/8moves_v3.pgn")
CANDIDATE_LAUNCHER = Path("/build/ap04_flat_gate_candidate_2/ap04-launch.sh")
BASELINE_LAUNCHER = Path("/build/ap04_flat_gate_baseline_1/ap04-launch.sh")
NAMES = ["AP04_TTFirst_741b05c", "AP04_Baseline_106f500"]
STAGES = (
    ("preflight", 4, 1, 600, False),
    ("orientation", 200, 4, 3600, False),
    ("release", 2000, 4, 43200, True),
)
EXPECTED_MANIFEST = "6116e06a80edac628ada494b9ed8cd12bd742672eb56c11f39e3ca0987ae591e"
EXPECTED_PERFORMANCE = "ec5903fae6443de66dd9321aa7e7d274df95103c284d1cb4db06bb6adfd049ba"
EXPECTED_PERFORMANCE_AUDIT = "c0f5ba53243673d13dfb8531d8cfb2dc73c89aa686d8c5c3c92cb0e2335a7b12"
AUDITOR_HASH_MODE = "sha256-crlf-to-lf-v1"
EXPECTED_MATCH_AUDITOR = "60ca7cfb77da90ec5f3f7b4a966d7db9164358b0f354e74108b787941be87273"
EXPECTED_UCI_AUDITOR = "3c414be028f299e5b49f08706eb1f25539589308fee805d4b7b8a248678e8a2f"
EXPECTED_LAUNCHER_SHA256 = {
    "candidate": "e8a6709a3b708919aa5537ccc0943e604892cc089d5b462f9ff9adef5c5c1e89",
    "baseline": "1e8ab376851c1e6a48111e1530d0b155daed2f1d5d142f434f1c25cd70c59dca",
}
EXPECTED_FASTCHESS = "4277d081b9478366d70934ad638a85958c5f006aa3cc73f3ce87ec251dbd97a7"
EXPECTED_BOOK = "5835239f88cc2c7511b177c32392a69f3ede21819cf0616f80a7f907cd21d17e"


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
    verified_match_auditor_source()
    verified_auditor_source(UCI_AUDITOR, EXPECTED_UCI_AUDITOR, "UCI")
    if (sha(MANIFEST) != EXPECTED_MANIFEST or sha(PERFORMANCE) != EXPECTED_PERFORMANCE
            or sha(PERFORMANCE_AUDIT) != EXPECTED_PERFORMANCE_AUDIT):
        raise RuntimeError("frozen AP04 evidence identity changed")
    manifest = cast(dict[str, Any], json.loads(MANIFEST.read_text()))
    performance = json.loads(PERFORMANCE.read_text())
    audit = json.loads(PERFORMANCE_AUDIT.read_text())
    if (performance["status"] != "PASS" or audit["audit"] != "PASS"
            or audit["performance_verdict"] != "PASS"):
        raise RuntimeError("AP04 performance gate is not admitted")
    if (performance["manifest_sha256"] != EXPECTED_MANIFEST
            or audit["result_sha256"] != EXPECTED_PERFORMANCE
            or audit["raw_sha256"] != performance["raw_sha256"]
            or audit["parity_sha256"] != performance["parity_sha256"]
            or audit["engine_identities"] != {
                label: entry["identity"] for label, entry in manifest["engines"].items()
            }):
        raise RuntimeError("performance audit is not bound to the admitted evidence")
    for entry in manifest["engines"].values():
        root = Path(entry["root"])
        if sha(Path(entry["archive"])) != entry["archive_sha256"]:
            raise RuntimeError("engine archive changed")
        for relative, digest in entry["source"].items():
            if sha(root / relative) != digest:
                raise RuntimeError("engine source changed: " + relative)
        for imported in entry["identity"]["imported"].values():
            if sha(Path(imported["path"])) != imported["sha256"]:
                raise RuntimeError("imported engine artifact changed")
    if sha(FASTCHESS) != EXPECTED_FASTCHESS or sha(BOOK) != EXPECTED_BOOK:
        raise RuntimeError("Fastchess or opening book identity changed")
    for label, launcher in (("candidate", CANDIDATE_LAUNCHER), ("baseline", BASELINE_LAUNCHER)):
        if not launcher.is_file() or sha(launcher) != EXPECTED_LAUNCHER_SHA256[label]:
            raise RuntimeError("engine launcher identity changed: " + label)
        entry = manifest["engines"][label]
        expected_script = (
            f"#!/bin/sh\nexport PYTHONPATH={entry['root']}/src\n"
            f"exec {entry['identity']['executable']} -u -m chess_engine.stockconda\n"
        ).encode("utf-8")
        if launcher.read_bytes() != expected_script:
            raise RuntimeError("engine launcher does not match manifest root/interpreter: " + label)
    return manifest


def load_match_auditor() -> Any:
    source = verified_match_auditor_source()
    helper_source = verified_auditor_source(UCI_AUDITOR, EXPECTED_UCI_AUDITOR, "UCI")
    helper = ModuleType("ap04_verified_uci_audit")
    helper.__file__ = str(UCI_AUDITOR)
    exec(compile(helper_source, str(UCI_AUDITOR), "exec"), helper.__dict__)

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
    exec(compile(source, str(MATCH_AUDITOR), "exec"), module.__dict__)
    module.NAMES = NAMES
    return module


def audit_match(output: Path, games: int, concurrency: int, elo: bool) -> dict[str, Any]:
    module = load_match_auditor()
    return dict(module.audit_match(output, games, "10+0.1", concurrency, elo=elo))


def run_stage(stage: str, games: int, concurrency: int, timeout: int, elo: bool) -> str:
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
        "input_sha256": sha(config_path), "launcher_sha256": {
            NAMES[0]: sha(CANDIDATE_LAUNCHER), NAMES[1]: sha(BASELINE_LAUNCHER)},
        "engine_commits": {name: manifest["engines"][label]["commit"]
                           for name, label in zip(NAMES, ("candidate", "baseline"))},
        "outer_limit_seconds": timeout, "started_epoch": time.time(),
    }

    def save() -> None:
        temporary = output / "run.tmp"
        temporary.write_text(json.dumps(report, indent=2))
        temporary.replace(output / "run.json")

    save()
    with (output / "console.log").open("x") as console:
        process = subprocess.Popen(command, cwd=output, stdout=console,
                                   stderr=subprocess.STDOUT, start_new_session=True)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", choices=[item[0] for item in STAGES], default="preflight")
    args = parser.parse_args()
    for stage, games, concurrency, timeout, elo in stages_from(args.start):
        print(f"Starting {stage}: {games} games", flush=True)
        status = run_stage(stage, games, concurrency, timeout, elo)
        print(f"{stage}: {status}", flush=True)
        if status != "PASS":
            return 3 if status == "HOLD" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
