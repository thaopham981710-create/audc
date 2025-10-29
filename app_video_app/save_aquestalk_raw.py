#!/usr/bin/env python3
"""
Convert Japanese text with MeCab -> AquesTalk WAV with safer normalization and retries.

Usage examples:
  python save_aquestalk_raw.py --text "10月21日に〜" --voice f1 --speed 1.0
  python save_aquestalk_raw.py --text "裏付けになる数字、ちゃんとあるの？" --voice f1 --use-mecab-cli --mecab-path "C:\...\mecab.exe" --to-hiragana

This updated version:
- Normalizes katakana/hiragana before calling AquesTalk (replace ヂ/ヅ/ヴ etc).
- Generates several candidate variants (mapped katakana, hiragana, combo expansions) and tries them in order.
- Uses synth_aquestalk.synthesize_aquestalk_to_file if available.
- Falls back to printing a suggested AquesTalk CLI command on failure.
"""
from __future__ import annotations

import os
import sys
import argparse
import asyncio
import subprocess
import time
import re
import unicodedata
from pathlib import Path
from typing import List, Optional

APP_DIR = os.path.abspath(os.path.dirname(__file__))
OUT_DIR = os.path.join(APP_DIR, "aquestalk_samples")
os.makedirs(OUT_DIR, exist_ok=True)


# ----------------- helpers (preserve original helpers) -----------------
def hira_to_kata(s: str) -> str:
    if not s:
        return s
    out_chars = []
    for ch in s:
        code = ord(ch)
        if 0x3041 <= code <= 0x3096:
            out_chars.append(chr(code + 0x60))
        else:
            out_chars.append(ch)
    return "".join(out_chars)


