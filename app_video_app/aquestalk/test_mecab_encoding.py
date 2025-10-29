#!/usr/bin/env python3
# test_mecab_encoding.py
# Gọi mecab.exe qua subprocess với nhiều encoding khác nhau,
# in ra stdout bytes (hex) và thử decode bằng nhiều encodings để xem cái nào cho output hợp lệ.

import subprocess
import binascii
import sys

text = "なあ霊夢、ルークスが受注開始からわずか1か月で1万台超えって話、もう聞いたか？"
mecab_path = "mecab"  # hoặc full path "C:\\Program Files (x86)\\MeCab\\bin\\mecab.exe"

def run_with_input_bytes(input_bytes):
    proc = subprocess.Popen([mecab_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate(input_bytes)
    return proc.returncode, stdout, stderr

def try_encodings_and_print():
    candidates = [
        ("cp932 (shift_jis)", None, "cp932"),
        ("utf-8", None, "utf-8"),
        ("euc_jp", None, "euc_jp"),
        ("iso2022_jp", None, "iso2022_jp"),
        ("latin1 (pass-through)", None, "latin1"),
    ]

    for name, _, enc in candidates:
        try:
            b = text.encode(enc)
        except Exception as e:
            print(f"--- encode with {name} FAILED: {e}")
            continue
        print("=================================================================")
        print(f"Input encoded as {name} ({enc}), length={len(b)}")
        ret, stdout, stderr = run_with_input_bytes(b)
        print(f"mecab returncode: {ret}")
        if stderr:
            # try to decode stderr for readability
            try:
                print("stderr (decoded cp932):", stderr.decode('cp932', errors='replace'))
            except:
                print("stderr (repr):", repr(stderr))
        print("stdout bytes (hex, first 200 bytes):", binascii.hexlify(stdout[:200]))
        # try various decodings for stdout
        for try_enc in ("cp932", "utf-8", "euc_jp", "iso2022_jp", "latin1"):
            try:
                s = stdout.decode(try_enc)
                # print only first 3 lines to keep console readable
                snippet = "\\n".join(s.splitlines()[:6])
                print(f"decoded as {try_enc}:")
                print(snippet)
            except Exception as e:
                print(f"decoded as {try_enc} FAILED: {e}")
        print()

if __name__ == '__main__':
    print("Running MeCab encoding diagnostics for text:")
    print(text)
    print()
    try_encodings_and_print()
    print("Done. Copy & paste the whole output here so I can analyze which encoding works.")