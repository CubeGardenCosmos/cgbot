#!/usr/bin/env python3
"""
Host SoVITS IPC Daemon
Architecture mirrored from FunASR Host Service (UNIX domain socket + TCP fallback)
Provides low-latency IPC interface between Host GPU and the bot's Docker container.
"""

import asyncio
import base64
import importlib.machinery
import json
import logging
import os
import signal
import sys
import time
import types
import uuid
from pathlib import Path
from typing import Any

import soundfile as sf
import torch
from torch import nn

# Include local core paths
SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.join(SERVICE_DIR, "core")
sys.path.insert(0, CORE_DIR)

# 1. Clean Mocks for optional non-inference modules to keep daemon lean & stable
b_mock = types.ModuleType("BigVGAN")
b_sub = types.ModuleType("BigVGAN.bigvgan")
b_sub.BigVGAN = None
b_mock.bigvgan = b_sub
b_mock.__spec__ = importlib.machinery.ModuleSpec("BigVGAN", None)
sys.modules["BigVGAN"] = b_mock
sys.modules["BigVGAN.bigvgan"] = b_sub

f5_mock = types.ModuleType("f5_tts")
f5_model_mock = types.ModuleType("f5_tts.model")
f5_model_mock.DiT = None
f5_mock.model = f5_model_mock
f5_mock.__spec__ = importlib.machinery.ModuleSpec("f5_tts", None)
sys.modules["f5_tts"] = f5_mock
sys.modules["f5_tts.model"] = f5_model_mock

tm_mock = types.ModuleType("torchmetrics")
tm_cls_mock = types.ModuleType("torchmetrics.classification")
tm_cls_mock.MulticlassAccuracy = lambda *a, **k: nn.Identity()
tm_mock.classification = tm_cls_mock
tm_mock.__spec__ = importlib.machinery.ModuleSpec("torchmetrics", None)
sys.modules["torchmetrics"] = tm_mock
sys.modules["torchmetrics.classification"] = tm_cls_mock

pl_mock = types.ModuleType("pytorch_lightning")
pl_mock.LightningModule = torch.nn.Module
pl_mock.__spec__ = importlib.machinery.ModuleSpec("pytorch_lightning", None)
sys.modules["pytorch_lightning"] = pl_mock

peft_mock = types.ModuleType("peft")
peft_mock.LoraConfig = lambda *a, **k: None
peft_mock.get_peft_model = lambda *a, **k: None
peft_mock.__spec__ = importlib.machinery.ModuleSpec("peft", None)
sys.modules["peft"] = peft_mock

mat_mock = types.ModuleType("matplotlib")
mat_mock.__spec__ = importlib.machinery.ModuleSpec("matplotlib", None)
sys.modules["matplotlib"] = mat_mock
sys.modules["matplotlib.pyplot"] = types.ModuleType("matplotlib.pyplot")

# 2. Import Community TTS Core
from TTS_infer_pack.TTS import TTS, TTS_Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [HostSoVITS] %(message)s",
)
logger = logging.getLogger("HostSoVITS")

# Configurations（均可由环境变量覆盖；默认值见 voice-services/README.md）
DEFAULT_CHARACTER = os.environ.get("CGBOT_SOVITS_CHARACTER", "default")
MODEL_DIR = os.environ.get("CGBOT_SOVITS_MODEL_DIR", os.path.join(SERVICE_DIR, "models/default"))
SOVITS_WEIGHTS_PATH = os.path.join(MODEL_DIR, "sovits.pth")
GPT_WEIGHTS_PATH = os.path.join(MODEL_DIR, "gpt.ckpt")
REF_WAV_PATH = os.path.join(MODEL_DIR, "ref.wav")
REF_PROMPT_TEXT = os.environ.get("CGBOT_SOVITS_REF_PROMPT_TEXT", "")
REF_PROMPT_LANG = os.environ.get("CGBOT_SOVITS_REF_PROMPT_LANG", "zh")

