#!/usr/bin/env python3
"""Maintain language catalogs (src/wggui/locale/<language>.po).

  python3 scripts/i18n.py extract   # collect _()/N_()/ngettext() strings and update catalogs
  python3 scripts/i18n.py check     # verify translations and {…} placeholders (exit 1 on errors)
  python3 scripts/i18n.py missing <sprache>   # export missing translations as JSON
  python3 scripts/i18n.py fill <sprache> <json>  # import translations from JSON {"msgid": "msgstr"}

Source strings are German (see src/wggui/i18n.py). Format: GNU gettext PO ("The Format of PO Files").
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "wggui"
LOCALE = SRC / "locale"
TARGETS = ("en", "es", "it", "zh", "ja")
PLURAL_FORMS = {"zh": "nplurals=1; plural=0;", "ja": "nplurals=1; plural=0;"}
PLACEHOLDER = re.compile(r"\{[^{}]*\}")

sys.path.insert(0, str(ROOT / "src"))
from wggui.i18n import parse_po  # noqa: E402


def extract() -> dict[str, tuple[str | None, list[str]]]:
    """msgid → (msgid_plural, source locations) in encounter order."""
    found: dict[str, tuple[str | None, list[str]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "i18n.py":  # this module only forwards strings
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            name, args = node.func.id, node.args
            where = f"{path.relative_to(ROOT)}:{node.lineno}"
            if name in ("_", "N_") and args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                found.setdefault(args[0].value, (None, []))[1].append(where)
            elif (name == "ngettext" and len(args) >= 2 and all(isinstance(a, ast.Constant) for a in args[:2])):
                found.setdefault(args[0].value, (args[1].value, []))[1].append(where)
            elif name in ("N_", "ngettext") or (name == "_" and args and isinstance(args[0], ast.JoinedStr)):
                # _(variable) is allowed (constant marked with N_); no f-strings – use .format() placeholders
                raise SystemExit(f"{where}: {name}() braucht einen festen Text (kein f-String/Variable)")
    return dict(sorted(found.items(), key=lambda kv: kv[1][1][0]))


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


def write_po(lang: str, messages: dict, singles: dict, plurals: dict) -> None:
    forms = PLURAL_FORMS.get(lang, "nplurals=2; plural=(n != 1);")
    out = ['msgid ""', 'msgstr ""', f'"Language: {lang}\\n"', '"MIME-Version: 1.0\\n"',
           '"Content-Type: text/plain; charset=UTF-8\\n"', f'"Plural-Forms: {forms}\\n"', ""]
    for msgid, (plural, places) in messages.items():
        out.append("#: " + " ".join(places[:3]))
        out.append(f"msgid {_quote(msgid)}")
        if plural is not None:
            out.append(f"msgid_plural {_quote(plural)}")
            have = plurals.get(msgid, [])
            n = 1 if lang in PLURAL_FORMS else 2
            for i in range(n):
                out.append(f"msgstr[{i}] {_quote(have[i] if i < len(have) else '')}")
        else:
            out.append(f"msgstr {_quote(singles.get(msgid, ''))}")
        out.append("")
    (LOCALE / f"{lang}.po").write_text("\n".join(out), encoding="utf-8")


def load(lang: str) -> tuple[dict, dict]:
    path = LOCALE / f"{lang}.po"
    return parse_po(path.read_text(encoding="utf-8")) if path.exists() else ({}, {})


def cmd_extract() -> int:
    messages = extract()
    LOCALE.mkdir(exist_ok=True)
    for lang in TARGETS:
        singles, plurals = load(lang)
        write_po(lang, messages, singles, plurals)
    print(f"{len(messages)} Texte, Kataloge: {', '.join(TARGETS)}")
    return 0


def cmd_check() -> int:
    messages = extract()
    problems = []
    for lang in TARGETS:
        singles, plurals = load(lang)
        for msgid, (plural, _places) in messages.items():
            want = set(PLACEHOLDER.findall(msgid)) | set(PLACEHOLDER.findall(plural or ""))
            got = plurals.get(msgid) if plural is not None else ([singles[msgid]] if msgid in singles else None)
            if not got or not all(got):
                problems.append(f"{lang}: fehlt: {msgid[:70]!r}")
                continue
            for text in got:
                have = set(PLACEHOLDER.findall(text))
                if have - want or (plural is None and have != want):
                    problems.append(f"{lang}: Platzhalter {sorted(have)} statt {sorted(want)}: {msgid[:60]!r}")
    for p in problems[:50]:
        print(p)
    print("Kataloge vollständig" if not problems else f"{len(problems)} Problem(e)")
    return 1 if problems else 0


def cmd_missing(lang: str) -> int:
    singles, plurals = load(lang)
    todo = {}
    for msgid, (plural, _places) in extract().items():
        if plural is None and not singles.get(msgid):
            todo[msgid] = ""
        elif plural is not None and not (plurals.get(msgid) and all(plurals[msgid])):
            todo[msgid] = {"plural": plural, "forms": 1 if lang in PLURAL_FORMS else 2}
    print(json.dumps(todo, ensure_ascii=False, indent=1))
    return 0


def cmd_fill(lang: str, source: str) -> int:
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    singles, plurals = load(lang)
    for msgid, value in data.items():
        if isinstance(value, list):
            plurals[msgid] = value
        elif value:
            singles[msgid] = value
    write_po(lang, extract(), singles, plurals)
    print(f"{lang}: {len(data)} Übersetzungen eingetragen")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "extract":
        sys.exit(cmd_extract())
    if command == "missing":
        sys.exit(cmd_missing(sys.argv[2]))
    if command == "fill":
        sys.exit(cmd_fill(sys.argv[2], sys.argv[3]))
    sys.exit(cmd_check())
