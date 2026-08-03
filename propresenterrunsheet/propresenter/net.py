"""Building the ProPresenter base URL from operator settings.

`host` and `port` arrive from the browser (Settings form / request body).
This is a single-user local app talking to the operator's own
ProPresenter, so a user-chosen host is the feature — but the values
still get clamped to a hostname/IP and a numeric port so nothing else
(paths, credentials, a second URL) can ride along in them, and so log
lines built from them stay single-line.
"""

import re

_HOST_RE = re.compile(r"[^A-Za-z0-9.\-]")


def pp_base(host, port) -> str:
    """A clean http://host:port with everything non-hostname stripped."""
    clean_host = _HOST_RE.sub("", str(host or "").strip()) or "localhost"
    digits = re.sub(r"\D", "", str(port or ""))
    clean_port = digits or "50001"
    return f"http://{clean_host}:{clean_port}"
