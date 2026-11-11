#!/usr/bin/env python3
"""
Test client for Host SoVITS IPC daemon.
Can communicate over UNIX socket (CGBOT_SOVITS_SOCKET) or TCP (CGBOT_SOVITS_HOST:CGBOT_SOVITS_PORT).
"""

import asyncio
import json
import os
import sys

UNIX_SOCKET_PATH = os.environ.get(
    "CGBOT_SOVITS_SOCKET",
    os.path.join(os.environ.get("CGBOT_VOICE_DATA_DIR", "/var/lib/cgbot-voice"), "sovits.sock"),
)
TCP_HOST = os.environ.get("CGBOT_SOVITS_HOST", "127.0.0.1")
TCP_PORT = int(os.environ.get("CGBOT_SOVITS_PORT", "10096"))


async def send_ipc_request(req_dict: dict) -> dict:
    reader, writer = None, None
    # 1. Try UNIX domain socket first
    if os.path.exists(UNIX_SOCKET_PATH):
        try:
            reader, writer = await asyncio.open_unix_connection(UNIX_SOCKET_PATH)
            # print(f"[Client] Connected via UNIX socket: {UNIX_SOCKET_PATH}")
        except Exception as e:
            # print(f"[Client] UNIX socket failed ({e}), trying TCP...")
            pass

    # 2. Fallback to TCP
    if writer is None:
        try:
            reader, writer = await asyncio.open_connection(TCP_HOST, TCP_PORT)
            # print(f"[Client] Connected via TCP: {TCP_HOST}:{TCP_PORT}")
        except Exception as e:
            raise ConnectionError(f"Failed to connect to SoVITS daemon via Unix socket or TCP: {e}")

    try:
        payload = json.dumps(req_dict, ensure_ascii=False).encode("utf-8")
        header = len(payload).to_bytes(4, byteorder="big")
        writer.write(header + payload)
        await writer.drain()

        # Read response header
        raw_len = await reader.readexactly(4)
        length = int.from_bytes(raw_len, byteorder="big")
        raw_resp = await reader.readexactly(length)
        return json.loads(raw_resp.decode("utf-8"))
    finally:
        writer.close()
        await writer.wait_closed()


async def main():
    print("Testing SoVITS IPC Daemon...")
    # Ping
    ping_resp = await send_ipc_request({"action": "ping"})
    print("Ping Response:")
    print(json.dumps(ping_resp, indent=2, ensure_ascii=False))

    # Status
    status_resp = await send_ipc_request({"action": "status"})
    print("\nStatus Response:")
    print(json.dumps(status_resp, indent=2, ensure_ascii=False))

    # Synthesize
    print("\nTesting TTS Synthesis via IPC...")
    synth_resp = await send_ipc_request({
        "action": "synthesize",
        "text": "你好，IPC语音合成测试成功。",
        "language": "zh",
    })
    print("Synthesize Response:")
    print(json.dumps(synth_resp, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
