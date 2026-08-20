"""Flask blueprints — every /api/* route in the app, organised by topic.

Each module here defines a blueprint with its own routes, registered onto
the Flask app via `register_blueprints(app)`. The split mirrors how
features are usually edited together: settings + prompt, library + scan,
playlist + connection-test, etc.

After phase 4 of the refactor, propresenter_app.py is essentially just
the Flask app object plus this one call."""

from .clocks import bp as clocks_bp
from .core import bp as core_bp
from .library import bp as library_bp
from .media_assist import bp as media_assist_bp
from .license import bp as license_bp
from .parse import bp as parse_bp
from .playlist import bp as playlist_bp
from .runsheet import bp as runsheet_bp
from .settings import bp as settings_bp
from .update import bp as update_bp


def register_blueprints(app) -> None:
    """Register every blueprint in this package onto the Flask app."""
    for bp in (core_bp, settings_bp, library_bp, parse_bp,
               playlist_bp, runsheet_bp, clocks_bp, license_bp, update_bp,
               media_assist_bp):
        app.register_blueprint(bp)
