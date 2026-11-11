import asyncio
import base64
import json
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import torch
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [HostFunASR] %(message)s",
)
logger = logging.getLogger("HostFunASR")

# 配置常量（均可由环境变量覆盖；默认值见 voice-services/README.md）
DATA_DIR = os.environ.get("CGBOT_VOICE_DATA_DIR", "/var/lib/cgbot-voice")
MODEL_NAME_OR_PATH = os.environ.get("CGBOT_ASR_MODEL_PATH", "")
FALLBACK_MODEL = "iic/SenseVoiceSmall"
UNIX_SOCKET_PATH = os.environ.get("CGBOT_ASR_SOCKET", os.path.join(DATA_DIR, "funasr.sock"))
CONTAINER_MOUNT_PREFIX = os.environ.get("CGBOT_VOICE_CONTAINER_PREFIX", "/data")
HOST_MOUNT_PREFIX = DATA_DIR
TCP_HOST = os.environ.get("CGBOT_ASR_HOST", "127.0.0.1")
TCP_PORT = int(os.environ.get("CGBOT_ASR_PORT", "10095"))


class FunASRServer:
    def __init__(self) -> None:
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self.model = None
        self.inference_lock = asyncio.Lock()
        self.servers: list[asyncio.Server] = []
        self._running = True

    def load_model(self) -> None:
        logger.info(f"Loading FunASR SenseVoiceSmall model on device: {self.device}...")
        model_path = MODEL_NAME_OR_PATH if MODEL_NAME_OR_PATH and os.path.exists(MODEL_NAME_OR_PATH) else FALLBACK_MODEL
        t0 = time.time()
        self.model = AutoModel(
            model=model_path,
            device=self.device,
            disable_update=True,
        )
        logger.info(f"FunASR Model successfully loaded in {time.time() - t0:.2f}s on {self.device}")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client_addr = writer.get_extra_info("peername") or "unix_socket"
        logger.debug(f"New client connected: {client_addr}")
        try:
            while self._running:
                # 4-byte big-endian length prefix
                try:
                    raw_len = await reader.readexactly(4)
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    break

                length = int.from_bytes(raw_len, byteorder="big")
                if length <= 0 or length > 50 * 1024 * 1024:  # 50MB 保护上限
                    logger.warning(f"Invalid frame length: {length}")
                    break

                raw_payload = await reader.readexactly(length)
                try:
                    req = json.loads(raw_payload.decode("utf-8"))
                except Exception as e:
                    logger.error(f"Failed to parse JSON payload: {e}")
                    resp = {"status": "error", "error": f"Invalid JSON: {e}"}
                    await self._send_response(writer, resp)
                    continue

                resp = await self.process_request(req)
                await self._send_response(writer, resp)

        except Exception as e:
            logger.error(f"Client handling error ({client_addr}): {e}", exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_response(self, writer: asyncio.StreamWriter, resp_dict: dict[str, Any]) -> None:
        payload_bytes = json.dumps(resp_dict, ensure_ascii=False).encode("utf-8")
        header = len(payload_bytes).to_bytes(4, byteorder="big")
        writer.write(header + payload_bytes)
        await writer.drain()

    async def process_request(self, req: dict[str, Any]) -> dict[str, Any]:
        action = req.get("action", "transcribe")

        if action == "ping":
            return {
                "status": "ok",
                "device": self.device,
                "model": "SenseVoiceSmall",
                "cuda_available": torch.cuda.is_available(),
                "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU",
            }

        if action == "transcribe":
            container_path = req.get("container_path", "")
            audio_b64 = req.get("audio_b64", "")
            language = req.get("language", "auto")

            target_wav = None
            temp_file = None

            try:
                # 1. 尝试直接从宿主机挂载目录寻找文件（零拷贝极速模式）
                if container_path and container_path.startswith(CONTAINER_MOUNT_PREFIX):
                    rel = container_path[len(CONTAINER_MOUNT_PREFIX):].lstrip("/")
                    host_candidate = os.path.join(HOST_MOUNT_PREFIX, rel)
                    if os.path.exists(host_candidate):
                        target_wav = host_candidate
                        logger.info(f"Direct disk access for audio: {target_wav}")

                # 2. 如果直接路径不存在或不可达，使用传入的 base64 音频
                if not target_wav:
                    if not audio_b64:
                        return {"status": "error", "error": "No valid container_path or audio_b64 provided"}

                    audio_bytes = base64.b64decode(audio_b64)
                    temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                    temp_file.write(audio_bytes)
                    temp_file.flush()
                    temp_file.close()
                    target_wav = temp_file.name

                t0 = time.time()
                # 3. 推理加锁保证多并发单卡稳健性
                async with self.inference_lock:
                    loop = asyncio.get_running_loop()
                    res = await loop.run_in_executor(
                        None,
                        lambda: self.model.generate(input=target_wav, language=language, use_itn=True),
                    )

                elapsed_ms = int((time.time() - t0) * 1000)

                if not res or not isinstance(res, list):
                    return {"status": "error", "error": "Empty model output"}

                raw_text = res[0].get("text", "")
                clean_text = rich_transcription_postprocess(raw_text).strip()

                logger.info(f"ASR complete in {elapsed_ms}ms on {self.device}: '{clean_text}'")
                return {
                    "status": "ok",
                    "text": clean_text,
                    "raw_text": raw_text,
                    "device": self.device,
                    "elapsed_ms": elapsed_ms,
                }

            except Exception as e:
                logger.error(f"Inference exception: {e}", exc_info=True)
                return {"status": "error", "error": str(e)}
            finally:
                if temp_file and os.path.exists(temp_file.name):
                    try:
                        os.remove(temp_file.name)
                    except OSError:
                        pass

        return {"status": "error", "error": f"Unknown action: {action}"}

    async def start(self) -> None:
        self.load_model()

        loop = asyncio.get_running_loop()

        # 启动 UNIX Domain Socket 服务
        if os.path.exists(UNIX_SOCKET_PATH):
            try:
                os.remove(UNIX_SOCKET_PATH)
            except OSError:
                pass

        Path(UNIX_SOCKET_PATH).parent.mkdir(parents=True, exist_ok=True)
        unix_server = await asyncio.start_unix_server(self.handle_client, path=UNIX_SOCKET_PATH)
        # 赋予读写权限，使 Docker 容器内的 bot 拥有访问权限
        os.chmod(UNIX_SOCKET_PATH, 0o666)
        self.servers.append(unix_server)
        logger.info(f"UNIX domain socket server listening at {UNIX_SOCKET_PATH}")

        # 启动 TCP Socket 服务
        tcp_server = await asyncio.start_server(self.handle_client, host=TCP_HOST, port=TCP_PORT)
        self.servers.append(tcp_server)
        logger.info(f"TCP server listening at {TCP_HOST}:{TCP_PORT}")

        stop_event = asyncio.Event()

        def _signal_handler():
            logger.info("Received termination signal, shutting down FunASR server...")
            self._running = False
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                pass

        await stop_event.wait()

        logger.info("Closing socket servers...")
        for s in self.servers:
            s.close()
            await s.wait_closed()

        if os.path.exists(UNIX_SOCKET_PATH):
            try:
                os.remove(UNIX_SOCKET_PATH)
            except OSError:
                pass
        logger.info("FunASR server terminated gracefully.")


def main():
    server = FunASRServer()
    asyncio.run(server.start())


if __name__ == "__main__":
    main()
