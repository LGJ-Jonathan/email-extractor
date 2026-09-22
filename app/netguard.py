"""Is this address one we may connect to from inside the private network?

Shared by webhook delivery and direct page fetches. The worker and the API sit on
Railway's private network next to Postgres, Redis and the portal, so a URL that
resolves to a private, loopback, link-local or NAT64-mapped address must never be
fetched, whether it came from a webhook_url or from a business site's DNS record or
redirect.
"""

import asyncio
import ipaddress
import socket

_Address = ipaddress.IPv4Address | ipaddress.IPv6Address

# Python's is_global does not know these IPv6 forms, each of which embeds an IPv4
# address that a NAT64/DNS64 egress or the kernel would deliver to the private side.
_EMBEDDED_V4 = (
    ipaddress.ip_network("64:ff9b::/96"),      # NAT64 well-known prefix
    ipaddress.ip_network("64:ff9b:1::/48"),    # NAT64 local-use prefix
    ipaddress.ip_network("::/96"),             # IPv4-compatible (deprecated)
    ipaddress.ip_network("2002::/16"),         # 6to4
)


def is_public(ip: _Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _EMBEDDED_V4):
            return False
    return ip.is_global and not ip.is_multicast


class PrivateAddress(ValueError):
    pass


async def resolve_public(host: str, port: int) -> list[str]:
    """Every address the host resolves to, or PrivateAddress if any of them is not
    public. All of them, not just the first: a name with one public and one private
    record would otherwise pass the check and connect to the private one.
    Raises OSError when the name does not resolve."""
    try:
        addrs: list[_Address] = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
        addrs = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addrs:
        raise OSError("no addresses")
    if not all(is_public(a) for a in addrs):
        raise PrivateAddress(host)
    return [str(a) for a in addrs]
