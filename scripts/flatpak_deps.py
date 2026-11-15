#!/usr/bin/env python3
"""Build packaging/flatpak/python3-deps.json from uv.lock: runtime wheels with sha256 hashes
for offline, reproducible flatpak-builder builds.

Versions and hashes come from uv.lock (as with `uv sync --frozen`). PySide6 comes from the Base App
(io.qt.PySide.BaseApp). Runtime Python: 3.13 (org.kde.Sdk 6.11 / freedesktop 25.08).

Usage:  python3 scripts/flatpak_deps.py [--check]
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "packaging" / "flatpak" / "python3-deps.json"
PACKAGES = ("cryptography", "cffi", "pycparser", "segno")  # Runtime dependencies excluding PySide6
PY = "313"
ARCHES = {"x86_64": "x86_64", "aarch64": "aarch64"}


def _score(name: str, arch: str) -> int | None:
    """Score a wheel for CPython 3.13 on Linux/arch; higher prefers manylinux 2_28 over 2014."""
    if name.endswith("-py3-none-any.whl"):
        return 1
    if not name.endswith(f"{arch}.whl") or "manylinux" not in name:  # glibc Linux only (no macOS or musl)
        return None
    tags = re.search(r"-(cp\d+)-(abi3|cp\d+t?)-", name)
    if not tags:
        return None
    py, abi = tags.groups()
    if abi == "abi3":
        if int(py[2:]) > int(PY):
            return None
        return 3 if "manylinux_2_28" in name else 2
    if py == f"cp{PY}" and abi == f"cp{PY}":
        return 3
    return None


def pick(package: dict, arch: str) -> dict | None:
    best = None
    for wheel in package.get("wheels", []):
        name = wheel["url"].rsplit("/", 1)[-1]
        score = _score(name, arch)
        if score is not None and (best is None or score > best[0]):
            best = (score, wheel, name)
    return None if best is None else {"url": best[1]["url"], "sha256": best[1]["hash"].removeprefix("sha256:"),
                                      "name": best[2]}


def build() -> dict:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {p["name"]: p for p in lock["package"]}
    sources, names = [], []
    for pkg in PACKAGES:
        entry = packages[pkg]
        names.append(pkg)
        seen = {}
        for arch in ARCHES:
            wheel = pick(entry, arch)
            if wheel is None:
                raise SystemExit(f"Kein passendes Wheel für {pkg} {entry['version']} ({arch})")
            seen.setdefault(wheel["url"], (wheel, []))[1].append(arch)
        for wheel, arches in seen.values():
            source = {"type": "file", "url": wheel["url"], "sha256": wheel["sha256"]}
            if len(arches) < len(ARCHES):
                source["only-arches"] = arches
            sources.append(source)
    return {
        "name": "python3-deps",
        "buildsystem": "simple",
        "build-commands": [
            "pip3 install --verbose --exists-action=i --no-index --find-links=\"file://${PWD}\" "
            "--prefix=${FLATPAK_DEST} --no-build-isolation " + " ".join(names)
        ],
        "sources": sources,
    }


def main() -> int:
    text = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
    if "--check" in sys.argv:
        ok = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("python3-deps.json aktuell" if ok else "python3-deps.json veraltet → scripts/flatpak_deps.py")
        return 0 if ok else 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"geschrieben: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
