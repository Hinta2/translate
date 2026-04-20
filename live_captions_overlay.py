from __future__ import annotations

import asyncio
import difflib
import json
import os
import queue
import re
import textwrap
import threading
import tkinter as tk
from collections import deque

import argostranslate.translate
import sounddevice as sd
import websockets


# ================= CONFIG =================
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
DG_MODEL = os.getenv("DEEPGRAM_MODEL", "nova-2")

# loopback = يسمع صوت الجهاز (يوتيوب/جوجل/الهيدسيت)
# mic = يسمع من المايك
AUDIO_SOURCE_MODE = os.getenv("AUDIO_SOURCE_MODE", "loopback").lower()

CHANNELS = 1
BLOCKSIZE = 2048
MAX_VISIBLE_SENTENCES = 6
MAX_CHARS_PER_LINE = 68


audio_q: queue.Queue[bytes] = queue.Queue(maxsize=64)
shared_samplerate = 48000
last_final = ""


# ================= AUDIO =================
def _pick_microphone_device() -> int | None:
    for i, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            return i
    return None


def _pick_wasapi_output_device() -> int | None:
    """Pick default WASAPI output (speaker/headset) for system loopback capture on Windows."""
    try:
        hostapis = sd.query_hostapis()
        for host_idx, host in enumerate(hostapis):
            if "wasapi" not in host.get("name", "").lower():
                continue

            default_out = host.get("default_output_device", -1)
            if isinstance(default_out, int) and default_out >= 0:
                return default_out

            for dev_idx in host.get("devices", []):
                dev = sd.query_devices(dev_idx)
                if dev.get("max_output_channels", 0) > 0:
                    return int(dev_idx)
    except Exception:
        return None
    return None


def _pick_matching_loopback_input(output_device: int) -> int | None:
    """
    Detect a usable *input* device that captures system playback audio.

    This avoids depending on `loopback=True` / `WasapiSettings(loopback=...)`
    because many Windows builds of sounddevice don't expose those arguments.
    """
    try:
        output_info = sd.query_devices(output_device)
        output_name = str(output_info.get("name", "")).lower()
        output_hostapi = output_info.get("hostapi")

        keywords = (
            "loopback",
            "stereo mix",
            "what u hear",
            "wave out",
            "monitor",
            "mix",
        )

        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            if output_hostapi is not None and dev.get("hostapi") != output_hostapi:
                continue

            name = str(dev.get("name", "")).lower()
            output_hint = output_name.split("(")[0].strip()
            has_keyword = any(k in name for k in keywords)
            is_related_to_output = bool(output_hint and output_hint in name)
            if has_keyword or is_related_to_output:
                return idx
    except Exception:
        return None
    return None


def start_audio_capture() -> None:
    """Start audio capture in background thread (mic or system loopback)."""
    global shared_samplerate

    stream_kwargs = dict(
        blocksize=BLOCKSIZE,
        channels=CHANNELS,
        dtype="int16",
        callback=lambda indata, frames, time_info, status: _push_audio(bytes(indata), status),
    )

    preferred_device: int | None = None
    preferred_label = ""

    if AUDIO_SOURCE_MODE == "loopback":
        output_device = _pick_wasapi_output_device()
        if output_device is not None:
            preferred_device = _pick_matching_loopback_input(output_device)
            if preferred_device is not None:
                preferred_label = "SYSTEM audio"

    # If loopback wasn't found, auto-fallback to mic so the app still runs.
    if preferred_device is None:
        mic_device = _pick_microphone_device()
        if mic_device is None:
            raise RuntimeError("No usable input device found (loopback or microphone).")
        preferred_device = mic_device
        preferred_label = "MIC audio"
        if AUDIO_SOURCE_MODE == "loopback":
            print("⚠️ No loopback-capable input detected. Falling back to microphone.")

    info = sd.query_devices(preferred_device, "input")
    shared_samplerate = int(info.get("default_samplerate", 48000))
    print(f"🎧 Capturing {preferred_label} from: {info.get('name', preferred_device)}")

    with sd.RawInputStream(
        samplerate=shared_samplerate,
        device=preferred_device,
        **stream_kwargs,
    ):
        while True:
            sd.sleep(1000)


