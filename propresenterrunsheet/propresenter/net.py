"""Building the ProPresenter base URL from operator settings.

`host` and `port` arrive from the browser (Settings form / request body).
This is a single-user local app talking to the operator's own
ProPresenter, so a user-chosen host is the feature — but the values
still get clamped to a hostname/IP and a numeric port so nothing else
(paths, credentials, a second URL) can ride along in them, and so log
lines built from them stay single-line.
"""

import ipaddress
import re

_HOST_RE = re.compile(r"[^A-Za-z0-9.\-]")

# Link-local is allowed because a direct ethernet cable between two
# machines is a real AV setup — but these two addresses in that range are
# the cloud metadata services, the classic SSRF target, and ProPresenter
# is never behind them.
_BLOCKED = {ipaddress.ip_address("169.254.169.254"),
            ipaddress.ip_address("fd00:ec2::254")}


def _permitted(ip) -> bool:
    if ip in _BLOCKED:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def is_reachable_pp_host(host) -> bool:
    """True for a host ProPresenter could plausibly be running on.

    The app takes a host from the request body and fetches it, which is
    an SSRF sink: without a limit, anyone who can reach this local API
    could use the app to probe arbitrary internet addresses.

    ProPresenter is never on the public internet — it is this machine or
    another one on the church's LAN. So the honest boundary is loopback,
    the private IPv4/IPv6 ranges, link-local, and bare or `.local`
    hostnames. That keeps every real setup working (localhost,
    192.168.1.153, "Fynns-MacBook-Air.local") and refuses the rest.

    A hostname is resolved before judging it, so `evil.example.com`
    pointing at a public address is rejected too. Resolution failure
    means "not reachable", which is the safe answer either way.
    """
    host = (host or "").strip().lower()
    if not host or host in ("localhost", "::1"):
        return True
    try:
        return _permitted(ipaddress.ip_address(host))
    except ValueError:
        pass                       # a name, not an address — resolve it
    # A dotless label is a LAN name ("macbook") — but only if it looks
    # like a name. "134744072" and "0x8080808" are dotless too, and both
    # resolve to 8.8.8.8, so a digits-or-hex-only label is an IP literal
    # in disguise and must go through the address check instead.
    if re.fullmatch(r"(?:0x)?[0-9a-f]+", host):
        try:
            return _permitted(ipaddress.ip_address(int(
                host, 16 if host.startswith("0x") else 10)))
        except Exception:
            return False
    if host.endswith(".local") or "." not in host:
        return True                # mDNS / bare LAN name
    try:
        import socket
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False               # unresolvable: treat as unreachable
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            return False
        if not _permitted(ip):
            return False
    return bool(infos)


class UnreachableHost(ValueError):
    """The requested host isn't somewhere ProPresenter can be.

    Carries a message written for the operator, so routes can return it
    directly instead of inventing their own wording.
    """


def pp_base(host, port) -> str:
    """A clean http://host:port with everything non-hostname stripped.

    RAISES UnreachableHost for anything outside loopback and the LAN.
    The check lives here, not in the routes, because every ProPresenter
    URL in the app is built through this one function — the first
    version of this guard was wired into a single route and left four
    other request-body-driven fetches wide open.
    """
    if not is_reachable_pp_host(host):
        raise UnreachableHost(
            f"{str(host)[:60]} isn't an address ProPresenter can be on. "
            f"Use localhost, or the computer's name or LAN IP.")
    clean_host = _HOST_RE.sub("", str(host or "").strip()) or "localhost"
    digits = re.sub(r"\D", "", str(port or ""))
    clean_port = digits or "50001"
    return f"http://{clean_host}:{clean_port}"
