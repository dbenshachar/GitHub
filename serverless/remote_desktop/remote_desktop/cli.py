from __future__ import annotations

import argparse
import asyncio
import select
import socket
import sys
import termios
import tty
from contextlib import suppress

from .artifacts import ArtifactError, MiniOSArtifacts
from .config import ServiceConfig
from .distributed import DistributedSessionRouter, NodeAddress, NodeAgentServer, NodeClient
from .gateway import EdgeAddress, EdgeGateway
from .router import SessionRouter
from .runner import NodeRunner
from .tcp_router import TcpRouter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="remote-desktop")
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser("serve", help="run a single-machine router and node")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8023)
    serve.add_argument("--mini-os", default="auto", help="path to the mini_os repo")
    serve.add_argument("--work-dir", default=".remote_desktop")
    serve.add_argument("--qemu", default="qemu-system-aarch64")
    serve.add_argument("--make", default="make")
    serve.add_argument("--no-build", action="store_true", help="use existing Mini OS artifacts")
    serve.add_argument("--max-sessions", type=int, default=16)

    node = subcommands.add_parser("node", help="run a worker node agent")
    node.add_argument("--host", default="0.0.0.0")
    node.add_argument("--port", type=int, default=9100)
    node.add_argument("--node-id", default=socket.gethostname())
    node.add_argument("--mini-os", default="auto", help="path to the mini_os repo")
    node.add_argument("--work-dir", default=".remote_desktop_node")
    node.add_argument("--qemu", default="qemu-system-aarch64")
    node.add_argument("--make", default="make")
    node.add_argument("--no-build", action="store_true", help="use existing Mini OS artifacts")
    node.add_argument("--max-sessions", type=int, default=16)

    router = subcommands.add_parser("router", help="run a distributed edge router")
    router.add_argument("--host", default="127.0.0.1")
    router.add_argument("--port", type=int, default=8023)
    router.add_argument(
        "--node",
        action="append",
        required=True,
        help="worker as HOST:PORT or NODE_ID=HOST:PORT; repeat for multiple computers",
    )

    gateway = subcommands.add_parser("gateway", help="run a frontdoor across multiple edge routers")
    gateway.add_argument("--host", default="127.0.0.1")
    gateway.add_argument("--port", type=int, default=8000)
    gateway.add_argument(
        "--edge",
        action="append",
        required=True,
        help="edge router as HOST:PORT or EDGE_ID=HOST:PORT; repeat for multiple routers",
    )
    gateway.add_argument("--connect-timeout", type=float, default=5)

    connect = subcommands.add_parser("connect", help="create and attach to a session")
    connect.add_argument("--host", default="127.0.0.1")
    connect.add_argument("--port", type=int, default=8023)
    connect.add_argument("--name", default=None)
    connect.add_argument("--profile", default=None)
    connect.add_argument("--session", default=None, help="attach to an existing session id")

    resize = subcommands.add_parser("resize", help="resize an existing session resource profile")
    resize.add_argument("session_id")
    resize.add_argument("profile")
    resize.add_argument("--host", default="127.0.0.1")
    resize.add_argument("--port", type=int, default=8023)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return asyncio.run(_serve(args))
    if args.command == "node":
        return asyncio.run(_node(args))
    if args.command == "router":
        return asyncio.run(_router(args))
    if args.command == "gateway":
        return asyncio.run(_gateway(args))
    if args.command == "connect":
        return _connect(args)
    if args.command == "resize":
        return _resize(args)
    raise AssertionError(f"unhandled command: {args.command}")


async def _serve(args: argparse.Namespace) -> int:
    config = ServiceConfig.default(
        host=args.host,
        port=args.port,
        mini_os_root=args.mini_os,
        work_dir=args.work_dir,
        qemu_bin=args.qemu,
        make_bin=args.make,
        build=not args.no_build,
        max_sessions=args.max_sessions,
    )
    artifacts = MiniOSArtifacts(
        config.mini_os_root,
        make_bin=config.make_bin,
        build=config.build,
        tool_dir=config.work_dir / "bin",
    )
    try:
        await artifacts.prepare()
    except ArtifactError as exc:
        print(f"remote_desktop: {exc}", file=sys.stderr)
        return 1

    runner = NodeRunner(
        artifacts=artifacts,
        work_dir=config.work_dir,
        qemu_bin=config.qemu_bin,
        replay_bytes=config.replay_bytes,
    )
    router = SessionRouter(runner, max_sessions=config.max_sessions)
    tcp_router = TcpRouter(router)
    try:
        await tcp_router.serve(config.host, config.port)
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        pass
    finally:
        await router.close_all()
    return 0


