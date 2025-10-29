#!/usr/bin/env python3
# mecab_helper.py
# Robust MeCab helper that:
# - prefers an explicit MeCab bin directory set via environment variable AQUESTALK_MECAB_BIN
# - falls back to bundled BASE_DIR/MeCab/bin if provided to init_mecab()
# - falls back to PATH / common Program Files locations
# - ensures the MeCab bin dir is added to DLL search path on Windows
# - calls mecab -Oyomi, tries CP932/UTF-8/EUC-JP decodes and returns the best yomi
# - logs the final mecab.exe path used (via optional log_callback) and writes debug files when helpful

import os
import sys
import subprocess
import shutil
import tempfile
import re
import time

TEMP_DIR = tempfile.gettempdir()

def _add_dir_to_dll_search(dirpath):
    try:
        if not dirpath:
            return
        if sys.platform == "win32":
            try:
                os.add_dll_directory(dirpath)
            except Exception:
                # fallback: prepend to PATH
                os.environ["PATH"] = dirpath + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass

def init_mecab(base_dir):
    """
    Call at startup with BASE_DIR (project root). This will add bundled MeCab/bin
    to DLL search path so subprocess and DLL loads find libmecab if present.
    """
    # Prefer explicit env var first (user-specified bin dir)
    env_bin = os.environ.get("AQUESTALK_MECAB_BIN")
    if env_bin and os.path.isdir(env_bin):
        _add_dir_to_dll_search(env_bin)
    # Then add bundled bin if present
    if base_dir:
        bundled = os.path.join(base_dir, "MeCab", "bin")
        if os.path.isdir(bundled):
            _add_dir_to_dll_search(bundled)

def find_mecab_executable(base_dir=None, log_callback=None):
    """
    Return full path to mecab executable or None.
    Order:
      1) explicit env AQUESTALK_MECAB_BIN (mecab.exe inside)
      2) bundled base_dir/MeCab/bin/mecab.exe (if base_dir provided)
      3) mecab in PATH (shutil.which)
      4) common Program Files locations
    """
    # 1) env override
    env_bin = os.environ.get("AQUESTALK_MECAB_BIN")
    if env_bin:
        candidate = os.path.join(env_bin, "mecab.exe")
        if os.path.exists(candidate):
            if log_callback:
                try: log_callback(f"[MeCab] Using mecab from AQUESTALK_MECAB_BIN: {candidate}") 
                except Exception: pass
            return candidate

    # 2) bundled in project
    if base_dir:
        candidate = os.path.join(base_dir, "MeCab", "bin", "mecab.exe")
        if os.path.exists(candidate):
            if log_callback:
                try: log_callback(f"[MeCab] Using bundled mecab: {candidate}") 
                except Exception: pass
            return candidate

    # 3) PATH
    exe = shutil.which("mecab")
    if exe:
        if log_callback:
            try: log_callback(f"[MeCab] Using mecab from PATH: {exe}") 
            except Exception: pass
        return exe

    # 4) common locations
    commons = [
        r"C:\Program Files\MeCab\bin\mecab.exe",
        r"C:\Program Files (x86)\MeCab\bin\mecab.exe",
    ]
    for p in commons:
        if os.path.exists(p):
            if log_callback:
                try: log_callback(f"[MeCab] Using mecab from common location: {p}") 
                except Exception: pass
            return p

    if log_callback:
        try: log_callback("[MeCab] mecab executable not found (env, bundled, PATH and common locations tried).") 
        except Exception: pass
    return None

_kana_re = re.compile(r'[ぁ-ゔァ-ヴー]')

def _looks_like_yomi(s: str) -> bool:
    if not s:
        return False
    return bool(_kana_re.search(s))

def _try_decode(output_bytes):
    candidates = {}
    for enc in ("cp932", "utf-8", "euc_jp"):
        try:
            decoded = output_bytes.decode(enc, errors="replace").strip()
            candidates[enc] = decoded
        except Exception:
            candidates[enc] = None
    return candidates

def mecab_yomi(text, base_dir=None, timeout=6, log_callback=None):
    """
    Convert text -> yomi (katakana/hiragana) by calling mecab -Oyomi.
    Returns yomi string (decoded) or None on failure.
    """
    if not text:
        return None

    mecab_exe = find_mecab_executable(base_dir=base_dir, log_callback=log_callback)
    if not mecab_exe:
        return None

    # Prepare input bytes (prefer CP932 on Windows)
    try:
        input_bytes = text.encode("cp932", errors="replace")
    except Exception:
        input_bytes = text.encode("utf-8", errors="replace")

    try:
        proc = subprocess.run([mecab_exe, "-Oyomi"], input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        out_bytes = proc.stdout or b""
        err_bytes = proc.stderr or b""
    except Exception as e:
        if log_callback:
            try: log_callback(f"[MeCab] exception calling mecab: {e}") 
            except Exception: pass
        return None

    # If no stdout, decode stderr for hints
    if not out_bytes and err_bytes:
        decs_err = _try_decode(err_bytes)
        debug_path = os.path.join(TEMP_DIR, f"mecab_stderr_{int(time.time())}.txt")
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write("mecab stderr decodings:\n")
                for enc, dec in decs_err.items():
                    f.write(f"--- {enc} ---\n")
                    f.write((dec or "") + "\n\n")
            if log_callback:
                try: log_callback(f"[MeCab] no stdout; stderr decodings written to {debug_path}") 
                except Exception: pass
        except Exception:
            pass
        return None

    decs_out = _try_decode(out_bytes)
    # choose decoding that contains kana
    best = None
    chosen_enc = None
    for enc in ("cp932", "utf-8", "euc_jp"):
        candidate = decs_out.get(enc)
        if candidate and _looks_like_yomi(candidate):
            best = candidate
            chosen_enc = enc
            break
    # fallback: pick first non-empty decoded
    if best is None:
        for enc in ("cp932", "utf-8", "euc_jp"):
            candidate = decs_out.get(enc)
            if candidate:
                best = candidate
                chosen_enc = enc
                break

    # write debug file with decodings (helpful to inspect)
    try:
        debug_path = os.path.join(TEMP_DIR, f"mecab_yomi_debug_{int(time.time())}.txt")
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(f"mecab_exe: {mecab_exe}\n")
            f.write(f"input repr: {repr(text)[:1000]}\n\n")
            f.write("stdout decodings:\n")
            for enc, dec in decs_out.items():
                f.write(f"--- {enc} ---\n")
                f.write((dec or "")[:4000] + "\n\n")
            f.write("stderr raw (hex prefix):\n")
            f.write((err_bytes or b"")[:1024].hex() + "\n\n")
            f.write(f"chosen_encoding: {chosen_enc}\n")
            f.write(f"chosen_decoded_repr: {repr(best)[:1000]}\n")
        if log_callback:
            try: log_callback(f"[MeCab] wrote debug to {debug_path}") 
            except Exception: pass
    except Exception:
        pass

    return best