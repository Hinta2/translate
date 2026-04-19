"""
Google Chrome Live Caption -> Arabic Overlay Translator (Windows 10/11)

Features:
- Captures the Google Chrome window and OCRs the lower-center region (where Chrome Live Caption appears).
- Uses pytesseract + Pillow + mss for OCR capture.
- Detects newly appearing caption segments and only translates new content.
- Translates English captions to Arabic using OpenAI API.
- Displays Arabic text in a draggable, transparent, always-on-top overlay.
- Global hotkeys:
    Ctrl+Shift+T -> Toggle overlay visibility
    Ctrl+Shift+C -> Clear translated text
- Starts listening automatically on launch.

Requirements:
    pip install openai keyboard pywin32 pytesseract pillow mss

Important:
- Install Tesseract OCR on Windows and make sure it is discoverable.
  Example path:
    C:\\Program Files\\Tesseract-OCR\\tesseract.exe
- Optional environment variable to force path:
    set TESSERACT_CMD=C:\\Program Files\\Tesseract-OCR\\tesseract.exe

Run:
    set OPENAI_API_KEY=your_key_here
    python live_captions_overlay.py
"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import threading
import time
import tkinter as tk
from collections import OrderedDict, deque
from dataclasses import dataclass
from tkinter import messagebox

try:
    import keyboard  # global hotkeys
except Exception:  # optional fallback
    keyboard = None

import mss
import pytesseract
import win32gui
from openai import OpenAI
from PIL import Image


# ----------------------------- Configuration ---------------------------------
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
CHROME_WINDOW_TITLE_KEYWORD = "Google Chrome"
POLL_INTERVAL_SECONDS = 0.5
MAX_CHARS_PER_REQUEST = 900
OVERLAY_ALPHA = 0.65
FONT = ("Segoe UI", 24, "bold")
MAX_CACHE_SIZE = 300

# OCR crop region in Chrome window (fractions of width/height)
# Lower-center area where Live Caption usually appears.
OCR_CROP_WIDTH_RATIO = 0.72
OCR_CROP_HEIGHT_RATIO = 0.28
OCR_CROP_CENTER_X = 0.5
OCR_CROP_CENTER_Y = 0.82


# ----------------------------- Data structures --------------------------------
@dataclass
class CaptionEvent:
    """Represents a newly detected caption segment."""

    text: str
    timestamp: float


class LRUCache:
    """Simple fixed-size LRU cache for recent translations."""

    def __init__(self, max_size: int = 300):
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


# ------------------------ Chrome OCR extraction -------------------------------
class ChromeLiveCaptionOCRReader:
    """Reads current caption-like text from Google Chrome window via OCR."""

    def __init__(self, title_keyword: str = CHROME_WINDOW_TITLE_KEYWORD):
        self.title_keyword = title_keyword.lower()

        tesseract_cmd = os.getenv("TESSERACT_CMD", "").strip()
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    @staticmethod
    def _normalize(text: str) -> str:
        text = text.replace("\u200f", " ").replace("\u200e", " ")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _find_chrome_window_rect(self) -> tuple[int, int, int, int] | None:
        windows: list[tuple[int, str, tuple[int, int, int, int]]] = []

        def enum_handler(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            if not title:
                return
            if self.title_keyword not in title.lower():
                return
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right - left < 300 or bottom - top < 200:
                return
            windows.append((hwnd, title, (left, top, right, bottom)))

        win32gui.EnumWindows(enum_handler, None)

        if not windows:
            return None

        try:
            fg = win32gui.GetForegroundWindow()
        except Exception:
            fg = 0

        # Prefer the focused Chrome window; fallback to largest visible one.
        for hwnd, _title, rect in windows:
            if hwnd == fg:
                return rect

        windows.sort(key=lambda item: (item[2][2] - item[2][0]) * (item[2][3] - item[2][1]), reverse=True)
        return windows[0][2]

    @staticmethod
    def _crop_live_caption_region(img: Image.Image) -> Image.Image:
        w, h = img.size

        crop_w = int(w * OCR_CROP_WIDTH_RATIO)
        crop_h = int(h * OCR_CROP_HEIGHT_RATIO)

        center_x = int(w * OCR_CROP_CENTER_X)
        center_y = int(h * OCR_CROP_CENTER_Y)

        left = max(0, center_x - crop_w // 2)
        upper = max(0, center_y - crop_h // 2)
        right = min(w, left + crop_w)
        lower = min(h, upper + crop_h)

        return img.crop((left, upper, right, lower))

    @staticmethod
    def _preprocess_for_ocr(img: Image.Image) -> Image.Image:
        # Convert to grayscale + increase contrast by thresholding to improve subtitle OCR.
        gray = img.convert("L")
        bw = gray.point(lambda x: 255 if x > 140 else 0, mode="1")
        return bw

    def get_current_text(self) -> str | None:
        """
        Returns OCR text from lower-center Google Chrome region.
        Returns None when Chrome window is not found or capture fails.
        """
        rect = self._find_chrome_window_rect()
        if not rect:
            return None

        left, top, right, bottom = rect
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None

        monitor = {"left": left, "top": top, "width": width, "height": height}

        try:
            with mss.mss() as sct:
                shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            region = self._crop_live_caption_region(img)
            prepared = self._preprocess_for_ocr(region)
            text = pytesseract.image_to_string(prepared, lang="eng", config="--oem 3 --psm 6")
            cleaned = self._normalize(text)
            return cleaned
        except Exception:
            return None


# ------------------------------ Translator ------------------------------------
class OpenAITranslator:
    """Translates English caption segments into Arabic."""

    def __init__(self, api_key: str, model: str = OPENAI_MODEL):
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def translate_to_arabic(self, text: str) -> str:
        """Translate text with low-latency settings."""
        response = self.client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Translate spoken English captions to clear Modern Standard Arabic. "
                        "Keep it concise, natural, and do not add explanations."
                    ),
                },
                {
                    "role": "user",
                    "content": text,
                },
            ],
            max_output_tokens=350,
            temperature=0.2,
        )
        return (response.output_text or "").strip()


# ------------------------------ Overlay UI ------------------------------------
class OverlayApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Arabic Live Captions Overlay")
        self.root.overrideredirect(True)  # borderless
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", OVERLAY_ALPHA)
        self.root.configure(bg="black")
        self.root.geometry("1000x260+180+740")

        # Draggable state
        self._drag_x = 0
        self._drag_y = 0

        # Threading and queues
        self.stop_event = threading.Event()
        self.capture_queue: queue.Queue[CaptionEvent] = queue.Queue(maxsize=120)
        self.ui_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=120)

        # Workers / state
        self.reader = ChromeLiveCaptionOCRReader()
        self.overlay_visible = True
        self.translation_cache = LRUCache(MAX_CACHE_SIZE)
        self.translated_lines = deque(maxlen=7)
        self.sent_segments = deque(maxlen=300)
        self.last_full_text = ""
        self.last_warning_time = 0.0

        # Translator setup
        self.translator: OpenAITranslator | None = None
        if OPENAI_API_KEY:
            try:
                self.translator = OpenAITranslator(OPENAI_API_KEY, OPENAI_MODEL)
            except Exception:
                self.translator = None

        # Main text label
        self.text_var = tk.StringVar(value="Starting listener...")
        self.label = tk.Label(
            self.root,
            textvariable=self.text_var,
            bg="black",
            fg="white",
            font=FONT,
            justify="left",
            anchor="sw",
            padx=22,
            pady=16,
            wraplength=960,
        )
        self.label.pack(fill="both", expand=True)

        # Mouse drag handlers
        self.label.bind("<ButtonPress-1>", self._start_drag)
        self.label.bind("<B1-Motion>", self._do_drag)

        # Right-click menu
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Clear text", command=self.clear_text)
        self.menu.add_command(label="Toggle overlay", command=self.toggle_overlay)
        self.menu.add_command(label="Toggle click-through", command=self.toggle_click_through)
        self.menu.add_separator()
        self.menu.add_command(label="Exit", command=self.shutdown)
        self.label.bind("<Button-3>", self._open_menu)

        # Fallback local hotkeys (work when app has focus)
        self.root.bind_all("<F8>", lambda _e: self.toggle_overlay())
        self.root.bind_all("<F9>", lambda _e: self.clear_text())

        # WM cleanup
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

        self.click_through = False
        self._set_click_through(False)
        self._register_global_hotkeys()

        # Auto-start workers
        self._start_threads()
        self._poll_ui_queue()

    # ---------------- UI helpers ----------------
    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_drag(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _open_menu(self, event):
        self.menu.tk_popup(event.x_root, event.y_root)

    def append_translated_text(self, text: str):
        if not text:
            return
        self.translated_lines.append(text)
        self.text_var.set("\n".join(self.translated_lines))

    def clear_text(self):
        self.translated_lines.clear()
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
        """Enable/disable click-through on Windows by modifying extended styles."""
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
    def _start_threads(self):
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._translate_loop, daemon=True).start()

    def _capture_loop(self):
        while not self.stop_event.is_set():
            full_text = self.reader.get_current_text()

            if full_text is None:
                now = time.time()
                if now - self.last_warning_time > 5:
                    self.ui_queue.put(("warning", "Google Chrome window not detected for OCR."))
                    self.last_warning_time = now
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            new_segment = self._extract_new_segment(self.last_full_text, full_text)
            self.last_full_text = full_text

            if new_segment:
                segment = new_segment.strip()
                if segment and segment not in self.sent_segments:
                    self.sent_segments.append(segment)
                    try:
                        self.capture_queue.put_nowait(CaptionEvent(text=segment, timestamp=time.time()))
                    except queue.Full:
                        pass

            time.sleep(POLL_INTERVAL_SECONDS)

    @staticmethod
    def _extract_new_segment(previous: str, current: str) -> str:
        """Best-effort extraction of newly appended text."""
        if not current:
            return ""
        if not previous:
            return current

        if current.startswith(previous):
            return current[len(previous) :].strip()

        # If captions rolled/reflowed, take unseen tail tokens.
        prev_tokens = previous.split(" ")
        curr_tokens = current.split(" ")

        overlap = 0
        max_k = min(len(prev_tokens), len(curr_tokens), 22)
        for k in range(max_k, 0, -1):
            if prev_tokens[-k:] == curr_tokens[:k]:
                overlap = k
                break

        tail = " ".join(curr_tokens[overlap:]).strip()
        if tail == current.strip():
            if len(current) < 140:
                return current.strip()
            return ""
        return tail

    def _translate_loop(self):
        while not self.stop_event.is_set():
            try:
                event = self.capture_queue.get(timeout=0.25)
            except queue.Empty:
                continue

            source = event.text[:MAX_CHARS_PER_REQUEST].strip()
            if not source:
                continue

            cached = self.translation_cache.get(source)
            if cached is not None:
                self.ui_queue.put(("append", cached))
                continue

            if not self.translator:
                self.ui_queue.put(("warning", "OPENAI_API_KEY is missing or invalid. Translation paused."))
                continue

            try:
                translated = self.translator.translate_to_arabic(source)
                if translated:
                    self.translation_cache.set(source, translated)
                    self.ui_queue.put(("append", translated))
            except Exception as exc:
                self.ui_queue.put(("warning", f"Translation API error: {exc}"))

    def _poll_ui_queue(self):
        while True:
            try:
                action, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if action == "append":
                self.append_translated_text(payload)
            elif action == "warning":
                if not self.translated_lines:
                    self.text_var.set(payload)

        self.root.after(80, self._poll_ui_queue)

    # ---------------- hotkeys / lifecycle ----------------
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

    def run(self):
        if not OPENAI_API_KEY:
            messagebox.showwarning(
                "Missing API Key",
                "OPENAI_API_KEY is not set.\nSet your key, then restart for translation to work.",
            )
        self.root.mainloop()

    def shutdown(self):
        self.stop_event.set()
        try:
            if keyboard is not None:
                keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        self.root.destroy()


# ------------------------------ Entrypoint ------------------------------------
if __name__ == "__main__":
    app = OverlayApp()
    app.run()
