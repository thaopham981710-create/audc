# AUDC - Auto Video Dialogue Creator

An automated video creation application with Japanese text-to-speech support using AquesTalk and Voicevox.

## Features

- **Multiple TTS Engines**: Support for both Voicevox and AquesTalk voice synthesis
- **Video Generation**: Create dialogue videos with automatic subtitle generation
- **Character Icons**: Customizable character avatars with talking animations
- **Multiple Voice Options**: 9 AquesTalk voices (f1-f3, m1-m2, r1, dvd, imd1, jgr) plus Voicevox voices

## AquesTalk Setup

The AquesTalk Japanese TTS library is included in `app_video_app/aquestalk/`. To verify the setup:

```bash
cd app_video_app
python verify_aquestalk_setup.py
```

For more details about AquesTalk configuration, see [app_video_app/aquestalk/README.md](app_video_app/aquestalk/README.md).

## Project Structure

```
audc/
├── app_video_app/           # Main application directory
│   ├── aquestalk/          # AquesTalk voice library
│   ├── *.py                # Application modules
│   └── verify_aquestalk_setup.py  # Verification script
├── aquestalkplayer.zip     # AquesTalk voice data (backup)
└── aquestalkplayer_20250606.zip  # Updated voice data (backup)
```

## Requirements

- Python 3.x
- Windows (for AquesTalk DLL support)
- FFmpeg
- Voicevox (optional, for Voicevox voices)

## Note

This is a Windows-specific application due to the AquesTalk library requirements (32-bit Windows DLLs).