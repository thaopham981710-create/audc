# synth_aquestalk.py
# Helper for AquesTalk synthesis.
# - Robust import of submodule aquestalk.aquestalk
# - Caches loaded voice objects to avoid repeated load overhead
# - Provides synthesize_aquestalk_to_file (sync) and synthesize_aquestalk_to_file_async (async)
# - Provides list_aquestalk_voices which prefers directory names and can optionally test-synth each voice.

import os
import sys
import subprocess
import re
import jaconv
import importlib
import threading
from typing import List

_ALLOWED_RE = re.compile(r'[^\u3040-\u309F\u30A0-\u30FF\u3001\u3002\uFF1F\uFF01\u300C\u300D\u30FB\u3000\uFF0C\uFF08\uFF09\u300E\u300F\u30FC\s]')

# simple in-process cache for loaded voice objects
_VOICE_CACHE = {}
_VOICE_CACHE_LOCK = threading.Lock()

def _project_base():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

def _get_mecab_paths():
    base = _project_base()
    mecab_folder = os.path.join(base, "MeCab")
    mecab_exe = os.path.join(mecab_folder, "bin", "mecab.exe")
    if not os.path.isfile(mecab_exe):
        alt = os.path.join(mecab_folder, "mecab.exe")
        if os.path.isfile(alt):
            mecab_exe = alt
    dic_dir = None
    for candidate in ("dic\\ipadic", "dic\\unidic", "dic", "dic\\ipadic-utf8"):
        p = os.path.join(mecab_folder, *candidate.split("\\"))
        if os.path.isdir(p):
            dic_dir = p
            break
    mecabrc = os.path.join(mecab_folder, "etc", "mecabrc") if os.path.isdir(os.path.join(mecab_folder, "etc")) else None
    return mecab_exe, dic_dir, mecabrc

