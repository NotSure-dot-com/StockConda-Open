"""Build the Windows x64 Arena bundle from an isolated Python 3.12 venv.

Run from a clean source export after installing the project and PyInstaller in
that venv. The output directory must not exist. No global Python installation
or package state is changed by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NATIVE_MODULES = (
    "chess_engine.cython_ext.cython_chess",
    "chess_engine.cython_ext.evaluation",
    "chess_engine.cython_ext.transposition",
)
BUILD_ONLY = {"cython", "setuptools", "wheel", "pip"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_source_zip(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        manifest_name = next(
            (name for name in names if name.endswith("/RELEASE_EXPORT_MANIFEST.json")),
            None,
        )
        if manifest_name is None:
            raise SystemExit("source ZIP lacks release export manifest")
        manifest = json.loads(archive.read(manifest_name))
        prefix = manifest_name.removesuffix("RELEASE_EXPORT_MANIFEST.json")
        listed = {entry["path"] for entry in manifest["files"]}
        required = {
            "LICENSE", "THIRD_PARTY_NOTICES.md", "opening_book.pkl",
            "pyproject.toml", "setup.py", "scripts/build_windows_onedir.py",
            "scripts/stockconda_frozen.py", "src/chess_engine/stockconda.py",
            "src/chess_engine/cython_ext/cython_chess.pyx",
            "src/chess_engine/cython_ext/evaluation.pyx",
            "src/chess_engine/cython_ext/transposition.pyx",
        }
        if required - listed or {prefix + name for name in required} - names:
            raise SystemExit("source ZIP lacks required build or license files")
        expected = {manifest_name} | {prefix + entry["path"] for entry in manifest["files"]}
        if names != expected:
            raise SystemExit("source ZIP and release manifest differ")
        for entry in manifest["files"]:
            data = archive.read(prefix + entry["path"])
            if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise SystemExit("source ZIP file differs from release manifest: " + entry["path"])
        return manifest


def check_local_source(manifest: dict[str, object]) -> None:
    local_manifest_path = ROOT / "RELEASE_EXPORT_MANIFEST.json"
    if not local_manifest_path.is_file():
        raise SystemExit("build from the matching history-free source export")
    if json.loads(local_manifest_path.read_text(encoding="utf-8")) != manifest:
        raise SystemExit("local source manifest differs from source ZIP")
    for entry in manifest["files"]:
        path = ROOT / entry["path"]
        if not path.is_file() or path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
            raise SystemExit("local source differs from source ZIP: " + entry["path"])


def check_native_modules() -> None:
    for name in NATIVE_MODULES:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        if path.suffix.lower() != ".pyd" or not path.is_relative_to(ROOT):
            raise SystemExit(f"Windows native module not built in this source tree: {name}: {path}")


def copy_licenses(destination: Path, msvc_notices: Path) -> list[dict[str, object]]:
    destination.mkdir()
    shutil.copy2(ROOT / "LICENSE", destination / "StockConda-GPL-3.0.txt")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", destination / "THIRD_PARTY_NOTICES.md")
    shutil.copytree(ROOT / "third_party" / "licenses", destination / "source-third-party")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise SystemExit(f"Python runtime license missing: {python_license}")
    shutil.copy2(python_license, destination / "Python-LICENSE.txt")
    for name in ("Redist.txt", "ThirdPartyNotices.txt"):
        source = msvc_notices / name
        if not source.is_file():
            raise SystemExit(f"Microsoft redistributable notice missing: {source}")
        shutil.copy2(source, destination / ("Microsoft-Visual-Studio-2022-" + name))

    distributions = list(metadata.distributions())
    licensed_installations = {
        (distribution.metadata["Name"].lower(), distribution.version)
        for distribution in distributions
        if any(
            file.name.lower().startswith(("license", "licence", "copying", "notice", "copyright"))
            and distribution.locate_file(file).is_file()
            for file in distribution.files or []
        )
    }
    inventory: list[dict[str, object]] = []
    missing: list[str] = []
    for distribution in sorted(distributions, key=lambda d: d.metadata["Name"].lower()):
        name = distribution.metadata["Name"]
        version = distribution.version
        files = distribution.files or []
        license_files = [
            file for file in files
            if file.name.lower().startswith(("license", "licence", "copying", "notice", "copyright"))
            and distribution.locate_file(file).is_file()
        ]
        if not license_files:
            if (name.lower(), version) in licensed_installations:
                # An editable install can expose both a licensed dist-info and
                # an empty source-tree egg-info for the same distribution.
                continue
            if name.lower() in BUILD_ONLY:
                inventory.append({"name": name, "version": version, "build_only": True,
                                  "license_files": []})
                continue
            missing.append(f"{name}=={version}")
            continue
        copied: list[str] = []
        for index, file in enumerate(license_files, 1):
            target = destination / "installed" / f"{name}-{version}" / f"{index}-{file.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(distribution.locate_file(file), target)
            copied.append(str(target.relative_to(destination)))
        inventory.append({"name": name, "version": version, "license_files": copied})
    (destination / "INSTALLED_DISTRIBUTIONS.json").write_text(
        json.dumps({"distributions": inventory, "missing_licenses": missing}, indent=2) + "\n",
        encoding="utf-8",
    )
    if missing:
        raise SystemExit("license files missing from installed distributions: " + ", ".join(missing))
    return inventory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-zip", type=Path, required=True)
    parser.add_argument("--msvc-notices", type=Path, required=True,
                        help="installed Visual Studio 2022 Licenses/<locale> directory")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12) or struct.calcsize("P") != 8:
        raise SystemExit("build requires Windows x64 and Python 3.12")
    if sys.prefix == sys.base_prefix:
        raise SystemExit("run from an isolated virtual environment")
    source_zip = args.source_zip.resolve(strict=True)
    manifest = check_source_zip(source_zip)
    check_local_source(manifest)
    if not (ROOT / "opening_book.pkl").is_file():
        raise SystemExit("bundled opening book missing")
    check_native_modules()
    importlib.import_module("PyInstaller")
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("output path must not exist")
    output.mkdir(parents=True)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir", "--console", "--noupx", "--name", "StockConda",
        "--paths", str(ROOT / "src"),
        "--distpath", str(output / "dist"),
        "--workpath", str(output / "work"),
        "--specpath", str(output / "spec"),
        str(ROOT / "scripts" / "stockconda_frozen.py"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    package = output / "dist" / "StockConda"
    exe = package / "StockConda.exe"
    if not exe.is_file() or not (package / "_internal").is_dir():
        raise SystemExit("PyInstaller did not produce the expected onedir package")
    shutil.copy2(ROOT / "opening_book.pkl", package / "opening_book.pkl")
    copy_licenses(package / "licenses", args.msvc_notices.resolve(strict=True))
    shutil.copy2(source_zip, package / source_zip.name)
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_windows_onedir.py"),
         str(exe), str(output / "uci-smoke.log")],
        cwd=ROOT, check=True,
    )
    (package / "BUILD_MANIFEST.json").write_text(
        json.dumps({
            "source_commit": manifest["source_commit"],
            "source_zip": source_zip.name,
            "source_zip_sha256": sha256(source_zip),
            "executable_sha256": sha256(exe),
            "book_sha256": sha256(package / "opening_book.pkl"),
            "python": sys.version,
            "pyinstaller": metadata.version("pyinstaller"),
            "build_mode": "PyInstaller console onedir, no UPX",
            "entry_point": "scripts/stockconda_frozen.py",
        }, indent=2) + "\n", encoding="utf-8",
    )
    shutil.make_archive(str(output / "StockConda-Windows-x64-onedir"), "zip", output / "dist")
    print(output / "StockConda-Windows-x64-onedir.zip")


if __name__ == "__main__":
    main()
