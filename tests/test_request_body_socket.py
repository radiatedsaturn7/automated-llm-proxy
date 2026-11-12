"""Disposable loopback regressions for unfinished HTTP/1.1 uploads."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
import socket
from types import SimpleNamespace

import pytest
import uvicorn

from gateway.main import BodyLimitMiddleware


class SocketTestServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The test owns this server task, not process-wide shutdown handlers.
        yield


@asynccontextmanager
async def listening(app, protocol):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = SocketTestServer(uvicorn.Config(
        app, http=protocol, lifespan="off", log_config=None, access_log=False,
        timeout_keep_alive=30, timeout_graceful_shutdown=2,
    ))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    task.result()
                    raise AssertionError("Disposable listener stopped before startup")
                await asyncio.sleep(0.01)
        yield port
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            listener.close()


@pytest.mark.parametrize("protocol", ["h11", "httptools"])
@pytest.mark.parametrize("status,payload", [(408, b"x"), (413, b"x" * 17)])
async def test_unfinished_upload_rejection_closes_connection(monkeypatch, protocol, status, payload):
    pytest.importorskip(protocol)
    monkeypatch.setattr("gateway.main.get_settings", lambda: SimpleNamespace(
        max_request_bytes=16, request_body_timeout_seconds=0.2,
    ))
    dispatched = []

    async def downstream(scope, receive, send):
        dispatched.append(scope)
        raise AssertionError("Rejected upload must not reach authentication or inference")

    async with listening(BodyLimitMiddleware(downstream), protocol) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Length: 1024\r\n\r\n" + payload
            )
            await writer.drain()
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = headers.split(b"\r\n")
            assert lines[0].split()[1] == str(status).encode()
            fields = dict(line.split(b":", 1) for line in lines[1:] if line)
            fields = {key.lower(): value.strip().lower() for key, value in fields.items()}
            assert fields[b"connection"] == b"close"
            await asyncio.wait_for(reader.readexactly(int(fields[b"content-length"])), 5)

            # Without Connection: close, a further byte cancels Uvicorn's
            # keepalive timer while the unfinished upload has no receive task.
            # It must not extend this rejected request's connection lifetime.
            try:
                writer.write(b"x")
                await writer.drain()
            except ConnectionError:
                pass  # An already-closed socket is also the required result.
            try:
                assert await asyncio.wait_for(reader.read(1), 2) == b""
            except ConnectionResetError:
                pass
            assert not dispatched
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except ConnectionError:
                pass
