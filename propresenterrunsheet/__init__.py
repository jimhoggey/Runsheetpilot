"""propresenterrunsheet — internal package for the ProPresenter Runsheet
Builder app.

The package is a refactor in progress (see plan/crystalline-chasing-peach.md
for the full plan). Phase 2 has carved out Service Mate; later phases will
also pull out ProPresenter integration, parsing/AI, and the slim entry-point
shell.

For now, propresenter_app.py still hosts most of the app and re-exports the
package's symbols so tests and external callers don't need to know about
the new layout."""

from . import propresenter  # noqa: F401  — load submodule for re-export
from . import service_mate  # noqa: F401  — load submodule for re-export
from .routes import register_blueprints  # noqa: F401
