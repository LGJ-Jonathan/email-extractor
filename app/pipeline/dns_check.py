"""Stage 1 DNS: reachability, MX provider, and the NS records parked detection needs."""

import asyncio
import logging
from dataclasses import dataclass, field

import dns.asyncresolver
import dns.exception
import dns.resolver

from app.pipeline.normalize import host_of_key

log = logging.getLogger("email_extractor.dns")

DNS_TIMEOUT_S = 5.0


@dataclass
class DnsResult:
    resolves: bool = False
    a_domain: bool = False
    a_www: bool = False
    mx_provider: str = "none"          # google | microsoft | other | none
    ns_hosts: list[str] = field(default_factory=list)
    nxdomain: bool = False
    error: str | None = None

    @property
    def error_class(self) -> str | None:
        """NXDOMAIN is permanent; a timeout or SERVFAIL is transient (spec 17)."""
        if self.resolves:
            return None
        return "permanent" if self.nxdomain else "transient"


def _resolver() -> dns.asyncresolver.Resolver:
    r = dns.asyncresolver.Resolver()
    r.timeout = DNS_TIMEOUT_S
    r.lifetime = DNS_TIMEOUT_S
    return r


def classify_mx(mx_hosts: list[str]) -> str:
    """Spec Stage 1: google | microsoft | other | none."""
    if not mx_hosts:
        return "none"
    for h in mx_hosts:
        h = h.lower().rstrip(".")
        if h.endswith("google.com") or h.endswith("googlemail.com"):
            return "google"
    for h in mx_hosts:
        h = h.lower().rstrip(".")
        if h.endswith("outlook.com") or "protection.outlook" in h:
            return "microsoft"
    return "other"


async def _query(resolver, name: str, rdtype: str) -> tuple[list[str], Exception | None]:
    try:
        answer = await resolver.resolve(name, rdtype)
        return [str(r).rstrip(".") for r in answer], None
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers) as e:
        return [], e
    except (dns.exception.Timeout, dns.exception.DNSException) as e:
        return [], e


async def check_domain(domain: str) -> DnsResult:
    """Resolve A/AAAA for the host and its www form, plus MX and NS."""
    host = host_of_key(domain)
    resolver = _resolver()

    async def any_address(name: str) -> tuple[bool, Exception | None]:
        for rdtype in ("A", "AAAA"):
            values, err = await _query(resolver, name, rdtype)
            if values:
                return True, None
            if isinstance(err, dns.resolver.NXDOMAIN):
                return False, err
        return False, err

    (a_dom, e1), (a_www, e2), (mx, _), (ns, _) = await asyncio.gather(
        any_address(host),
        any_address(f"www.{host}"),
        _query(resolver, host, "MX"),
        _query(resolver, host, "NS"),
    )

    resolves = bool(a_dom or a_www)
    nxdomain = not resolves and isinstance(e1, dns.resolver.NXDOMAIN) and (
        e2 is None or isinstance(e2, dns.resolver.NXDOMAIN)
    )

    # MX records arrive as "10 mail.example.com"; keep the host half.
    mx_hosts = [v.split()[-1] for v in mx if v.split()]

    return DnsResult(
        resolves=resolves,
        a_domain=a_dom,
        a_www=a_www,
        mx_provider=classify_mx(mx_hosts),
        ns_hosts=[n.lower() for n in ns],
        nxdomain=nxdomain,
        error=None if resolves else (type(e1).__name__ if e1 else "no_address"),
    )