def sanitize_yomi_keep_katakana(yomi: str) -> str:
    if not yomi:
        return yomi
    s = hira_to_kata(yomi)
    s = s.replace(",", "、").replace(".", "。").replace("?", "？").replace("!", "！")
    s = s.replace(";", "、").replace(":", "、")
    s = s.replace("“", "").replace("”", "").replace("‘", "").replace("’", "")
    s = s.replace('"', "").replace("'", "")
    s = re.sub(r"[^ァ-ヴー\u3000\s、。！？]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def to_fullwidth_digits(s: str) -> str:
    if not s:
        return s
    return s.translate(str.maketrans("0123456789", "０１２３４５６７８９"))


def sanitize_for_aquestalk_fallback(text: str) -> str:
    # keep Japanese and common punctuation used by AquesTalk
    if not text:
        return text
    s = re.sub(r"[^\u3000-\u30FF\u4E00-\u9FFF\uFF01-\uFF60\u3001\u3002\u30FB\u30FC\s、。！？0-9０-９]", "", text)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def get_mecab_yomi_cli(text: str, mecab_path=None, timeout=6) -> Optional[str]:
    """
    Use mecab -Oyomi to get reading fallback.
    mecab_path: path to mecab exe or 'mecab' in PATH
    """
    if mecab_path is None:
        mecab_path = "mecab"
    try:
        p = subprocess.Popen([mecab_path, "-Oyomi"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate(input=text.encode("utf-8"), timeout=timeout)
        # try decode
        try:
            y = out.decode("utf-8").strip()
        except Exception:
            try:
                y = out.decode("cp932").strip()
            except Exception:
                y = out.decode("utf-8", errors="ignore").strip()
        return y
    except Exception:
        return None


# ----------------- normalization and variant generation -----------------
# Base mapping that fixes many AquesTalk undefined-symbol errors for some voices:
_BASE_MAPPING = {
    "ヂ": "ジ",
    "ヅ": "ズ",
    "ヴ": "ブ",  # simple fallback; expand if you need VA/VI/VE/VO -> バ/ビ/ベ/ボ combos
    "ゔ": "ぶ",
    "・": "、",
    "〜": "ー",
    "‐": "ー",
    "“": "",
    "”": "",
    "‘": "",
    "’": "",
}

# small-kana combos that many voices don't support well; expand to safer sequences.
_COMBO_MAPPING = {
    "ティ": "テイ",
    "トゥ": "トウ",
    "ディ": "デイ",
    "ドゥ": "ドウ",
    "チェ": "チエ",
    "ファ": "フア",  # sometimes ファ -> フア is more compatible
    "フィ": "フイ",
    "フェ": "フエ",
    "フォ": "フオ",
    "ウェ": "ウエ",
    "ウォ": "ウオ",
    "ヴァ": "バ",
    "ヴィ": "ビ",
    "ヴェ": "ベ",
    "ヴォ": "ボ",
}


_CONTROL_RE = re.compile(r"[\u0000-\u001F\u007F-\u009F]")


def _apply_mapping(s: str, mapping: dict) -> str:
    for k, v in mapping.items():
        s = s.replace(k, v)
    return s


def normalize_for_aquestalk(text: str, to_hiragana: bool = False) -> str:
    """
    Normalize a kana/reading string to reduce 'undefined symbol (105)' errors with AquesTalk.
    - NFKC normalize
    - apply base replacements and combo expansions
    - remove invisible/control chars and ASCII letters
    - optionally convert katakana -> hiragana (requires jaconv module if present)
    """
    if not text:
        return text
    try:
        import jaconv
    except Exception:
        jaconv = None

    s = unicodedata.normalize("NFKC", text)
    s = _apply_mapping(s, _BASE_MAPPING)
    s = _apply_mapping(s, _COMBO_MAPPING)
    s = _CONTROL_RE.sub("", s)
    s = re.sub(r"[A-Za-z]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if to_hiragana and jaconv:
        try:
            s = jaconv.kata2hira(s)
        except Exception:
            pass
    return s


def generate_candidate_variants(katakana_text: str, prefer_hiragana: bool = False) -> List[str]:
    """
    Return a list of candidate strings to try with AquesTalk in order:
      - original katakana_text
      - base-mapped katakana (ヂ/ヅ/ヴ etc)
      - combo-expanded katakana
      - candidate hiragana variants (if requested)
      - stripped/cleaned fallback
    """
    candidates = []
    t = katakana_text.strip()
    if not t:
        return candidates

    # original as-is
    candidates.append(t)

    # base mapping
    mapped_base = _apply_mapping(t, _BASE_MAPPING)
    if mapped_base not in candidates:
        candidates.append(mapped_base)

    # combo expanded (apply combo mapping after base)
    mapped_combo = _apply_mapping(mapped_base, _COMBO_MAPPING)
    if mapped_combo not in candidates:
        candidates.append(mapped_combo)

    # try removing small-kana by replacing with expanded forms (again defensive)
    # (sometimes doubling replacement helps; we ensure uniqueness)
    mapped_combo2 = mapped_combo
    for k, v in _COMBO_MAPPING.items():
        mapped_combo2 = mapped_combo2.replace(k, v)
    if mapped_combo2 not in candidates:
        candidates.append(mapped_combo2)

    # hiragana variant
    try:
        import jaconv
        hira = jaconv.kata2hira(mapped_combo)
        if hira and hira not in candidates:
            candidates.append(hira)
        if prefer_hiragana:
            # put hiragana at front if preferred
            if hira in candidates:
                candidates.remove(hira)
                candidates.insert(0, hira)
    except Exception:
        # no jaconv installed -> optionally convert katakana -> hiragana with a naive approach
        pass

    # last-resort: remove characters outside katakana/hiragana/basic punctuation
    fallback = re.sub(r"[^\u3040-\u30FF\u3000\s、。！？ー]", "", mapped_combo)
    fallback = re.sub(r"\s+", " ", fallback).strip()
    if fallback and fallback not in candidates:
        candidates.append(fallback)

    # ensure uniqueness and non-empty
    out = []
    for c in candidates:
        if c and c not in out:
            out.append(c)
    return out


# ----------------- synth wrapper -----------------
def synth_via_wrapper(text_for_aq: str, voice: str, speed_percent: int, out_wav: str):
    """
    Try to synthesize using synth_aquestalk.synthesize_aquestalk_to_file (sync).
    Raises exception on failure.
    """
    try:
        # import local helper module (part of this project)
        from synth_aquestalk import synthesize_aquestalk_to_file
    except Exception as e:
        raise RuntimeError("synth_aquestalk wrapper not available: " + str(e)) from e

    # call the sync synth function
    return synthesize_aquestalk_to_file(text_for_aq, out_wav, voice=voice, speed=speed_percent)


# ----------------- main flow -----------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--text", "-t", type=str, required=False,
                   default="10月21日に「受注開始から1か月で1万1千台を突破」と明言、さらに10月19日時点の具体的な数は1万1344台。",
                   help="Original Japanese text")
    p.add_argument("--voice", default="f1", help="AquesTalk voice id/name (e.g. f1)")
    p.add_argument("--speed", type=float, default=1.0, help="Speed multiplier, e.g. 1.0 (converted to percent)")
    p.add_argument("--use-mecab-cli", action="store_true", help="Force using mecab CLI -Oyomi instead of any Python helper")
    p.add_argument("--mecab-path", default=None, help="Path to mecab executable if using CLI")
    p.add_argument("--to-hiragana", action="store_true", help="Try converting katakana -> hiragana for candidates")
    p.add_argument("--prefer-hiragana", action="store_true", help="Prioritize hiragana candidate first when trying")
    args = p.parse_args()

    original = args.text.strip()
    print("Original:", original)

    # 1) sanitize original for AquesTalk fallback (remove foreign ascii etc)
    sanitized_original = sanitize_for_aquestalk_fallback(to_fullwidth_digits(original))
    print("Sanitized original (fallback):", sanitized_original)

    # 2) get yomi via mecab (first try any installed Python helper? fallback to mecab CLI)
    yomi = None
    if not args.use_mecab_cli:
        # try to import project helper if exists
        try:
            from mecab_helper import mecab_yomi
            try:
                yomi = mecab_yomi(original, base_dir=APP_DIR)
            except Exception:
                yomi = None
        except Exception:
            yomi = None

    if yomi is None:
        print("Using MeCab CLI -Oyomi fallback...")
        yomi = get_mecab_yomi_cli(original, mecab_path=args.mecab_path)

    if not yomi:
        print("MeCab returned nothing; will use sanitized original for synthesis.")
        yomi = sanitized_original

    print("MeCab yomi (raw):", yomi)

    # 3) clean yomi -> keep katakana and punctuation suitable for AquesTalk
    yomi_kata = sanitize_yomi_keep_katakana(yomi)
    if not yomi_kata:
        # fallback to sanitized original
        text_for_aq = sanitized_original
    else:
        text_for_aq = yomi_kata

    print("Text for AquesTalk (after MeCab -> sanitized katakana):", text_for_aq)

    # 4) remove trailing commas on clauses (recommended)
    text_for_aq = re.sub(r"[、，,]+$", "", text_for_aq).strip()

    # 5) generate variants and try synth
    speed_param = max(30, min(400, int(args.speed * 100)))
    candidates = generate_candidate_variants(text_for_aq, prefer_hiragana=args.prefer_hiragana)
    # if user explicitly asked to try hiragana, ensure hiragana variant is present and prioritized
    if args.to_hiragana:
        # ensure hiragana lead candidate if jaconv available
        try:
            import jaconv

            hira_first = jaconv.kata2hira(text_for_aq)
            if hira_first:
                if hira_first in candidates:
                    candidates.remove(hira_first)
                candidates.insert(0, hira_first)
        except Exception:
            # if jaconv not present, try naive fallback by leaving as-is
            pass

    if not candidates:
        candidates = [text_for_aq]

    print("Will try these candidates (in order):")
    for i, c in enumerate(candidates, start=1):
        print(f"{i}: {c}")

    ts = int(time.time())
    attempted = 0
    last_exc: Optional[Exception] = None
    for idx, cand in enumerate(candidates, start=1):
        out_name = f"aquestalk_{ts}_{idx}.wav"
        out_wav = os.path.join(OUT_DIR, out_name)
        print(f"\nAttempt #{idx} voice={args.voice} text='{cand}' -> out={out_wav}")
        attempted += 1
        try:
            # try wrapper synth (sync)
            synth_via_wrapper(cand, args.voice, speed_param, out_wav)
            print("Synthesis SUCCESS ->", out_wav)
            return
        except Exception as e:
            last_exc = e
            print(" -> FAILED:", repr(e))
            # continue to next candidate

    # if reached here, all attempts failed
    print("\nAll attempts failed (tried", attempted, "variants).")
    if last_exc:
        print("Last error:", last_exc)

    # Suggest CLI command for manual test (escape quotes)
    safe_text = text_for_aq.replace('"', '\\"')
    print()
    print("If you have AquesTalk CLI, try running a command like (adjust path/flags):")
    print(f'  "C:\\path\\to\\AquesTalk.exe" -v {args.voice} -s {speed_param} -o "{os.path.join(OUT_DIR, "aquestalk_failed.wav")}" "{safe_text}"')
    print()
    print("MeCab yomi (kana) ->", yomi)
    print("Sanitized katakana for AquesTalk ->", text_for_aq)
    print("Candidates tried (in order):")
    for i, c in enumerate(candidates, start=1):
        print(i, c)


if __name__ == "__main__":
    main()