"""propresenterrunsheet — internal package for the ProPresenter Runsheet
Builder app.

The package is a refactor in progress (see plan/crystalline-chasing-peach.md
for the full plan). Phase 2 has carved out Service Mate; later phases will
also pull out ProPresenter integration, parsing/AI, and the slim entry-point
shell.

For now, propresenter_app.py still hosts most of the app and re-exports the
package's symbols so tests and external callers don't need to know about
the new layout."""

# Order matters: config + logging_setup populate DATA_DIR / log first so
# downstream submodules can import from them without further setup.
from . import config  # noqa: F401
from . import logging_setup  # noqa: F401
from . import parsing  # noqa: F401
from . import propresenter  # noqa: F401
from . import service_mate  # noqa: F401
from . import settings  # noqa: F401
from .routes import register_blueprints  # noqa: F401
