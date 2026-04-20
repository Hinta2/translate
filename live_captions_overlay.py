"""
Live System-Audio Captions -> Arabic Overlay (Windows)

What it does:
- Captures any audio playing on your computer (WASAPI loopback).
- Streams audio to Deepgram in real-time for fast English transcription.
- Translates English transcript to Arabic.
- Displays Arabic text in a sleek, transparent, always-on-top overlay.

Hotkeys:
- Ctrl+Shift+T : Toggle overlay visibility
- Ctrl+Shift+C : Clear text
- Ctrl+Shift+= : Increase size
- Ctrl+Shift+- : Decrease size

Environment variables:
- DEEPGRAM_API_KEY (required)
- DG_MODEL (optional, default: nova-2)
- CAPTION_ALPHA (optional, default: 0.72)

Dependencies:
    pip install sounddevice websockets keyboard deep-translator pywin32
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from collections import OrderedDict, deque
from dataclasses import dataclass
from tkinter import messagebox
from urllib.parse import urlencode

import sounddevice as sd
import websockets
from deep_translator import GoogleTranslator

try:
    import keyboard
except Exception:
    keyboard = None


# ----------------------------- Configuration ---------------------------------
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "").strip()
DEEPGRAM_MODEL = os.getenv("DG_MODEL", "nova-2")
CAPTION_ALPHA = float(os.getenv("CAPTION_ALPHA", "0.72"))
PUNCT_RE = re.compile(r"\s+")

OVERLAY_BG = "#050505"
OVERLAY_FG = "#F7F7F7"
OVERLAY_BORDER = "#2F2F2F"
BASE_WIDTH = 1050
BASE_HEIGHT = 280
BASE_FONT_SIZE = 30
MIN_FONT_SIZE = 16
MAX_FONT_SIZE = 54
WRAP_MARGIN = 40

MAX_LINES = 8
MAX_CACHE_SIZE = 500
TRANSLATE_MIN_LEN = 1
UI_POLL_MS = 70


# ----------------------------- Data structures --------------------------------
@dataclass
class TranscriptEvent:
    text: str
    is_final: bool
    ts: float


class LRUCache:
    def __init__(self, max_size: int = 500):
        self.max_size = max_size
        self._store: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            if key not in self._store:
                return None
            self._store.move_to_end(key)
            return self._store[key]

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            if len(self._store) > self.max_size:
                self._store.popitem(last=False)


# ------------------------------ Audio capture ---------------------------------
class LoopbackAudioSource:
    """Capture system output audio via Windows WASAPI loopback."""

    def __init__(self):
        self.stream: sd.RawInputStream | None = None
        self.device_index, self.device = self._pick_loopback_device()
        self.samplerate = int(self.device.get("default_samplerate", 48000) or 48000)
        self.channels = min(2, max(1, int(self.device.get("max_output_channels", 2) or 2)))

    @staticmethod
    def _pick_loopback_device() -> tuple[int, dict]:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()

        wasapi_index = None
        for idx, api in enumerate(hostapis):
            if "WASAPI" in api["name"].upper():
                wasapi_index = idx
                break

        if wasapi_index is None:
            raise RuntimeError("WASAPI host API not found. Windows system-audio capture requires WASAPI.")

        default_out = sd.default.device[1]
        if default_out is not None and default_out >= 0:
            d = dict(devices[default_out])
            if d.get("hostapi") == wasapi_index and d.get("max_output_channels", 0) > 0:
                return int(default_out), d

        for idx, d in enumerate(devices):
            if d.get("hostapi") == wasapi_index and d.get("max_output_channels", 0) > 0:
                return idx, dict(d)

        raise RuntimeError("No suitable WASAPI output device found for loopback capture.")

    def open(self, callback):
        wasapi = sd.WasapiSettings(loopback=True)
        # Important: use the output-device index (not input/mic) with loopback=True.
        # Passing a name string can resolve ambiguously on some machines and may capture mic.
        self.stream = sd.RawInputStream(
            samplerate=self.samplerate,
            blocksize=0,
            device=self.device_index,
            channels=self.channels,
            dtype="int16",
            latency="low",
            extra_settings=wasapi,
            callback=callback,
        )
        self.stream.start()

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None


# ------------------------------ Deepgram client -------------------------------
class DeepgramStreamer:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self.audio_source = LoopbackAudioSource()
        self.audio_queue: asyncio.Queue[bytes] | None = None
        self.stop_event = threading.Event()

    def _build_ws_url(self) -> str:
        params = {
            "model": self.model,
            "language": "en-US",
            "smart_format": "true",
            "interim_results": "true",
            "endpointing": "300",
            "punctuate": "true",
            "encoding": "linear16",
            "channels": str(self.audio_source.channels),
            "sample_rate": str(self.audio_source.samplerate),
        }
        return f"wss://api.deepgram.com/v1/listen?{urlencode(params)}"

    async def run(self, transcript_cb, warning_cb):
        self.audio_queue = asyncio.Queue(maxsize=60)
        ws_url = self._build_ws_url()

        def on_audio(indata, _frames, _time_info, status):
            if status:
                warning_cb(f"Audio status: {status}")
            if self.audio_queue is None:
                return
            chunk = bytes(indata)
            try:
                self.audio_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass

        self.audio_source.open(on_audio)

        headers = {"Authorization": f"Token {self.api_key}"}
        try:
            async with websockets.connect(ws_url, additional_headers=headers, max_size=4_000_000) as ws:
                sender = asyncio.create_task(self._send_audio(ws))
                receiver = asyncio.create_task(self._recv_transcripts(ws, transcript_cb, warning_cb))
                done, pending = await asyncio.wait(
                    [sender, receiver], return_when=asyncio.FIRST_EXCEPTION
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    exc = task.exception()
                    if exc:
                        raise exc
        finally:
            self.audio_source.close()

    async def _send_audio(self, ws):
        assert self.audio_queue is not None
        while not self.stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(self.audio_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            await ws.send(chunk)

    async def _recv_transcripts(self, ws, transcript_cb, warning_cb):
        while not self.stop_event.is_set():
            raw = await ws.recv()
            if isinstance(raw, bytes):
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") == "Results":
                channel = msg.get("channel", {})
                alts = channel.get("alternatives", [])
                if not alts:
                    continue

                text = (alts[0].get("transcript") or "").strip()
                if not text:
                    continue

                is_final = bool(msg.get("is_final", False))
                transcript_cb(TranscriptEvent(text=text, is_final=is_final, ts=time.time()))
            elif msg.get("type") == "Metadata":
                continue
            elif msg.get("type") == "Warning":
                warning_cb(msg.get("description", "Deepgram warning."))


# ------------------------------ Translator ------------------------------------
class ArabicTranslator:
    """Fast translation with a tiny cache, optimized for streaming text."""

    def __init__(self):
        self.cache = LRUCache(MAX_CACHE_SIZE)
        self.translator = GoogleTranslator(source="en", target="ar")

    @staticmethod
    def normalize(text: str) -> str:
        return PUNCT_RE.sub(" ", text).strip()

    def to_arabic(self, text: str) -> str:
        cleaned = self.normalize(text)
        if len(cleaned) < TRANSLATE_MIN_LEN:
            return ""

        cached = self.cache.get(cleaned)
        if cached is not None:
            return cached

        result = self.translator.translate(cleaned) or ""
        result = result.strip()
        if result:
            self.cache.set(cleaned, result)
        return result


# ------------------------------ Overlay UI ------------------------------------
class OverlayApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Live Arabic Captions Overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", CAPTION_ALPHA)
        self.root.configure(bg=OVERLAY_BORDER)
        self.root.geometry(f"{BASE_WIDTH}x{BASE_HEIGHT}+160+760")

        self.font_size = BASE_FONT_SIZE
        self.overlay_visible = True
        self.click_through = False

        self._drag_x = 0
        self._drag_y = 0

        self.stop_event = threading.Event()
        self.transcript_queue: queue.Queue[TranscriptEvent] = queue.Queue(maxsize=160)
        self.ui_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=160)
        self.lines = deque(maxlen=MAX_LINES)

        self.translator = ArabicTranslator()
        self.last_rendered_text = ""

        self.deepgram = DeepgramStreamer(DEEPGRAM_API_KEY, DEEPGRAM_MODEL) if DEEPGRAM_API_KEY else None

        # outer frame for comfy styling
        self.frame = tk.Frame(self.root, bg=OVERLAY_BG, highlightbackground=OVERLAY_BORDER, highlightthickness=2)
        self.frame.pack(fill="both", expand=True, padx=2, pady=2)

        self.text_var = tk.StringVar(value="جاهز. شغّل أي صوت إنجليزي وسأعرض الترجمة هنا.")
        self.label = tk.Label(
            self.frame,
            textvariable=self.text_var,
            bg=OVERLAY_BG,
            fg=OVERLAY_FG,
            font=("Segoe UI", self.font_size, "bold"),
            justify="right",
            anchor="se",
            padx=18,
            pady=14,
            wraplength=BASE_WIDTH - WRAP_MARGIN,
        )
        self.label.pack(fill="both", expand=True)

        self._bind_events()
        self._register_global_hotkeys()
        self._set_click_through(False)
        self._start_threads()
        self._poll_ui_queue()

    def _bind_events(self):
        self.label.bind("<ButtonPress-1>", self._start_drag)
        self.label.bind("<B1-Motion>", self._do_drag)
        self.label.bind("<Button-3>", self._open_menu)
        self.label.bind("<Control-MouseWheel>", self._on_ctrl_wheel)

        self.root.bind_all("<F8>", lambda _e: self.toggle_overlay())
        self.root.bind_all("<F9>", lambda _e: self.clear_text())
        self.root.bind_all("<Control-plus>", lambda _e: self.increase_size())
        self.root.bind_all("<Control-minus>", lambda _e: self.decrease_size())

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="تكبير", command=self.increase_size)
        self.menu.add_command(label="تصغير", command=self.decrease_size)
        self.menu.add_command(label="مسح النص", command=self.clear_text)
        self.menu.add_separator()
        self.menu.add_command(label="إخفاء / إظهار", command=self.toggle_overlay)
        self.menu.add_command(label="Click-through", command=self.toggle_click_through)
        self.menu.add_separator()
        self.menu.add_command(label="خروج", command=self.shutdown)

        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

    # ---------------- UI actions ----------------
    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_drag(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _open_menu(self, event):
        self.menu.tk_popup(event.x_root, event.y_root)

    def _on_ctrl_wheel(self, event):
        if event.delta > 0:
            self.increase_size()
        else:
            self.decrease_size()

    def increase_size(self):
        self._resize(+2)

    def decrease_size(self):
        self._resize(-2)

    def _resize(self, delta: int):
        self.font_size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, self.font_size + delta))
        width = BASE_WIDTH + (self.font_size - BASE_FONT_SIZE) * 14
        height = BASE_HEIGHT + (self.font_size - BASE_FONT_SIZE) * 5
        width = max(740, min(1460, width))
        height = max(180, min(520, height))
        self.root.geometry(f"{width}x{height}+{self.root.winfo_x()}+{self.root.winfo_y()}")
        self.label.configure(font=("Segoe UI", self.font_size, "bold"), wraplength=width - WRAP_MARGIN)

    def append_line(self, text: str):
        if not text:
            return
        if text == self.last_rendered_text:
            return
        self.lines.append(text)
        self.last_rendered_text = text
        self.text_var.set("\n".join(self.lines))

    def clear_text(self):
        self.lines.clear()
        self.last_rendered_text = ""
        self.text_var.set("")

    def toggle_overlay(self):
        if self.overlay_visible:
            self.root.withdraw()
            self.overlay_visible = False
        else:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.overlay_visible = True

    def toggle_click_through(self):
        self.click_through = not self.click_through
        self._set_click_through(self.click_through)

    def _set_click_through(self, enabled: bool):
        try:
            hwnd = self.root.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x20
            WS_EX_LAYERED = 0x80000

            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if enabled:
                style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
            else:
                style = style & ~WS_EX_TRANSPARENT
                style |= WS_EX_LAYERED
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    # ---------------- workers ----------------
    def _register_global_hotkeys(self):
        if keyboard is None:
            return

        def _safe_register(hotkey: str, callback):
            try:
                keyboard.add_hotkey(hotkey, callback)
            except Exception:
                pass

        _safe_register("ctrl+shift+t", self.toggle_overlay)
        _safe_register("ctrl+shift+c", self.clear_text)
        _safe_register("ctrl+shift+=", self.increase_size)
        _safe_register("ctrl+shift+-", self.decrease_size)

    def _start_threads(self):
        threading.Thread(target=self._translator_loop, daemon=True).start()
        if self.deepgram is None:
            self.ui_queue.put(("warning", "DEEPGRAM_API_KEY غير موجود. ضعه ثم أعد التشغيل."))
            return
        threading.Thread(target=self._deepgram_thread, daemon=True).start()

    def _deepgram_thread(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def on_transcript(event: TranscriptEvent):
            if event.is_final:
                try:
                    self.transcript_queue.put_nowait(event)
                except queue.Full:
                    pass

        def on_warning(msg: str):
            try:
                self.ui_queue.put_nowait(("warning", msg))
            except queue.Full:
                pass

        try:
            loop.run_until_complete(self.deepgram.run(on_transcript, on_warning))
        except Exception as exc:
            self.ui_queue.put(("warning", f"Deepgram error: {exc}"))
        finally:
            loop.stop()
            loop.close()

    def _translator_loop(self):
        while not self.stop_event.is_set():
            try:
                event = self.transcript_queue.get(timeout=0.25)
            except queue.Empty:
                continue

            try:
                translated = self.translator.to_arabic(event.text)
                if translated:
                    self.ui_queue.put(("append", translated))
            except Exception as exc:
                self.ui_queue.put(("warning", f"Translation error: {exc}"))

    def _poll_ui_queue(self):
        while True:
            try:
                action, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if action == "append":
                self.append_line(payload)
            elif action == "warning":
                if not self.lines:
                    self.text_var.set(payload)

        self.root.after(UI_POLL_MS, self._poll_ui_queue)

    def run(self):
        if not DEEPGRAM_API_KEY:
            messagebox.showwarning(
                "Missing API Key",
                "DEEPGRAM_API_KEY is not set. Add it and restart.",
            )
        self.root.mainloop()

    def shutdown(self):
        self.stop_event.set()
        if self.deepgram is not None:
            self.deepgram.stop_event.set()
        try:
            if keyboard is not None:
                keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    app = OverlayApp()
    app.run()
