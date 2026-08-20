"""Building the ProPresenter base URL — validate, resolve, and PIN.

`host` and `port` arrive from the browser (Settings form / request body).
Choosing the host is the feature — ProPresenter can be on another
machine — but it is also an SSRF sink, so the boundary is enforced here,
in the one function every ProPresenter URL must come from.

The guard works by CONSTRUCTION, not by checking-then-passing-through:

  1. the host is validated against the only places ProPresenter can be —
     loopback, private ranges, link-local, LAN names;
  2. a name is resolved ONCE, and the URL is built from the vetted IP,
     never from the caller's string.

Pinning to the resolved address closes the classic validate/fetch gap
(DNS rebinding: a name that answers private for the check and public for
the fetch), makes every IP-literal spelling trick (integer, hex, dotted
octal) irrelevant, and fixes a real bug — "::1" used to be mangled to
"http://1:50001" by the character-strip. It is also why the URL contains
no request-derived string at all, which is what a taint analyser needs
to see.

Severity context, for whoever reads this during an audit: the app binds
127.0.0.1 only, so reaching this API at all requires code already
running on the operator's machine. The boundary is defence in depth,
not the last line.
"""

import ipaddress
import re
import socket
import time

_HOST_RE = re.compile(r"[^A-Za-z0-9.\-]")

# Link-local stays allowed — a direct ethernet cable between two machines
# is a real AV setup — but these two addresses in that range are the
# cloud metadata services, the classic SSRF target, and ProPresenter is
# never behind them.
_BLOCKED = {ipaddress.ip_address("169.254.169.254"),
            ipaddress.ip_address("fd00:ec2::254")}

# Resolution cache: the media-assist poll and port discovery hit pp_base
# every few seconds, and re-asking mDNS each time is pointless. Short TTL
# so a machine that changes address is picked up within a minute.
_CACHE_TTL_S = 30
_cache: dict = {}


class UnreachableHost(ValueError):
    """The requested host isn't somewhere ProPresenter can be.

    Carries a message written for the operator, so routes can return it
    directly instead of inventing their own wording. An app-level
    errorhandler in propresenter_app.py turns it into the standard
    {"ok": false, "error": ...} shape for every route at once.
    """


def _permitted(ip) -> bool:
    if ip in _BLOCKED:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def _as_ip_literal(host: str):
    """The host as an address object, decoding disguises, or None.

    "134744072" and "0x8080808" are dotless, so a naive "no dot means
    LAN name" rule waves them through — and both are 8.8.8.8.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if re.fullmatch(r"(?:0x)?[0-9a-f]+", host):
        try:
            return ipaddress.ip_address(
                int(host, 16 if host.startswith("0x") else 10))
        except Exception:
            return None
    return None


def resolve_pp_host(host):
    """Validate `host` and return the vetted address to connect to.

    Returns an ipaddress object, or None when the host is outside
    loopback/LAN, unresolvable, or malformed. None is deliberate for
    ALL failure modes — "we couldn't check" must not mean "allowed".

    Names resolve through the OS (which covers .local via mDNS and bare
    names via the LAN's own lookup), and the first permitted address is
    pinned, IPv4 preferred. A record set that ALSO carries a public
    address doesn't disqualify the name — we never connect to the public
    one, which is the point of pinning.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host or host == "localhost":
        return ipaddress.ip_address("127.0.0.1")

    literal = _as_ip_literal(host)
    if literal is not None:
        return literal if _permitted(literal) else None

    if _HOST_RE.search(host):
        return None                      # not a hostname shape at all

    now = time.time()
    hit = _cache.get(host)
    if hit and hit[0] > now:
        return hit[1]

    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return None                      # unresolvable: fail closed

    v4 = v6 = None
    for info in infos:
        try:
            # v6 sockaddrs can carry a %zone suffix on some platforms.
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except Exception:
            continue
        if not _permitted(ip):
            continue
        if ip.version == 4 and v4 is None:
            v4 = ip
        elif ip.version == 6 and v6 is None:
            v6 = ip
    resolved = v4 or v6
    _cache[host] = (now + _CACHE_TTL_S, resolved)
    return resolved


def is_reachable_pp_host(host) -> bool:
    """True when `host` is somewhere ProPresenter could actually be."""
    return resolve_pp_host(host) is not None


def reset_cache():
    """Tests, and anywhere a stale resolution would confuse things."""
    _cache.clear()


def pp_base(host, port) -> str:
    """The base URL to reach ProPresenter, or raise UnreachableHost.

    The returned URL's host is ALWAYS the string form of the vetted
    address from resolve_pp_host — never the caller's input. See the
    module docstring for why that construction is the security property.
    """
    ip = resolve_pp_host(host)
    if ip is None:
        raise UnreachableHost(
            f"{str(host)[:60]} isn't an address ProPresenter can be on. "
            f"Use localhost, or the computer's name or LAN IP.")
    host_part = f"[{ip}]" if ip.version == 6 else str(ip)
    digits = re.sub(r"\D", "", str(port or ""))
    return f"http://{host_part}:{digits or '50001'}"
