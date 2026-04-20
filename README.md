# Live Arabic Captions Overlay (Deepgram + Argos)

This project contains a single Python desktop app that:

- Captures **system audio** from your headset/speakers using WASAPI loopback (default)
- Sends audio to Deepgram live transcription (`nova-2` by default)
- Translates English captions to Arabic locally using Argos Translate
- Displays clean, styled live captions in an always-on-top overlay

## File

- `live_captions_overlay.py`

## Install

```bash
pip install sounddevice websockets argostranslate
```

## Environment variables

```bash
# Required
set DEEPGRAM_API_KEY=your_key_here

# Optional
set DEEPGRAM_MODEL=nova-2
set AUDIO_SOURCE_MODE=loopback   # loopback (default) or mic
```

## Run

```bash
python live_captions_overlay.py
```

## Notes

- `AUDIO_SOURCE_MODE=loopback` listens to playback audio (YouTube, browser, media player).
- `AUDIO_SOURCE_MODE=mic` switches back to microphone input.
- The overlay keeps recent lines organized and wrapped for readability.
