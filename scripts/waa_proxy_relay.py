#!/usr/bin/env python3
"""Forward one TCP port on the docker bridge to a proxy listening on localhost.

A container's 127.0.0.1 is itself, so a Clash Verge proxy on the host's loopback is
unreachable there. `scripts/waa` listens on the bridge gateway and points the container
at host.docker.internal. The Windows VM is not involved.
"""

from __future__ import annotations

import argparse
import asyncio


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def _handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter, host: str, port: int) -> None:
    try:
        remote_reader, remote_writer = await asyncio.open_connection(host, port)
    except Exception:
        client_writer.close()
        return
    await asyncio.gather(_pipe(client_reader, remote_writer), _pipe(remote_reader, client_writer))


def _endpoint(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator or not host or not port.isdigit():
        raise SystemExit(f"want host:port, got {value!r}")
    return host, int(port)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", required=True, help="bridge address, as host:port")
    parser.add_argument("--target", required=True, help="the host proxy, as host:port")
    args = parser.parse_args()
    listen_host, listen_port = _endpoint(args.listen)
    target_host, target_port = _endpoint(args.target)

    async def serve() -> None:
        server = await asyncio.start_server(
            lambda reader, writer: _handle(reader, writer, target_host, target_port),
            listen_host,
            listen_port,
        )
        async with server:
            await server.serve_forever()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
