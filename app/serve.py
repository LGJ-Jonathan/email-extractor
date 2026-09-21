"""Start the API on one dual-stack socket: `python -m app.serve`.

Railway's health check connects over IPv4 and its private network (how the portal
reaches us) over IPv6. `uvicorn --host ::` gave an IPv6-only socket there, so the
health check never got an answer. Clearing IPV6_V6ONLY explicitly takes both.
"""

import os
import socket

import uvicorn


def dual_stack_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(("::", port))
    return sock


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    config = uvicorn.Config("app.main:app", proxy_headers=True, forwarded_allow_ips="*")
    uvicorn.Server(config).run(sockets=[dual_stack_socket(port)])


if __name__ == "__main__":
    main()
