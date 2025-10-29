#!/usr/bin/env python3
# example_synth_mecab_subprocess.py
# Use system MeCab (mecab.exe) via subprocess to get readings (読み),
# convert to hiragana, sanitize, then synthesize with AquesTalk.
#
# Usage:
#   python example_synth_mecab_subprocess.py "テキスト" out.wav [path_to_mecab_exe] [voice]
#
# Requirements:
# - Python 32-bit (to match AquesTalk DLLs)
# - MeCab installed (mecab.exe present)
# - pip install jaconv

import sys
import subprocess
import re
import io
import wave
import jaconv

import aquestalk
from aquestalk.aquestalk import AquesTalkError

_ALLOWED_RE = re.compile(r'[^\u3040-\u309F\u30A0-\u30FF\u3001\u3002\uFF1F\uFF01\u300C\u300D\u30FB\u3000\uFF0C\uFF08\uFF09\u300E\u300F\u30FC\s]')

def mecab_reading_via_subprocess_utf8(text, mecab_path='mecab'):
    """
    Call mecab.exe via subprocess using UTF-8 encoding for both stdin and stdout.
    Returns katakana string (concatenated readings).
    """
    # encode input as UTF-8 bytes
    input_bytes = text.encode('utf-8')
    proc = subprocess.Popen([mecab_path],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    stdout_bytes, stderr_bytes = proc.communicate(input_bytes)

    if proc.returncode != 0:
        # try to decode stderr as utf-8 for readable message
        try:
            stderr_text = stderr_bytes.decode('utf-8', errors='replace')
        except Exception:
            stderr_text = repr(stderr_bytes)
        raise RuntimeError(f"mecab failed (returncode={proc.returncode}): {stderr_text.strip()}")

    # decode stdout as utf-8
    stdout_text = stdout_bytes.decode('utf-8', errors='replace')

    readings = []
    for line in stdout_text.splitlines():
        if line == 'EOS' or not line.strip():
            continue
        if '\t' in line:
            surface, feats = line.split('\t', 1)
            cols = feats.split(',')
            pron = None
            if len(cols) > 7 and cols[7] != '*':
                pron = cols[7]
            elif len(cols) > 6 and cols[6] != '*':
                pron = cols[6]
            else:
                pron = surface
            readings.append(pron)
        else:
            parts = line.split(',')
            if parts:
                readings.append(parts[0])
    katakana = ''.join(readings)
    return katakana

def sanitize_for_aquestalk(text):
    text = text.replace('-', 'ー')
    text = text.replace(',', '、').replace('?', '？').replace('!', '！').replace('.', '。')
    text = jaconv.h2z(text, digit=True, ascii=False)
    text = re.sub(r'[\(\)（）\[\]［］]', '', text)
    cleaned = _ALLOWED_RE.sub('', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = jaconv.kata2hira(cleaned)
    return cleaned

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
        print("Usage: python example_synth_mecab_subprocess.py \"TEXT\" [out.wav] [mecab_path] [voice]")
        sys.exit(1)

    raw_text = sys.argv[1]
    out_file = sys.argv[2] if len(sys.argv) >= 3 else 'out.wav'
    mecab_path = sys.argv[3] if len(sys.argv) >= 4 else 'mecab'
    voice = sys.argv[4] if len(sys.argv) >= 5 else 'f1'

    print("Original input (repr):", repr(raw_text))
    print("Using mecab executable:", mecab_path)

    try:
        katakana = mecab_reading_via_subprocess_utf8(raw_text, mecab_path=mecab_path)
        print("Katakana reading (repr):", repr(katakana))
    except Exception as e:
        print("Error running MeCab:", e)
        sys.exit(2)

    hiragana = jaconv.kata2hira(katakana)
    sanitized = sanitize_for_aquestalk(hiragana)
    print("Sanitized kana for AquesTalk (repr):", repr(sanitized))
    print("Sanitized length:", len(sanitized))

    try:
        aq = aquestalk.load(voice)
    except Exception as e:
        print("Cannot load AquesTalk DLL:", e)
        sys.exit(1)

    try:
        _ = aq.synthe("こんにちは")
    except Exception as e:
        print("Sanity test failed (cannot synth 'こんにちは'):", e)
        sys.exit(3)

    try:
        raw = aq.synthe_raw(sanitized)
        save_raw_wav_bytes(raw, out_file)
        print("Saved WAV:", out_file)
    except AquesTalkError as ae:
        print("AquesTalkError:", ae)
        print("Failed on sanitized input (repr):", repr(sanitized))
        sys.exit(4)
    except Exception as ex:
        print("Other error:", ex)
        sys.exit(5)

if __name__ == '__main__':
    main()