async def _node(args: argparse.Namespace) -> int:
    config = ServiceConfig.default(
        host=args.host,
        port=args.port,
        mini_os_root=args.mini_os,
        work_dir=args.work_dir,
        qemu_bin=args.qemu,
        make_bin=args.make,
        build=not args.no_build,
        max_sessions=args.max_sessions,
    )
    artifacts = MiniOSArtifacts(
        config.mini_os_root,
        make_bin=config.make_bin,
        build=config.build,
        tool_dir=config.work_dir / "bin",
    )
    try:
        await artifacts.prepare()
    except ArtifactError as exc:
        print(f"remote_desktop node: {exc}", file=sys.stderr)
        return 1

    runner = NodeRunner(
        artifacts=artifacts,
        work_dir=config.work_dir,
        qemu_bin=config.qemu_bin,
        replay_bytes=config.replay_bytes,
    )
    agent = NodeAgentServer.from_runner(
        node_id=args.node_id,
        host=config.host,
        port=config.port,
        runner=runner,
        max_sessions=config.max_sessions,
    )
    try:
        await agent.serve()
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        pass
    finally:
        await agent.close()
    return 0


async def _router(args: argparse.Namespace) -> int:
    try:
        addresses = [NodeAddress.parse(value) for value in args.node]
    except ValueError as exc:
        print(f"remote_desktop router: {exc}", file=sys.stderr)
        return 2
    nodes = [NodeClient(address) for address in addresses]
    router = DistributedSessionRouter(nodes)
    tcp_router = TcpRouter(router)
    try:
        await tcp_router.serve(args.host, args.port)
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        pass
    return 0


async def _gateway(args: argparse.Namespace) -> int:
    try:
        edges = [EdgeAddress.parse(value) for value in args.edge]
    except ValueError as exc:
        print(f"remote_desktop gateway: {exc}", file=sys.stderr)
        return 2
    gateway = EdgeGateway(edges, connect_timeout=args.connect_timeout)
    try:
        await gateway.serve(args.host, args.port)
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        pass
    return 0


def _connect(args: argparse.Namespace) -> int:
    with socket.create_connection((args.host, args.port)) as sock:
        sock.settimeout(5)
        greeting = _recv_until(sock, b"rd> ")
        sys.stdout.buffer.write(greeting)
        sys.stdout.buffer.flush()

        session_id = args.session
        if session_id is None:
            parts = []
            if args.name:
                parts.append(args.name)
            if args.profile:
                parts.append(f"profile={args.profile}")
            suffix = " " + " ".join(parts) if parts else ""
            sock.sendall(f"NEW{suffix}\n".encode())
            response = _recv_until(sock, b"rd> ")
            sys.stdout.buffer.write(response)
            sys.stdout.buffer.flush()
            session_id = _parse_session_id(response)
            if session_id is None:
                print("remote_desktop: server did not return a session id", file=sys.stderr)
                return 1

        sock.sendall(f"ATTACH {session_id}\n".encode())
        attached = _recv_line(sock)
        sys.stdout.buffer.write(attached)
        sys.stdout.buffer.flush()
        if not attached.startswith(b"ATTACHED "):
            return 1
        return _interactive(sock)


def _resize(args: argparse.Namespace) -> int:
    with socket.create_connection((args.host, args.port)) as sock:
        sock.settimeout(10)
        _recv_until(sock, b"rd> ")
        sock.sendall(f"RESIZE {args.session_id} {args.profile}\n".encode())
        response = _recv_until(sock, b"rd> ")
        sys.stdout.buffer.write(response)
        sys.stdout.buffer.flush()
        return 0 if response.startswith(b"RESIZED ") else 1


def _recv_until(sock: socket.socket, marker: bytes) -> bytes:
    chunks: list[bytes] = []
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        data += chunk
    return b"".join(chunks)


def _recv_line(sock: socket.socket) -> bytes:
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            break
        data += chunk
    return data


def _parse_session_id(response: bytes) -> str | None:
    for line in response.decode(errors="replace").splitlines():
        if line.startswith("SESSION "):
            return line.split(maxsplit=1)[1].strip()
    return None


def _interactive(sock: socket.socket) -> int:
    sock.setblocking(False)
    stdin_fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(stdin_fd)
    try:
        tty.setraw(stdin_fd)
        while True:
            readable, _, _ = select.select([stdin_fd, sock], [], [])
            if stdin_fd in readable:
                data = sys.stdin.buffer.read1(4096)
                if not data:
                    return 0
                sock.sendall(data)
            if sock in readable:
                with suppress(BlockingIOError):
                    data = sock.recv(4096)
                    if not data:
                        return 0
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
    except KeyboardInterrupt:
        return 130
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)


if __name__ == "__main__":
    raise SystemExit(main())
