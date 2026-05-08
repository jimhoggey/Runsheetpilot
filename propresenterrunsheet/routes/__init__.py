"""Flask blueprints for Service Mate routes.

Phase 2 of the refactor extracts /api/runsheet/* and /api/clocks/* into
blueprints registered from propresenter_app.py via register_blueprints(app).
The remaining routes (settings, library, parse, playlist, etc.) stay inline
in propresenter_app.py for now and migrate in phase 4."""

from .clocks import bp as clocks_bp
from .runsheet import bp as runsheet_bp


def register_blueprints(app) -> None:
    """Register all Service Mate blueprints onto the Flask app."""
    app.register_blueprint(runsheet_bp)
    app.register_blueprint(clocks_bp)
