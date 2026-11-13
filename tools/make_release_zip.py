"""Create a deterministic source ZIP from a verified release export directory."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("export", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    export = args.export.resolve(strict=True)
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("output ZIP already exists")
    manifest = json.loads((export / "RELEASE_EXPORT_MANIFEST.json").read_text(encoding="utf-8"))
    selected = {entry["path"] for entry in manifest["files"]}
    actual = {p.relative_to(export).as_posix() for p in export.rglob("*") if p.is_file()}
    if actual != selected | {"RELEASE_EXPORT_MANIFEST.json"}:
        raise SystemExit("source export and manifest differ")
    for entry in manifest["files"]:
        data = (export / entry["path"]).read_bytes()
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise SystemExit("source export file hash mismatch: " + entry["path"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in sorted(actual):
            info = ZipInfo("StockConda-0.2.3.5.dev2/" + relative, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, (export / relative).read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)
    print(output, hashlib.sha256(output.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
