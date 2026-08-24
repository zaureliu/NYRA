from __future__ import annotations
import asyncio, json, socket


async def discover(timeout: float=.35) -> dict | None:
    def receive():
        sock=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sock.settimeout(timeout)
        try:
            sock.bind(("0.0.0.0", 47779)); return json.loads(sock.recvfrom(8192)[0])
        except (OSError, ValueError): return None
        finally: sock.close()
    return await asyncio.to_thread(receive)
