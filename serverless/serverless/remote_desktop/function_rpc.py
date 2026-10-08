"""Bounded asynchronous JSON HTTP transport for the worker data path."""
import asyncio
import hmac
import json
import ssl
from urllib.parse import urlsplit

MAX_BODY = 4 * 1024 * 1024


class RPCError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


async def request(url, token, method, path, body=None, *, timeout=30):
    endpoint = urlsplit(url)
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
        raise ValueError("RPC requires an HTTP(S) URL")
    writer = None
    try:
        async with asyncio.timeout(timeout):
            context = ssl.create_default_context() if endpoint.scheme == "https" else None
            reader, writer = await asyncio.open_connection(endpoint.hostname,
                endpoint.port or (443 if context else 80), ssl=context, limit=16384)
            data = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
            if len(data) > MAX_BODY:
                raise RPCError(413, "RPC request exceeds size limit")
            writer.write((f"{method} {path} HTTP/1.1\r\nHost: {endpoint.netloc}\r\n"
                f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data)
            await writer.drain()
            header = (await reader.readuntil(b"\r\n\r\n")).decode()
            lines = header.split("\r\n")
            status = int(lines[0].split()[1])
            fields = {k.lower(): v.strip() for k, v in
                      (line.split(":", 1) for line in lines[1:] if ":" in line)}
            size = int(fields.get("content-length", "0"))
            if not 0 <= size <= MAX_BODY:
                raise RPCError(502, "RPC response exceeds size limit")
            value = json.loads(await reader.readexactly(size)) if size else {}
            if not 200 <= status < 300:
                raise RPCError(status, value.get("error", f"HTTP {status}"))
            return value
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


class Server:
    def __init__(self, handler, token, *, max_connections=4096):
        if len(token) < 32:
            raise ValueError("RPC token must contain at least 32 characters")
        self.handler, self.token = handler, token
        self.max_connections = max_connections
        self.tasks = set()

    async def reply(self, writer, status, value):
        body = json.dumps(value, separators=(",", ":")).encode()
        if len(body) > MAX_BODY:
            status, body = 413, b'{"error":"result exceeds RPC size limit"}'
        writer.write(f"HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        if len(self.tasks) >= self.max_connections:
            try:
                await self.reply(writer, 503, {"error": "RPC connection limit reached"})
            finally:
                writer.close()
            return
        self.tasks.add(task)
        operation = disconnected = None
        try:
            async with asyncio.timeout(10):
                lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
                method, path, _ = lines[0].split()
                fields = {k.lower(): v.strip() for k, v in
                          (line.split(":", 1) for line in lines[1:] if ":" in line)}
                if path != "/healthz" and not hmac.compare_digest(
                        fields.get("authorization", ""), "Bearer " + self.token):
                    return await self.reply(writer, 401, {"error": "unauthorized"})
                size = int(fields.get("content-length", "0"))
                if "transfer-encoding" in fields or not 0 <= size <= 100000:
                    return await self.reply(writer, 413, {"error": "invalid request size"})
                body = json.loads(await reader.readexactly(size)) if size else None
            operation = asyncio.create_task(self.handler(method, path, body))
            disconnected = asyncio.create_task(reader.read(1))
            done, _ = await asyncio.wait((operation, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if operation in done:
                status, value = await operation
                await self.reply(writer, status, value)
        except RPCError as exc:
            await self.reply(writer, exc.status, {"error": str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            await self.reply(writer, 400, {"error": str(exc)})
        except (ConnectionError, OSError, TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError):
            pass
        except Exception as exc:
            print(json.dumps({"event": "rpc_error", "error": str(exc)}), flush=True)
            await self.reply(writer, 503, {"error": "worker routing unavailable"})
        finally:
            children = [t for t in (operation, disconnected) if t]
            for child in children:
                if not child.done():
                    child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.tasks.discard(task)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
