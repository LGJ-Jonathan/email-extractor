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
# Asked when the system resolver gives no usable answer (timeout, SERVFAIL). The
# platform's resolver is one shared address; under a 50-domain crawl it dropped
# queries, and each drop was recorded as a dead domain.
FALLBACK_NAMESERVERS = ("1.1.1.1", "8.8.8.8")


@dataclass
class DnsResult:
    resolves: bool = False
    a_domain: bool = False
    a_www: bool = False
    mx_provider: str = "none"          # google | microsoft | other | none
    ns_hosts: list[str] = field(default_factory=list)
    nxdomain: bool = False
    # The lookup itself failed (timeout, SERVFAIL): we do not know whether the domain
    # exists, so this must never be stored as the domain's answer.
    transient: bool = False
    error: str | None = None

    @property
    def error_class(self) -> str | None:
        """No such domain / no address is permanent; a failed lookup is transient
        (spec 17)."""
        if self.resolves:
            return None
        return "transient" if self.transient else "permanent"


def _resolver(nameservers: tuple[str, ...] | None = None) -> dns.asyncresolver.Resolver:
    if nameservers:
        r = dns.asyncresolver.Resolver(configure=False)
        r.nameservers = list(nameservers)
    else:
        r = dns.asyncresolver.Resolver()
    r.timeout = DNS_TIMEOUT_S / 2          # per server, so a retry fits in the lifetime
    r.lifetime = DNS_TIMEOUT_S
    return r


def _definitive(err: Exception | None) -> bool:
    """Did DNS actually answer? NXDOMAIN and NoAnswer are answers; anything else
    (timeout, SERVFAIL, refused) is the lookup failing."""
    return err is None or isinstance(err, (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer))


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


async def _lookup(resolver, host: str):
    async def any_address(name: str) -> tuple[bool, Exception | None]:
        err: Exception | None = None
        failed: Exception | None = None
        for rdtype in ("A", "AAAA"):
            values, err = await _query(resolver, name, rdtype)
            if values:
                return True, None
            if isinstance(err, dns.resolver.NXDOMAIN):
                return False, err
            if not _definitive(err):
                failed = err
        # One failed lookup outweighs a "no AAAA record": the A answer is still unknown.
        return False, failed or err

    return await asyncio.gather(
        any_address(host),
        any_address(f"www.{host}"),
        _query(resolver, host, "MX"),
        _query(resolver, host, "NS"),
    )


async def check_domain(domain: str) -> DnsResult:
    """Resolve A/AAAA for the host and its www form, plus MX and NS.

    A lookup that fails without an answer is asked again of public resolvers before
    it is believed. On the 18k Miami run 1,446 domains were written off as "dns" in
    one pass; re-resolved from the same container minutes later, 88% of a 120-domain
    sample had perfectly good records.
    """
    host = host_of_key(domain)
    (a_dom, e1), (a_www, e2), (mx, _), (ns, _) = await _lookup(_resolver(), host)

    if not (a_dom or a_www) and not (_definitive(e1) and _definitive(e2)):
        log.info("dns_fallback", extra={"domain": domain,
                                        "err": type(e1 or e2).__name__})
        (a_dom, e1), (a_www, e2), (mx2, _), (ns2, _) = await _lookup(
            _resolver(FALLBACK_NAMESERVERS), host
        )
        mx, ns = mx or mx2, ns or ns2

    resolves = bool(a_dom or a_www)
    nxdomain = not resolves and isinstance(e1, dns.resolver.NXDOMAIN) and (
        e2 is None or isinstance(e2, dns.resolver.NXDOMAIN)
    )
    transient = not resolves and not (_definitive(e1) and _definitive(e2))

    # MX records arrive as "10 mail.example.com"; keep the host half.
    mx_hosts = [v.split()[-1] for v in mx if v.split()]

    return DnsResult(
        resolves=resolves,
        a_domain=a_dom,
        a_www=a_www,
        mx_provider=classify_mx(mx_hosts),
        ns_hosts=[n.lower() for n in ns],
        nxdomain=nxdomain,
        transient=transient,
        error=None if resolves else (type(e1 or e2).__name__ if (e1 or e2) else "no_address"),
    )
