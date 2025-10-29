#!/usr/bin/env python3
# voice_test.py
# Usage:
#   python voice_test.py "テキスト" [out_prefix] [voice1,voice2,...]
# Example:
#   python voice_test.py "こんにちは" sample f1,f2,f3
#
# If no voices provided, the script will try default voices f1..f8.
# The script creates WAV files named {out_prefix}_{voice}.wav for each voice that works.

import sys
import os
import io
import wave

import jaconv
import aquestalk
from aquestalk.aquestalk import AquesTalkError

DEFAULT_VOICES = ['f1','f2','f3','f4','f5','f6','f7','f8']

def save_raw_wav_bytes(raw_bytes, out_path):
    with wave.open(io.BytesIO(raw_bytes), 'rb') as r:
        frames = r.readframes(r.getnframes())
        with wave.open(out_path, 'wb') as wf:
            wf.setnchannels(r.getnchannels())
            wf.setsampwidth(r.getsampwidth())
            wf.setframerate(r.getframerate())
            wf.writeframes(frames)

def main():
    if len(sys.argv) < 2:
        print("Usage: python voice_test.py \"TEXT\" [out_prefix] [voice1,voice2,...]")
        sys.exit(1)

    text = sys.argv[1]
    out_prefix = sys.argv[2] if len(sys.argv) >= 3 else 'sample'
    if len(sys.argv) >= 4 and sys.argv[3].strip():
        voices = [v.strip() for v in sys.argv[3].split(',') if v.strip()]
    else:
        voices = DEFAULT_VOICES

    print("Text to synthesize:", repr(text))
    print("Voices to test:", voices)
    print("Output prefix:", out_prefix)
    success = []
    failed = []

    for v in voices:
        try:
            print(f"-- Testing voice: {v} ...", end=' ')
            aq = aquestalk.load(v)  # may raise if voice not available
            # You can change speed (e.g., 80..200) or other params if wrapper supports them
            raw = aq.synthe_raw(text, speed=100)  # adjust speed as needed
            out_name = f"{out_prefix}_{v}.wav"
            save_raw_wav_bytes(raw, out_name)
            print("OK ->", out_name)
            success.append(out_name)
        except AquesTalkError as ae:
            print("AquesTalkError:", ae)
            failed.append((v, str(ae)))
        except Exception as e:
            print("FAILED:", e)
            failed.append((v, str(e)))

    print("\nSummary:")
    print("Succeeded:", len(success))
    for s in success:
        print("  ", s)
    print("Failed:", len(failed))
    for v, err in failed:
        print("  ", v, "->", err)

if __name__ == "__main__":
    main()