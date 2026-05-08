"""Cross-platform discovery of the user's local ProPresenter folder.

ProPresenter stores its libraries and playlists under a `~/Documents/
ProPresenter` (or platform-specific) directory. We find it so the user
doesn't have to type a path; the app falls back to the canonical Mac
default when nothing exists yet."""

import sys
from pathlib import Path


def _pp_candidates():
    if sys.platform == "darwin":
        return [
            Path.home() / "Documents" / "ProPresenter",
            Path.home() / "ProPresenter",
            Path("/Users/Shared/ProPresenter"),
        ]
    return [
        Path.home() / "Documents" / "ProPresenter",
        Path("C:/Users/Public/Documents/ProPresenter"),
        Path.home() / "Documents" / "RenewedVision" / "ProPresenter",
    ]


def find_pp_root() -> str:
    for p in _pp_candidates():
        if p.exists():
            return str(p)
    return str(Path.home() / "Documents" / "ProPresenter")


def find_library_dirs(pp_root: str) -> list:
    lib = Path(pp_root) / "Libraries"
    if not lib.exists():
        return []
    return [str(d) for d in sorted(lib.iterdir()) if d.is_dir()]


def find_playlist_dir(pp_root: str):
    p = Path(pp_root) / "Playlists"
    return str(p) if p.exists() else None