def _mecab_reading_utf8(text: str, timeout: int = 8) -> str:
    mecab_exe, dic_dir, mecabrc = _get_mecab_paths()
    if not mecab_exe or not os.path.isfile(mecab_exe):
        raise FileNotFoundError("mecab.exe không tìm thấy trong MeCab folder.")
    if os.name == "nt":
        dll_dir = os.path.dirname(mecab_exe)
        if dll_dir:
            try:
                os.add_dll_directory(dll_dir)
            except Exception:
                os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
    args = [mecab_exe]
    if dic_dir:
        args += ["-d", dic_dir]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout_bytes, stderr_bytes = proc.communicate(text.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("MeCab timeout")
    if proc.returncode != 0:
        raise RuntimeError("MeCab lỗi: " + stderr_bytes.decode("utf-8", errors="ignore"))
    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    readings = []
    for line in stdout_text.splitlines():
        if line == "EOS" or not line.strip():
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
    return ''.join(readings)

def _sanitize_for_aquestalk(text: str) -> str:
    text = text.replace('-', 'ー')
    text = text.replace(',', '、').replace('?', '？').replace('!', '！').replace('.', '。')
    text = jaconv.h2z(text, digit=True, ascii=False)
    text = re.sub(r'[\(\)（）\[\]［］]', '', text)
    cleaned = _ALLOWED_RE.sub('', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = jaconv.kata2hira(cleaned)
    return cleaned

def _import_aquestalk_submodule():
    """
    Import the konkrent submodule that defines load/synthe functions.
    Try:
      - aquestalk.aquestalk
      - aquestalk (and attribute)
    """
    try:
        return importlib.import_module("aquestalk.aquestalk")
    except Exception:
        try:
            pkg = importlib.import_module("aquestalk")
            if hasattr(pkg, "aquestalk"):
                return pkg.aquestalk
            return pkg
        except Exception as e:
            raise ImportError(f"Không thể import module aquestalk: {e}")

def _get_voice_obj(voice_name: str):
    """
    Load and cache voice object via wrapper's load API (if available).
    voice_name: 'f1', 'm1', etc.
    """
    with _VOICE_CACHE_LOCK:
        if voice_name in _VOICE_CACHE:
            return _VOICE_CACHE[voice_name]
    aqmod = _import_aquestalk_submodule()
    # if wrapper exposes load(voice)
    if hasattr(aqmod, "load"):
        obj = aqmod.load(voice_name)
    elif hasattr(aqmod, "AquesTalk"):
        obj = aqmod.AquesTalk(voice_name)
    else:
        # no load API -> return module itself; many wrappers expose module-level synthe functions
        obj = aqmod
    with _VOICE_CACHE_LOCK:
        _VOICE_CACHE[voice_name] = obj
    return obj

def synthesize_aquestalk_to_file(text: str, output_path: str, voice: str = "f1", speed: int = 100) -> str:
    """
    Synchronously synthesize text to WAV file.
    Returns output_path on success or raises exception.
    """
    if not text:
        raise ValueError("Text empty")
    # 1) get reading via MeCab
    try:
        katakana = _mecab_reading_utf8(text)
    except Exception as e:
        raise RuntimeError(f"MeCab failure: {e}")
    hiragana = jaconv.kata2hira(katakana)
    sanitized = _sanitize_for_aquestalk(hiragana)
    if not sanitized:
        raise RuntimeError("Sanitized text empty")
    # 2) get voice object
    try:
        voice_obj = _get_voice_obj(voice)
    except Exception as e:
        raise RuntimeError(f"Cannot load AquesTalk voice '{voice}': {e}")
    # 3) call synth - support multiple API shapes
    raw_bytes = None
    try:
        if hasattr(voice_obj, "synthe_raw"):
            try:
                raw_bytes = voice_obj.synthe_raw(sanitized, speed)
            except TypeError:
                raw_bytes = voice_obj.synthe_raw(sanitized)
        elif hasattr(voice_obj, "synthe"):
            raw = voice_obj.synthe(sanitized)
            # some synthe return bytes directly, others return memoryview/bytearray
            raw_bytes = raw if isinstance(raw, (bytes, bytearray)) else bytes(raw)
        elif hasattr(voice_obj, "synth") or hasattr(voice_obj, "synthesize"):
            fn = getattr(voice_obj, "synth", None) or getattr(voice_obj, "synthesize", None)
            raw = fn(sanitized)
            raw_bytes = raw if isinstance(raw, (bytes, bytearray)) else bytes(raw)
        else:
            # module-level functions?
            mod = voice_obj
            if hasattr(mod, "synthe_raw"):
                raw_bytes = mod.synthe_raw(sanitized, speed) if callable(getattr(mod, "synthe_raw")) else None
            elif hasattr(mod, "synthe"):
                raw = mod.synthe(sanitized)
                raw_bytes = raw if isinstance(raw, (bytes, bytearray)) else bytes(raw)
    except Exception as e:
        raise RuntimeError(f"AquesTalk synth failed for '{voice}': {e}")

    if not raw_bytes or not isinstance(raw_bytes, (bytes, bytearray)):
        raise RuntimeError("AquesTalk returned non-bytes result")
    # write WAV bytes to file
    with open(output_path, "wb") as f:
        f.write(raw_bytes)
    return output_path

# async wrapper convenience
async def synthesize_aquestalk_to_file_async(text: str, output_path: str, voice: str = "f1", speed: int = 100):
    loop = __import__("asyncio").get_event_loop()
    return await loop.run_in_executor(None, synthesize_aquestalk_to_file, text, output_path, voice, speed)

def list_aquestalk_voices(candidates: List[str] = None, try_short_test: bool = False) -> List[str]:
    """
    Return list of voice names available.
    Strategy:
      - scan project/aquestalk/aquestalk subfolders (f1,f2...)
      - if wrapper available, optionally try quick synth (slow)
    """
    base = _project_base()
    candidate_dir = os.path.join(base, "aquestalk", "aquestalk")
    if not os.path.isdir(candidate_dir):
        candidate_dir = os.path.join(base, "aquestalk")
    voices = []
    if os.path.isdir(candidate_dir):
        for entry in sorted(os.listdir(candidate_dir)):
            p = os.path.join(candidate_dir, entry)
            if os.path.isdir(p):
                voices.append(entry)
    # if explicit candidates passed, prefer them
    if candidates:
        voices = [v for v in candidates if v in voices] or candidates
    # optional short test (may be slow)
    if try_short_test and voices:
        ok = []
        try:
            for v in voices:
                try:
                    _get_voice_obj(v)  # try load
                    ok.append(v)
                except Exception:
                    continue
        except Exception:
            pass
        if ok:
            return ok
    return voices