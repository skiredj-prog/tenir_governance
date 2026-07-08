"""
TENIR Copy-Lint Tool
====================
SPRINT 9 — Public-Safe Language Enforcement

Scans markdown, HTML, and plain-text files for PUBLIC_BANNED_TERMS and
warns about terms in PUBLIC_TRANSLATE_ON_FIRST_USE that appear without
being translated on first use in a document tagged exposure=PUBLIC.

Used as a non-blocking CI step — findings block merge if the file has
an `exposure: public` front-matter tag, otherwise they're advisory.

Usage:
    python -m tenir_governance.copy_lint path/to/docs/
    python -m tenir_governance.copy_lint --exposure public homepage.html
    python -m tenir_governance.copy_lint --strict --json docs/*.md

Exit codes:
    0 — no findings (or all advisory)
    1 — at least one blocking finding
    2 — file access / parse error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from .nomenclature import (
    PUBLIC_BANNED_TERMS,
    PUBLIC_TRANSLATE_ON_FIRST_USE,
    CAVEFieldNames,
)


# ─── DATA TYPES ──────────────────────────────────────────────────────────────

@dataclass
class Finding:
    file: str
    line: int
    column: int
    term: str
    category: str        # "banned" | "untranslated" | "deprecated_cave"
    severity: str        # "BLOCK" | "WARN" | "INFO"
    message: str
    exposure: str        # the exposure class of the file
    snippet: str = ""


@dataclass
class LintReport:
    findings: List[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(f.severity == "BLOCK" for f in self.findings)

    @property
    def block_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "BLOCK")

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "WARN")

    def summary(self) -> str:
        if not self.findings:
            return "TENIR Copy-Lint — CLEAN (no findings)"

        lines = [
            f"TENIR Copy-Lint — {'BLOCKING' if self.blocking else 'ADVISORY'}",
            f"  Findings: {len(self.findings)}  |  BLOCK: {self.block_count}  |  WARN: {self.warn_count}",
            "",
        ]
        by_file: Dict[str, List[Finding]] = {}
        for f in self.findings:
            by_file.setdefault(f.file, []).append(f)
        for filepath, file_findings in sorted(by_file.items()):
            lines.append(f"  {filepath}  [exposure={file_findings[0].exposure}]")
            for f in file_findings:
                icon = "✗" if f.severity == "BLOCK" else "⚠"
                lines.append(f"    {icon} line {f.line}:{f.column}  [{f.category}] {f.message}")
                if f.snippet:
                    lines.append(f"        → {f.snippet[:120]}")
            lines.append("")
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "blocking":    self.blocking,
            "block_count": self.block_count,
            "warn_count":  self.warn_count,
            "findings":    [asdict(f) for f in self.findings],
        }


# ─── EXPOSURE DETECTION ──────────────────────────────────────────────────────
# Files declare their exposure via front-matter tag or HTML meta:
#   <!-- exposure: public -->
#   ---
#   exposure: public
#   ---

_FRONT_MATTER_RE = re.compile(
    r"(?:^---\s*\n(.*?)\n---\s*\n)|(?:<!--\s*(exposure:\s*\w+)\s*-->)",
    re.DOTALL | re.MULTILINE,
)
_EXPOSURE_VALUE_RE = re.compile(r"exposure\s*:\s*(\w+)", re.IGNORECASE)


def detect_exposure(content: str, default: str = "operator") -> str:
    m = _FRONT_MATTER_RE.search(content[:1500])
    if m:
        body = m.group(1) or m.group(2) or ""
        v = _EXPOSURE_VALUE_RE.search(body)
        if v:
            return v.group(1).lower()
    return default


# ─── LINTER ──────────────────────────────────────────────────────────────────

class CopyLinter:
    """Scan documents for violations of the public-safe lexicon."""

    def __init__(
        self,
        strict: bool = False,
        override_exposure: Optional[str] = None,
    ) -> None:
        self.strict = strict
        self.override_exposure = override_exposure
        self._report = LintReport()

    def lint_file(self, path: Path) -> LintReport:
        self._report = LintReport()
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._report.findings.append(Finding(
                file=str(path), line=0, column=0,
                term="<file>", category="io_error", severity="BLOCK",
                message=f"Cannot read file: {e}",
                exposure="unknown",
            ))
            return self._report

        exposure = self.override_exposure or detect_exposure(content)
        self._scan_content(path, content, exposure)
        return self._report

    def lint_paths(self, paths: List[Path]) -> LintReport:
        combined = LintReport()
        for p in paths:
            if p.is_dir():
                for sub in sorted(p.rglob("*")):
                    if sub.is_file() and sub.suffix.lower() in {".md", ".html", ".txt"}:
                        r = self.lint_file(sub)
                        combined.findings.extend(r.findings)
            elif p.is_file():
                r = self.lint_file(p)
                combined.findings.extend(r.findings)
        self._report = combined
        return combined

    def _scan_content(self, path: Path, content: str, exposure: str) -> None:
        lines = content.splitlines()
        first_mentions: Dict[str, int] = {}

        for i, line in enumerate(lines, 1):
            line_lower = line.lower()

            # ── Check 1: banned terms ─────────────────────────────────────────
            for term in PUBLIC_BANNED_TERMS:
                term_lower = term.lower()
                pattern = r"\b" + re.escape(term_lower) + r"\b"
                for m in re.finditer(pattern, line_lower):
                    severity = "BLOCK" if exposure == "public" else "WARN"
                    if self.strict:
                        severity = "BLOCK"
                    self._report.findings.append(Finding(
                        file=str(path), line=i, column=m.start() + 1,
                        term=term, category="banned",
                        severity=severity,
                        message=f"Banned term {term!r} appears in exposure={exposure!r} content",
                        exposure=exposure,
                        snippet=line.strip(),
                    ))

            # ── Check 2: untranslated branded terms on first use ──────────────
            if exposure == "public":
                for term in PUBLIC_TRANSLATE_ON_FIRST_USE:
                    term_lower = term.lower()
                    if term_lower in line_lower and term_lower not in first_mentions:
                        first_mentions[term_lower] = i
                        # Check if translation hint appears on same or nearby line
                        window = "\n".join(lines[max(0, i-1):i+2]).lower()
                        has_translation = any(
                            hint in window
                            for hint in ("—", "–", ":", "is", "means", "(")
                        )
                        if not has_translation:
                            self._report.findings.append(Finding(
                                file=str(path), line=i, column=line_lower.find(term_lower) + 1,
                                term=term, category="untranslated",
                                severity="WARN",
                                message=f"Branded term {term!r} appears without translation on first use",
                                exposure=exposure,
                                snippet=line.strip(),
                            ))

            # ── Check 3: deprecated CAVE expansion ────────────────────────────
            for forbidden in CAVEFieldNames.FORBIDDEN_EXPANSIONS:
                if forbidden.lower() in line_lower:
                    self._report.findings.append(Finding(
                        file=str(path), line=i, column=line_lower.find(forbidden.lower()) + 1,
                        term=forbidden, category="deprecated_cave",
                        severity="BLOCK",
                        message=f"Deprecated CAVE expansion {forbidden!r} — use {CAVEFieldNames.EXPANSION!r}",
                        exposure=exposure,
                        snippet=line.strip(),
                    ))


# ─── CLI ENTRY POINT ──────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="TENIR Copy-Lint — enforce public-safe language"
    )
    parser.add_argument("paths", nargs="+", type=Path,
                        help="File(s) or directory(ies) to scan")
    parser.add_argument("--exposure", choices=["public", "operator", "canonical"],
                        help="Override detected exposure class")
    parser.add_argument("--strict", action="store_true",
                        help="Treat all banned terms as BLOCK regardless of exposure")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON report")
    args = parser.parse_args(argv)

    linter = CopyLinter(strict=args.strict, override_exposure=args.exposure)
    report = linter.lint_paths(args.paths)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.summary())

    return 1 if report.blocking else 0


if __name__ == "__main__":
    sys.exit(main())
