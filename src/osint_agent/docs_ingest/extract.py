"""Document text extraction for the ICE contracting corpus.

Handles PDF, DOCX, XLSX natively; falls back to libreoffice --convert-to txt
for DOC, XLS, PPTX, RTF. Results are cached alongside the source file as
<source>.extracted.txt so re-runs are free.

Public API:
    extract_text(path) -> ExtractResult
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import docx
import openpyxl

logger = logging.getLogger(__name__)

_NATIVE_EXTS = {".pdf", ".docx", ".xlsx"}
_LIBREOFFICE_EXTS = {".doc", ".xls", ".xlsm", ".pptx", ".rtf"}
_PASSTHROUGH_EXTS = {".txt", ".csv"}
_CACHE_SUFFIX = ".extracted.txt"


@dataclass
class ExtractResult:
    """Result of a single-file extraction."""

    source_path: Path
    text: str
    ok: bool
    format: str              # Extension used for dispatch (e.g. "pdf")
    method: str              # How we extracted it (pdftotext, python-docx, libreoffice, cached)
    char_count: int
    unit_count: int          # Pages / sheets / slides / paragraphs (format-dependent)
    error: str | None = None


def _cache_path(source: Path) -> Path:
    return source.with_suffix(source.suffix + _CACHE_SUFFIX)


def _write_cache(source: Path, text: str) -> None:
    try:
        _cache_path(source).write_text(text)
    except OSError as exc:
        logger.warning("Failed to write cache for %s: %s", source, exc)


def _read_cache(source: Path) -> str | None:
    cp = _cache_path(source)
    if not cp.exists():
        return None
    try:
        src_mtime = source.stat().st_mtime
        cache_mtime = cp.stat().st_mtime
        if cache_mtime < src_mtime:
            return None  # stale
        return cp.read_text()
    except OSError:
        return None


def _extract_pdf(path: Path) -> tuple[str, int]:
    """Extract text from a PDF via pdftotext -layout. Returns (text, page_count)."""
    result = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pdftotext exit {result.returncode}: {result.stderr.strip()[:200]}"
        )
    text = result.stdout
    # pdftotext separates pages with \f (form feed). Count those + 1.
    page_count = text.count("\f") + 1 if text else 0
    return text, page_count


def _extract_docx(path: Path) -> tuple[str, int]:
    """Extract paragraphs + table cells from a DOCX. Returns (text, paragraph_count)."""
    doc = docx.Document(str(path))
    parts: list[str] = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            parts.append(t)
    # Table cells — contracting PDFs/DOCX often have pricing tables, CLINs, etc.
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            cells = [c for c in cells if c]
            if cells:
                parts.append("\t".join(cells))
    return "\n".join(parts), len(parts)


def _extract_xlsx(path: Path) -> tuple[str, int]:
    """Extract a TSV-like rendering of each sheet. Returns (text, sheet_count)."""
    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    sections: list[str] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        sections.append(f"\n=== SHEET: {sheet_name} ===")
        for row in ws.iter_rows(values_only=True):
            cells = [
                str(c).strip() if c is not None else ""
                for c in row
            ]
            if any(cells):
                sections.append("\t".join(cells))
    wb.close()
    return "\n".join(sections), len(wb.sheetnames)


def _extract_via_libreoffice(path: Path) -> tuple[str, int]:
    """Convert to .txt via libreoffice --headless. Returns (text, 0)."""
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [
                "libreoffice", "--headless", "--convert-to", "txt",
                "--outdir", tmp, str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"libreoffice exit {result.returncode}: "
                f"{result.stderr.strip()[:200]}"
            )
        out_file = Path(tmp) / (path.stem + ".txt")
        if not out_file.exists():
            # Libreoffice sometimes renames funny chars; pick any .txt
            candidates = list(Path(tmp).glob("*.txt"))
            if not candidates:
                raise RuntimeError("libreoffice produced no .txt output")
            out_file = candidates[0]
        return out_file.read_text(errors="replace"), 0


def extract_text(
    source: str | Path,
    use_cache: bool = True,
) -> ExtractResult:
    """Extract plain text from a corpus file.

    Writes a sibling <source>.extracted.txt cache that invalidates on mtime.
    Returns an ExtractResult; callers should check .ok before using .text.
    """
    path = Path(source)
    ext = path.suffix.lower()

    if use_cache:
        cached = _read_cache(path)
        if cached is not None:
            return ExtractResult(
                source_path=path,
                text=cached,
                ok=True,
                format=ext.lstrip("."),
                method="cached",
                char_count=len(cached),
                unit_count=0,
            )

    try:
        if ext == ".pdf":
            text, units = _extract_pdf(path)
            method = "pdftotext"
        elif ext == ".docx":
            text, units = _extract_docx(path)
            method = "python-docx"
        elif ext == ".xlsx":
            text, units = _extract_xlsx(path)
            method = "openpyxl"
        elif ext in _PASSTHROUGH_EXTS:
            text = path.read_text(errors="replace")
            units = text.count("\n") + 1 if text else 0
            method = "passthrough"
        elif ext in _LIBREOFFICE_EXTS:
            text, units = _extract_via_libreoffice(path)
            method = "libreoffice"
        else:
            return ExtractResult(
                source_path=path,
                text="",
                ok=False,
                format=ext.lstrip("."),
                method="unsupported",
                char_count=0,
                unit_count=0,
                error=f"unsupported extension: {ext}",
            )
    except Exception as exc:
        return ExtractResult(
            source_path=path,
            text="",
            ok=False,
            format=ext.lstrip("."),
            method="failed",
            char_count=0,
            unit_count=0,
            error=f"{type(exc).__name__}: {exc}",
        )

    if use_cache and text:
        _write_cache(path, text)

    return ExtractResult(
        source_path=path,
        text=text,
        ok=True,
        format=ext.lstrip("."),
        method=method,
        char_count=len(text),
        unit_count=units,
    )
