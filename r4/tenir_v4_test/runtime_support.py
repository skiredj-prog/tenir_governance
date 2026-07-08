from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4


def project_root(anchor: str | Path | None = None) -> Path:
    if anchor is None:
        return Path(__file__).resolve().parents[1]
    path = Path(anchor).resolve()
    if path.is_dir():
        return path
    if path.parent.name in {"tenir_v4_test", "tests", "tools", "examples"}:
        return path.parent.parent
    return path.parent


def runtime_root(anchor: str | Path | None = None) -> Path:
    configured = os.environ.get("TENIR_V4_RUNTIME_ROOT")
    root = Path(configured).expanduser() if configured else project_root(anchor) / ".tenir_v4_runtime"
    root.mkdir(parents=True, exist_ok=True)
    return root


def runtime_artifact_path(relative_path: str, *, anchor: str | Path | None = None) -> Path:
    path = runtime_root(anchor) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def fresh_runtime_artifact_path(relative_path: str, *, anchor: str | Path | None = None) -> Path:
    path = runtime_artifact_path(relative_path, anchor=anchor)
    if not path.exists():
        return path
    if path.is_file():
        try:
            path.unlink()
            return path
        except PermissionError:
            pass
    return path.with_name(f"{path.stem}-{uuid4().hex[:8]}{path.suffix}")


@contextmanager
def managed_tempdir(
    *,
    anchor: str | Path | None = None,
    prefix: str = "tenir-v4-",
) -> Iterator[Path]:
    tmp_root = runtime_root(anchor) / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    path = tmp_root / f"{prefix}{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
