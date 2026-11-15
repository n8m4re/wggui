#!/usr/bin/env python3
"""Check that the runtime dependencies are licensed compatibly with GPL-3.0-or-later.

Reads the installed package metadata (License-Expression, License, classifiers) of the runtime dependencies
from pyproject.toml plus PySide6. Exit code 1 if a dependency is missing a known compatible license.

  python3 scripts/check_licenses.py         # table + verdict
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Licenses compatible with distributing this project under GPL-3.0-or-later (FSF license list:
# gnu.org/licenses/license-list.html – "GPL-compatible free software licenses").
COMPATIBLE = {"MIT", "MIT-0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Apache-2.0", "PSF-2.0", "LGPL-2.1-or-later",
              "LGPL-3.0-only", "LGPL-3.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later", "MPL-2.0", "Zlib"}
CLASSIFIER_MAP = {"BSD License": "BSD-3-Clause", "MIT License": "MIT", "Apache Software License": "Apache-2.0",
                  "ISC License (ISCL)": "ISC", "Python Software Foundation License": "PSF-2.0"}
SPDX_EXPRESSION = re.compile(r"[A-Za-z0-9.+-]+(?:\s+(?:OR|AND|WITH)\s+[A-Za-z0-9.+-]+)*")
# PySide6 is either the distribution package (PySide6) or the PyPI wheel (PySide6-Essentials)
QT_PACKAGES = (("PySide6", "PySide6-Essentials"), ("shiboken6",))


def runtime_dependencies() -> list[tuple[str, ...]]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = [re.split(r"[<>=!~ \[;]", d, maxsplit=1)[0] for d in project.get("dependencies", [])]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {p["name"]: p for p in lock["package"]}
    seen: list[str] = []
    todo = list(names)
    while todo:  # dependencies of dependencies (e.g. cffi, pycparser)
        name = todo.pop(0)
        if name in seen:
            continue
        seen.append(name)
        todo += [d["name"] for d in packages.get(name, {}).get("dependencies", [])]
    return [(n,) for n in seen] + list(QT_PACKAGES)


def licenses(alternatives: tuple[str, ...]) -> tuple[str, set[str]]:
    for name in alternatives:
        try:
            meta = md.metadata(name)
        except md.PackageNotFoundError:
            continue
        # License-Expression (PEP 639) or, in older metadata, an SPDX expression in the License field
        expression = meta.get("License-Expression") or ""
        legacy = (meta.get("License") or "").strip().splitlines()
        if not expression and legacy and SPDX_EXPRESSION.fullmatch(legacy[0].strip()):
            expression = legacy[0].strip()
        found = {t for t in re.split(r"\s+(?:OR|AND|WITH)\s+|[()]", expression) if t.strip()}
        for classifier in meta.get_all("Classifier") or []:
            if classifier.startswith("License :: OSI Approved :: "):
                found.add(CLASSIFIER_MAP.get(classifier.rsplit(" :: ", 1)[1], classifier.rsplit(" :: ", 1)[1]))
        if not found and legacy:
            found.add(legacy[0].strip())
        return name, {f.strip() for f in found}
    return alternatives[0], set()


def main() -> int:
    problems = []
    for alternatives in runtime_dependencies():
        name, found = licenses(alternatives)
        ok = bool(found & COMPATIBLE)
        print(f"{name:20} {' OR '.join(sorted(found)) or '(not installed)':45} {'ok' if ok else 'CHECK'}")
        if not found:
            print(f"  note: {name} not installed here – install the dependencies first", file=sys.stderr)
        elif not ok:
            problems.append(name)
    if problems:
        print(f"Not verified as GPL-3.0-compatible: {', '.join(problems)}")
        return 1
    print("All runtime dependencies are GPL-3.0-compatible.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
