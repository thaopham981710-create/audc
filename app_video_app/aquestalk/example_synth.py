#!/usr/bin/env python3
# example_synth_mecab.py
# Usage:
#   python example_synth_mecab.py "原文テキスト" out.wav [voice]
#
# Requires:
#   - MeCab installed on system (Windows: MeCab installer + IPADIC)
#   - pip install mecab-python3 jaconv
#
# Note: Run with Python 32-bit on Windows because AquesTalk dlls in the repo are 32-bit.

import sys
import re
import io
import wave

import jaconv
import aquestalk
from aquestalk.aquestalk import AquesTalkError

try:
    import MeCab
    _HAS_MECAB = True
except Exception:
    _HAS_MECAB = False

# Regex for allowed chars: hiragana, katakana, japanese punctuation, prolonged mark (ー) and spaces
_ALLOWED_RE = re.compile(r'[^\u3040-\u309F\u30A0-\u30FF\u3001\u3002\uFF1F\uFF01\u300C\u300D\u30FB\u3000\uFF0C\uFF08\uFF09\u300E\u300F\u30FC\s]')

def mecab_to_hiragana(text):
    """
    Use MeCab to get reading for each token. Try common feature positions.
    Fallback: use surface if reading not available.
    """
    if not _HAS_MECAB:
        raise RuntimeError("MeCab not available. Install mecab and mecab-python3, or use fugashi instead.")

    tagger = MeCab.Tagger()  # default ipadic if installed
    # Ensure parse output encoding is str on Python3
    node = tagger.parseToNode(text)
    parts = []
    while node:
        # skip BOS/EOS nodes
        if node.surface:
            feature = node.feature  # csv-like string for ipadic
            pron = None
            if feature:
                # feature format depends on dict:
                # - IPADIC: feature.split(',')[7] often holds "pronunciation" or reading
                # - UniDic: format may differ; try multiple indices/keys
                cols = feature.split(',')
                # try common index 7, 8
                if len(cols) > 7 and cols[7] and cols[7] != '*':
                    pron = cols[7]
                elif len(cols) > 6 and cols[6] and cols[6] != '*':
                    # fallback to index 6
                    pron = cols[6]
            if not pron or pron == '*':
                pron = node.surface
            parts.append(pron)
        node = node.next
    katakana = ''.join(parts)
    # Convert katakana -> hiragana for AquesTalk safety
    hiragana = jaconv.kata2hira(katakana)
    return hiragana

def sanitize_for_aquestalk(text):
    # Replace ascii hyphen with prolonged sound mark
    text = text.replace('-', 'ー')
    # Convert ascii punctuation to Japanese punctuation
    text = text.replace(',', '、').replace('?', '？').replace('!', '！').replace('.', '。')
    # Convert half-width digits to full-width digits (optional)
    text = jaconv.h2z(text, digit=True, ascii=False)
    # Remove parentheses (ASCII and fullwidth)
    text = re.sub(r'[\(\)（）［］\[\]]', '', text)
    # Remove characters not allowed
    cleaned = _ALLOWED_RE.sub('', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # Convert katakana -> hiragana
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
        print("Usage: python example_synth_mecab.py \"TEXT\" [out.wav] [voice]")
        sys.exit(1)

    raw_text = sys.argv[1]
    out_file = sys.argv[2] if len(sys.argv) >= 3 else 'out.wav'
    voice = sys.argv[3] if len(sys.argv) >= 4 else 'f1'

    print("Original input (repr):", repr(raw_text))

    # Convert Kanji -> kana using MeCab if available
    if _HAS_MECAB:
        kana = mecab_to_hiragana(raw_text)
        print("Converted by MeCab -> hiragana (repr):", repr(kana))
    else:
        print("Warning: MeCab not installed; assuming input already kana.")
        kana = raw_text

    # Sanitize for AquesTalk
    sanitized = sanitize_for_aquestalk(kana)
    print("Sanitized kana for AquesTalk (repr):", repr(sanitized))
    print("Sanitized length:", len(sanitized))

    try:
        aq = aquestalk.load(voice)
    except Exception as e:
        print("Cannot load AquesTalk DLL:", e)
        sys.exit(1)

    # sanity check
    try:
        _ = aq.synthe("こんにちは")
    except Exception as e:
        print("Sanity test failed:", e)
        sys.exit(2)

    try:
        raw = aq.synthe_raw(sanitized)
        save_raw_wav_bytes(raw, out_file)
        print("Saved WAV:", out_file)
    except AquesTalkError as ae:
        print("AquesTalkError:", ae)
        print("Failed on sanitized input (repr):", repr(sanitized))
        sys.exit(3)
    except Exception as ex:
        print("Other error:", ex)
        sys.exit(4)

if __name__ == '__main__':
    main()