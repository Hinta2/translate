# Live Captions Arabic Overlay (Windows)

This project contains a single Python desktop app that:

- Reads text from **Windows Live Captions** using UI Automation (`pywinauto`)
- Detects only new caption text segments
- Translates English captions to Arabic using the OpenAI API (`gpt-4o-mini` by default)
- Displays translated text in a draggable, transparent, always-on-top overlay

## File

- `live_captions_overlay.py`

## Install

```bash
pip install openai pywinauto keyboard pywin32
```

## Set your API key (required)

> Do not hardcode API keys in source code. Set it as an environment variable.

### PowerShell

```powershell
$env:OPENAI_API_KEY="your_api_key_here"
```

### Command Prompt

```cmd
set OPENAI_API_KEY=your_api_key_here
```

(Optional) choose a model:

```cmd
set OPENAI_MODEL=gpt-4o-mini
```

## Run

```bash
python live_captions_overlay.py
```

## Controls

- `Ctrl + Shift + T`: toggle overlay visibility
- `Ctrl + Shift + C`: clear translated text
- Right-click overlay for menu (clear/toggle/click-through/exit)

## Notes

- The app starts listening automatically when launched.
- If Live Captions is not running, the overlay shows a warning message.
- If `OPENAI_API_KEY` is missing/invalid, translation is paused and a warning is shown.
