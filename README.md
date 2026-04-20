# Live Caption Overlay (EN Audio -> AR Translation)

سكريبت Windows جاهز يعرض **Live Caption Overlay** شكله مريح (أسود شفاف) ويترجم أي صوت إنجليزي خارج من الجهاز إلى عربي بشكل لحظي.

## Features

- يلتقط صوت النظام بالكامل (System Audio / Speaker Output) عبر WASAPI Loopback.
- لا يلتقط الميكروفون (Mic) نهائيًا؛ الالتقاط من الـ Speaker/Output فقط.
- يحول الكلام الإنجليزي إلى نص بسرعة باستخدام Deepgram Realtime.
- يترجم النص للعربي تلقائيًا ويعرضه على Overlay عائم دائمًا فوق كل النوافذ.
- Overlay قابل للسحب + تكبير/تصغير سريع.

## File

- `live_captions_overlay.py`

## Requirements

```bash
pip install sounddevice websockets keyboard deep-translator pywin32
```

> ملاحظة: السكريبت مخصص لويندوز.

## API Key

### PowerShell

```powershell
$env:DEEPGRAM_API_KEY="your_deepgram_key_here"
```

### Command Prompt

```cmd
set DEEPGRAM_API_KEY=your_deepgram_key_here
```

(اختياري) موديل Deepgram:

```cmd
set DG_MODEL=nova-2
```

(اختياري) شفافية النافذة:

```cmd
set CAPTION_ALPHA=0.72
```

## Run

```bash
python live_captions_overlay.py
```

## Controls

- `Ctrl + Shift + T`: إخفاء/إظهار overlay
- `Ctrl + Shift + C`: مسح النص
- `Ctrl + Shift + =`: تكبير
- `Ctrl + Shift + -`: تصغير
- `Ctrl + Mouse Wheel`: تكبير/تصغير
- Right click على overlay لفتح القائمة

## Notes

- يبدأ تلقائيًا بدون خطوات إضافية.
- لو `DEEPGRAM_API_KEY` مش موجود، هيظهر تحذير واضح.
- الأداء سريع لأن التحويل الصوتي Realtime + ترجمة مباشرة مع كاش.
