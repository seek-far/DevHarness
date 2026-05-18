"""
inspection.loader — build a CodeTarget from a filesystem path.

Phase 1 scope: a single file or a directory of Python files. Junk dirs are
skipped. Kept dependency-free (stdlib only) so the inspector stays
independent of the bug-fix pipeline.
"""

from __future__ import annotations

from pathlib import Path

from inspection.base import CodeFile, CodeTarget

_SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
    "node_modules", ".mypy_cache", ".idea", ".eggs", "build", "dist",
}
_MAX_FILE_CHARS = 40_000  # skip giant generated files; Phase 1 reviews source


def load_target(path: str | Path, description: str = "") -> CodeTarget:
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"path not found: {p}")

    if p.is_file():
        files = [_read(p, p.parent)]
        desc = description or f"file {p.name}"
        return CodeTarget(files=[f for f in files if f], description=desc)

    out: list[CodeFile] = []
    for f in sorted(p.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in f.parts):
            continue
        cf = _read(f, p)
        if cf:
            out.append(cf)
    return CodeTarget(files=out, description=description or f"directory {p.name}")


def _read(f: Path, base: Path) -> CodeFile | None:
    try:
        text = f.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    if len(text) > _MAX_FILE_CHARS:
        return None
    try:
        rel = str(f.relative_to(base))
    except ValueError:
        rel = f.name
    return CodeFile(path=rel, content=text)