HUBERT_DIR = os.path.join(CORE_DIR, "GPT_SoVITS/pretrained_models/chinese-hubert-base")
BERT_DIR = os.path.join(CORE_DIR, "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large")

DATA_DIR = os.environ.get("CGBOT_VOICE_DATA_DIR", "/var/lib/cgbot-voice")
UNIX_SOCKET_PATH = os.environ.get("CGBOT_SOVITS_SOCKET", os.path.join(DATA_DIR, "sovits.sock"))
CONTAINER_MOUNT_PREFIX = os.environ.get("CGBOT_VOICE_CONTAINER_PREFIX", "/data")
HOST_MOUNT_PREFIX = DATA_DIR
TCP_HOST = os.environ.get("CGBOT_SOVITS_HOST", "127.0.0.1")
TCP_PORT = int(os.environ.get("CGBOT_SOVITS_PORT", "10096"))


class SoVITSServer:
    def __init__(self) -> None:
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self.is_half = (self.device != "cpu")
        self.character = DEFAULT_CHARACTER
        self.tts: TTS | None = None
        self.inference_lock = asyncio.Lock()
        self.servers: list[asyncio.Server] = []
        self._running = True

    def load_models(self) -> None:
        logger.info(f"Loading SoVITS community pipeline on {self.device} (is_half={self.is_half})...")
        t0 = time.time()

        custom_config = {
            "custom": {
                "device": self.device,
                "is_half": self.is_half,
                "version": "v2",
                "t2s_weights_path": GPT_WEIGHTS_PATH,
                "vits_weights_path": SOVITS_WEIGHTS_PATH,
                "cnhuhbert_base_path": HUBERT_DIR,
                "bert_base_path": BERT_DIR,
            }
        }

        tts_config = TTS_Config(custom_config)
        self.tts = TTS(tts_config)

        # Pre-cache reference audio prompt to avoid redundant disk I/O on inference
        if os.path.exists(REF_WAV_PATH):
            logger.info(f"Pre-caching reference audio prompt from: {REF_WAV_PATH}")
            self.tts.set_ref_audio(REF_WAV_PATH)

        vram_alloc = torch.cuda.memory_allocated(0) / (1024**2) if torch.cuda.is_available() else 0
        free_vram, total_vram = torch.cuda.mem_get_info(0) if torch.cuda.is_available() else (0, 0)
        logger.info(
            f"Successfully initialized {self.character} SoVITS pipeline in {time.time() - t0:.2f}s! "
            f"GPU Alloc: {vram_alloc:.1f}MB, Free: {free_vram / 1024**2:.1f}MB / {total_vram / 1024**2:.1f}MB"
        )

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client_addr = writer.get_extra_info("peername") or "unix_socket"
        logger.debug(f"New IPC connection: {client_addr}")
        try:
            while self._running:
                try:
                    raw_len = await reader.readexactly(4)
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    break

                length = int.from_bytes(raw_len, byteorder="big")
                if length <= 0 or length > 50 * 1024 * 1024:
                    logger.warning(f"Invalid frame length: {length}")
                    break

                raw_payload = await reader.readexactly(length)
                try:
                    req = json.loads(raw_payload.decode("utf-8"))
                except Exception as e:
                    logger.error(f"Malformed JSON: {e}")
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
        action = req.get("action", "ping")

        if action in ("ping", "health"):
            free_vram, total_vram = (
                torch.cuda.mem_get_info(0) if torch.cuda.is_available() else (0, 0)
            )
            vram_alloc = torch.cuda.memory_allocated(0) if torch.cuda.is_available() else 0
            return {
                "status": "ok",
                "service": "HostSoVITS",
                "character": self.character,
                "device": self.device,
                "is_half": self.is_half,
                "cuda_available": torch.cuda.is_available(),
                "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU",
                "vram_allocated_mb": round(vram_alloc / (1024**2), 2),
                "vram_free_mb": round(free_vram / (1024**2), 2),
                "vram_total_mb": round(total_vram / (1024**2), 2),
                "models": {
                    "sovits": os.path.basename(SOVITS_WEIGHTS_PATH),
                    "gpt": os.path.basename(GPT_WEIGHTS_PATH),
                },
            }

        if action == "status":
            return {
                "status": "ok",
                "character": self.character,
                "tts_loaded": self.tts is not None,
                "device": self.device,
                "unix_socket": UNIX_SOCKET_PATH,
                "tcp_endpoint": f"{TCP_HOST}:{TCP_PORT}",
            }

        if action in ("tts", "synthesize"):
            text = req.get("text", "").strip()
            target_char = req.get("character", self.character)
            language = req.get("language", "zh")

            if not text:
                return {"status": "error", "error": "Text is empty"}

            if self.tts is None:
                return {"status": "error", "error": "TTS engine not initialized"}

            t0 = time.time()
            try:
                loop = asyncio.get_running_loop()

                infer_req = {
                    "text": text,
                    "text_lang": language,
                    "ref_audio_path": REF_WAV_PATH,
                    "prompt_text": REF_PROMPT_TEXT,
                    "prompt_lang": REF_PROMPT_LANG,
                    "top_k": 20,
                    "top_p": 0.6,
                    "temperature": 0.6,
                    "text_split_method": "cut0",
                    "batch_size": 1,
                    "speed_factor": 1.0,
                }

                def _infer():
                    gen = self.tts.run(infer_req)
                    sr, audio = next(gen)
                    return sr, audio

                async with self.inference_lock:
                    sr, audio_data = await loop.run_in_executor(None, _infer)

                # Write to shared temp directory (accessible by container via mount)
                temp_dir = os.path.join(HOST_MOUNT_PREFIX, "temp")
                os.makedirs(temp_dir, exist_ok=True)
                filename = f"tts_{uuid.uuid4().hex[:12]}.wav"
                host_audio_path = os.path.join(temp_dir, filename)
                container_audio_path = f"{CONTAINER_MOUNT_PREFIX}/temp/{filename}"

                sf.write(host_audio_path, audio_data, sr)
                os.chmod(host_audio_path, 0o666)

                elapsed_ms = int((time.time() - t0) * 1000)
                duration = len(audio_data) / sr

                logger.info(
                    f"TTS synthesis complete in {elapsed_ms}ms ({duration:.2f}s audio): '{text[:30]}...' -> {host_audio_path}"
                )

                return {
                    "status": "ok",
                    "container_path": container_audio_path,
                    "host_path": host_audio_path,
                    "sampling_rate": sr,
                    "duration_sec": round(duration, 2),
                    "character": target_char,
                    "elapsed_ms": elapsed_ms,
                    "device": self.device,
                }

            except Exception as e:
                logger.error(f"Inference exception: {e}", exc_info=True)
                return {"status": "error", "error": str(e)}

        return {"status": "error", "error": f"Unknown action: {action}"}

    async def start(self) -> None:
        self.load_models()
        loop = asyncio.get_running_loop()

        # UNIX Domain Socket
        if os.path.exists(UNIX_SOCKET_PATH):
            try:
                os.remove(UNIX_SOCKET_PATH)
            except OSError:
                pass

        Path(UNIX_SOCKET_PATH).parent.mkdir(parents=True, exist_ok=True)
        unix_server = await asyncio.start_unix_server(self.handle_client, path=UNIX_SOCKET_PATH)
        os.chmod(UNIX_SOCKET_PATH, 0o666)
        self.servers.append(unix_server)
        logger.info(f"UNIX domain socket server listening at {UNIX_SOCKET_PATH}")

        # TCP Server Fallback
        tcp_server = await asyncio.start_server(self.handle_client, host=TCP_HOST, port=TCP_PORT)
        self.servers.append(tcp_server)
        logger.info(f"TCP server listening at {TCP_HOST}:{TCP_PORT}")

        stop_event = asyncio.Event()

        def _signal_handler():
            logger.info("Termination signal received, shutting down SoVITS server...")
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
        logger.info("Host SoVITS daemon stopped gracefully.")


def main():
    server = SoVITSServer()
    asyncio.run(server.start())


if __name__ == "__main__":
    main()
