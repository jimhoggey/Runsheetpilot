"""ProPresenter integration — filesystem paths, library scan, fuzzy match,
REST timer creation, and playlist payload assembly.

Submodule responsibilities:
  paths.py     — discover the user's local PP folder + libraries + playlists
  library.py   — scan .pro files for UUIDs; fuzzy-match by title
  timers.py    — duration-based [RB] countdown timers via the REST API
  playlist.py  — assemble the items list for PUT /v1/playlist/{uuid}
"""

from .library import (
    _UUID_RE,
    _norm,
    _uuid_from_binary,
    fuzzy_match,
    resolve_library_name,
    scan_library,
)
from .paths import (
    _pp_candidates,
    find_library_dirs,
    find_playlist_dir,
    find_pp_root,
)
from .playlist import (
    ACTION_NEEDED_COLOR,
    TYPE_COLORS,
    _color_dict,
    _color_for_type,
    build_playlist_payload,
)
from .templates import (
    auto_detect_template_uuid,
    fetch_pp_playlist_items,
    fetch_pp_playlists,
    playlist_to_sections,
    resolve_section,
    template_candidates,
)
from .timers import (
    _RB_TIMER_PREFIX,
    _create_pp_timers,
    _delete_existing_rb_timers,
)
