"""Locate the source package's external assets for developer commands."""
from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    """Find an EchoPath checkout/extracted package, independently of the caller's cwd.

    A wheel does not contain the external bridges or local credentials. Its
    commands can use ECHOPATH_PROJECT_ROOT or run from a source package instead.
    Never default to writing runtime data beside an installed Python module.
    """
    def is_project(path: Path) -> bool:
        return (path / "echopath.config.json").is_file() and (path / "bridges").is_dir()

    override = os.environ.get("ECHOPATH_PROJECT_ROOT")
    if override:
        root = Path(override).expanduser().resolve()
        if not is_project(root):
            raise ValueError("ECHOPATH_PROJECT_ROOT must contain echopath.config.json and bridges/.")
        return root

    source_root = Path(__file__).resolve().parents[2]
    if is_project(source_root):
        return source_root
    cwd = Path.cwd().resolve()
    for root in (cwd, *cwd.parents):
        if is_project(root):
            return root
    raise ValueError(
        "Cannot locate the EchoPath source package. Set ECHOPATH_PROJECT_ROOT "
        "to the checkout or extracted folder containing echopath.config.json and bridges/."
    )
