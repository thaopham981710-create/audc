# AquesTalk Voice Library

This directory contains the AquesTalk Japanese text-to-speech voice library and supporting files.

## Directory Structure

```
aquestalk/
├── AqKanji2Koe.dll      # Kanji to phonetic conversion library
├── AqResample.dll        # Audio resampling library
├── AqUsrDic.dll          # User dictionary library
├── aq_dic/               # Dictionary files
│   ├── aqdic.bin        # Main dictionary
│   ├── aq_user.dic      # User dictionary
│   └── CREDITS          # Dictionary credits
└── aquestalk/            # Voice data directory
    ├── f1/              # Female voice 1
    ├── f2/              # Female voice 2
    ├── f3/              # Female voice 3
    ├── m1/              # Male voice 1
    ├── m2/              # Male voice 2
    ├── r1/              # Robot voice 1
    ├── dvd/             # DVD voice
    ├── imd1/            # IMD voice 1
    └── jgr/             # JGR voice
```

## Available Voices

- **f1, f2, f3**: Female voices with different characteristics
- **m1, m2**: Male voices with different characteristics
- **r1**: Robot-like voice
- **dvd, imd1, jgr**: Special character voices

## Usage

The voice library is automatically detected by the application when:
1. The directory structure matches the expected layout (as shown above)
2. Each voice subdirectory contains the required `AquesTalk.dll` file
3. The supporting DLL files are present in the parent directory

## Notes

- This is a Windows-specific library (requires Windows DLLs)
- The library uses 32-bit architecture
- Voice synthesis requires proper Japanese text input (hiragana/katakana)
