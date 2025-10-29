#!/usr/bin/env python3
# diagnose_aquestalk_input.py
# Usage:
#   python diagnose_aquestalk_input.py "sanitized_or_raw_text_here"
#
# This script prints codepoints, then tries aq.synthe_raw on whole string,
# on segments split by punctuation, and on incremental prefixes to locate the
# smallest substring that triggers AquesTalkError.

import sys
import unicodedata
import traceback

try:
    import aquestalk
    from aquestalk.aquestalk import AquesTalkError
except Exception:
    aquestalk = None
    AquesTalkError = Exception

PUNCT = ('、','。','，','．','「','」','『','』','・','！','？',' ')  # split chars to try

def show_chars(s):
    print("repr:", repr(s))
    print("length:", len(s))
    print("per-char:")
    for i,ch in enumerate(s):
        try:
            name = unicodedata.name(ch)
        except ValueError:
            name = "<no name>"
        print(f"  [{i}] U+{ord(ch):04X} '{ch}'  name: {name}")

def try_synth(aq, text, method='raw'):
    try:
        if method == 'raw':
            _ = aq.synthe_raw(text)
        else:
            _ = aq.synthe(text)
        return True, None
    except Exception as e:
        return False, e

def find_bad_segment(aq, s):
    # try segments split by punctuation
    import re
    split_re = '[' + re.escape(''.join(PUNCT)) + ']+'
    parts = [p for p in re.split(split_re, s) if p]
    if not parts:
        parts = [s]
    for i,p in enumerate(parts):
        ok, err = try_synth(aq, p, method='raw')
        print(f"Segment[{i}] repr={repr(p)} len={len(p)} ->", "OK" if ok else f"ERR: {err}")
        if not ok:
            return ('segment', i, p, err)
    # try prefixes to find smallest failing prefix (binary search style)
    lo = 0
    hi = len(s)
    # first ensure whole string fails
    ok_whole, err_whole = try_synth(aq, s, method='raw')
    if ok_whole:
        return ('none_whole_ok', None, None, None)
    print("Whole string failed; searching smallest failing prefix...")
    # find first index where prefix fails
    left = 0
    right = len(s)
    first_bad = None
    while left < right:
        mid = (left + right) // 2
        prefix = s[:mid+1]
        ok, err = try_synth(aq, prefix, method='raw')
        if ok:
            left = mid + 1
        else:
            first_bad = (mid, prefix, err)
            right = mid
    return ('prefix',) + (first_bad if first_bad else (None, None, None))

def main():
    if len(sys.argv) < 2:
        print("Usage: python diagnose_aquestalk_input.py \"TEXT\"")
        sys.exit(1)
    s = sys.argv[1]
    print("=== INPUT CHARS ===")
    show_chars(s)
    if aquestalk is None:
        print("ERROR: cannot import aquestalk package. Make sure you run with the same Python used for GUI.")
        sys.exit(2)
    # load a voice to test (try default f1)
    try:
        aq = aquestalk.load('f1')
    except Exception as e:
        print("Failed to load voice f1:", e)
        traceback.print_exc()
        sys.exit(3)

    print("\n=== try aq.synthe (if available) on whole string ===")
    ok, err = try_synth(aq, s, method='synth')
    if ok:
        print("aq.synthe succeeded for whole string")
    else:
        print("aq.synthe error:", err)

    print("\n=== try aq.synthe_raw on whole string ===")
    ok, err = try_synth(aq, s, method='raw')
    if ok:
        print("aq.synthe_raw succeeded for whole string")
        sys.exit(0)
    else:
        print("aq.synthe_raw error:", err)

    print("\n=== Splitting and testing segments ===")
    res = find_bad_segment(aq, s)
    print("\n=== RESULT ===")
    print(res)

if __name__ == '__main__':
    main()