def _push_audio(packet: bytes, status) -> None:
    if status:
        # status warnings are expected occasionally; no crash needed.
        pass
    try:
        audio_q.put_nowait(packet)
    except queue.Full:
        # Drop oldest packet to keep latency low.
        try:
            audio_q.get_nowait()
        except queue.Empty:
            return
        try:
            audio_q.put_nowait(packet)
        except queue.Full:
            return


# ================= TRANSLATION =================
def translate_en_to_ar(text: str) -> str:
    try:
        return argostranslate.translate.translate(text, "en", "ar")
    except Exception:
        return ""


# ================= UI =================
class Overlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Live Caption Overlay")
        self.root.geometry("1120x360+120+640")
        self.root.configure(bg="#050505")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.86)

        self.sentences: deque[str] = deque(maxlen=MAX_VISIBLE_SENTENCES)

        self.title = tk.Label(
            self.root,
            text="Live Arabic Captions",
            fg="#67e8f9",
            bg="#050505",
            font=("Segoe UI", 13, "bold"),
            anchor="w",
            padx=16,
            pady=8,
        )
        self.title.pack(fill="x")

        self.label = tk.Label(
            self.root,
            text="Waiting for speech...",
            fg="white",
            bg="#050505",
            font=("Segoe UI", 21, "bold"),
            justify="left",
            anchor="nw",
            wraplength=1080,
            padx=18,
            pady=10,
        )
        self.label.pack(expand=True, fill="both")

    def add_sentence(self, text: str) -> None:
        text = self._clean_text(text)
        if not text:
            return

        self.sentences.append(text)
        rendered = self._render_text()
        self.root.after(0, self.label.config, {"text": rendered})

    def _render_text(self) -> str:
        blocks: list[str] = []
        for sentence in self.sentences:
            blocks.append(textwrap.fill(sentence, width=MAX_CHARS_PER_LINE))
        return "\n\n".join(blocks)

    @staticmethod
    def _clean_text(text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"([.!؟،])\1+", r"\1", text)
        return text

    def run(self) -> None:
        self.root.mainloop()


# ================= DEEPGRAM =================
async def deepgram_loop(overlay: Overlay) -> None:
    global last_final, shared_samplerate

    if not DEEPGRAM_API_KEY:
        raise RuntimeError("Missing DEEPGRAM_API_KEY environment variable.")

    url = (
        "wss://api.deepgram.com/v1/listen"
        f"?model={DG_MODEL}"
        "&language=en"
        "&interim_results=true"
        "&punctuate=true"
        "&encoding=linear16"
        f"&sample_rate={shared_samplerate}"
        "&channels=1"
    )

    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}

    async with websockets.connect(url, extra_headers=headers, max_size=2**22) as ws:
        async def sender() -> None:
            while True:
                data = await asyncio.to_thread(audio_q.get)
                await ws.send(data)

        async def receiver() -> None:
            global last_final
            while True:
                msg = await ws.recv()
                payload = json.loads(msg)

                alternatives = payload.get("channel", {}).get("alternatives", [])
                if not alternatives:
                    continue

                text = alternatives[0].get("transcript", "").strip()
                is_final = bool(payload.get("is_final", False))
                if not text or not is_final:
                    continue

                # منع التكرار الناتج من إعادة الصياغة في STT
                if last_final and difflib.SequenceMatcher(None, text, last_final).ratio() > 0.9:
                    continue
                last_final = text

                print("🟡 EN:", text)
                translated = translate_en_to_ar(text)
                overlay.add_sentence(translated or text)

        await asyncio.gather(sender(), receiver())


def start_ws(overlay: Overlay) -> None:
    asyncio.run(deepgram_loop(overlay))


# ================= MAIN =================
def main() -> None:
    overlay = Overlay()

    threading.Thread(target=start_audio_capture, daemon=True).start()
    threading.Thread(target=start_ws, args=(overlay,), daemon=True).start()

    overlay.run()


if __name__ == "__main__":
    main()
