#!/usr/bin/env python3
# video_worker.py
# Backend for Auto Video App (rendering, TTS orchestration, MeCab/AquesTalk integration)
# Full version (v24-based) with improved encoder smoke-test to avoid false negatives on NVENC/AMF/QSV
# - Clause-based AquesTalk synth with robust per-clause fallback
# - aq_normalize integration (optional)
# - Thread/GPU tuning: AUTO_VIDEO_MAX_THREADS, AUTO_VIDEO_PREFER_GPU, AUTO_VIDEO_FORCE_ENCODER
# - FFmpeg encoder detection preferring NVENC/AMF/QSV when available
# - Improved smoke-test: uses larger test frame (128x128, fallback 256x256) and captures stderr snippet
# - Many FFmpeg robustness fallbacks and logging helpers
#
# Replace your existing video_worker.py with this complete file (it contains the full logic).
# Restart the app from the same console after replacing the file so environment variables take effect.

import sys
import os
import subprocess
import tempfile
import re
import asyncio
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageDraw, ImageFont
import requests
import json
import shutil as _shutil
import shutil
import time
import uuid
import hashlib

# Configuration from environment (allow user override)
# AUTO_VIDEO_MAX_THREADS: maximum CPU threads used by app (thread pool + ffmpeg -threads cap). Default 24.
# AUTO_VIDEO_PREFER_GPU: "1" to prefer GPU encoders (nvenc, amf, qsv) if ffmpeg supports them. Default 1.
# AUTO_VIDEO_FORCE_ENCODER: optional. If set to a encoder name (eg "h264_nvenc"), will try to force using it.
_MAX_THREADS_ENV = int(os.environ.get("AUTO_VIDEO_MAX_THREADS", "24"))
_AUTO_VIDEO_PREFER_GPU = os.environ.get("AUTO_VIDEO_PREFER_GPU", "1") == "1"
_AUTO_VIDEO_FORCE_ENCODER = os.environ.get("AUTO_VIDEO_FORCE_ENCODER", "").strip()

# Ensure positive
_MAX_THREADS = max(1, _MAX_THREADS_ENV)
# threads string used for ffmpeg '-threads'
_FFMPEG_THREADS_STR = str(min(_MAX_THREADS, max(1, (os.cpu_count() or 1))))

# Try import normalization helper (optional)
try:
    from aq_normalize import normalize_for_aquestalk
except Exception:
    normalize_for_aquestalk = None

# Base dir (handles frozen exe)
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

EFFECTS_DIR = os.path.join(BASE_DIR, "effects")

# Windows startupinfo / flags
if sys.platform == "win32":
    try:
        CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
    except Exception:
        CREATE_NO_WINDOW = 0
    si = subprocess.STARTUPINFO()
    try:
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
    except Exception:
        pass
else:
    CREATE_NO_WINDOW = 0
    si = None

# temp dir and executor
output_temp_dir = tempfile.gettempdir()
# ThreadPoolExecutor capped by _MAX_THREADS so CPU usage won't spawn more than this many worker threads
executor = ThreadPoolExecutor(max_workers=_MAX_THREADS)

# Minimum sample rate to enforce for AquesTalk outputs to avoid pitch/speed artifacts
MIN_SR_ENFORCE = int(os.environ.get("AQUESTALK_MIN_SR", "16000"))

# Try import mecab_helper (project may provide it)
try:
    from mecab_helper import init_mecab, mecab_yomi, find_mecab_executable
    try:
        init_mecab(BASE_DIR)
    except Exception:
        pass
except Exception:
    def mecab_yomi(text, base_dir=None, timeout=6, log_callback=None):
        return None
    def find_mecab_executable(base_dir=None):
        candidate = os.path.join(base_dir or BASE_DIR, "MeCab", "bin", "mecab.exe")
        return candidate if os.path.exists(candidate) else None

# Try import synth_aquestalk wrapper (async)
try:
    from synth_aquestalk import synthesize_aquestalk_to_file_async, list_aquestalk_voices
    _HAS_AQUESTALK = True
except Exception:
    # synth_aquestalk might not be available
    _HAS_AQUESTALK = False

# ---------------- helper --------------------------------------------------
def _dbg(msg: str, log_callback=None):
    try:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass
        print(msg)
    except Exception:
        pass

# Print whether aq_normalize was imported (debug)
try:
    _HAS_AQ_NORMALIZE = normalize_for_aquestalk is not None
except Exception:
    _HAS_AQ_NORMALIZE = False
_dbg(f"[Init] aq_normalize present: {_HAS_AQ_NORMALIZE}")
_dbg(f"[Init] AUTO_VIDEO_MAX_THREADS={_MAX_THREADS}, AUTO_VIDEO_PREFER_GPU={_AUTO_VIDEO_PREFER_GPU}, AUTO_VIDEO_FORCE_ENCODER='{_AUTO_VIDEO_FORCE_ENCODER}', ffmpeg -threads={_FFMPEG_THREADS_STR}")

# ---------------- FFmpeg / probe helpers ------------------------------
def get_ffmpeg_path():
    p = os.path.join(BASE_DIR, "ffmpeg", "ffmpeg.exe")
    if os.path.exists(p):
        return p
    return shutil.which("ffmpeg") or "ffmpeg"

def get_ffprobe_path():
    p = os.path.join(BASE_DIR, "ffmpeg", "ffprobe.exe")
    if os.path.exists(p):
        return p
    return shutil.which("ffprobe") or "ffprobe"

def normalize_path_for_ffmpeg(path):
    return os.path.normpath(path).replace('\\', '/')

def _ffmpeg_supports_soxr(ffmpeg_path=None):
    ffmpeg_path = ffmpeg_path or get_ffmpeg_path()
    try:
        res = subprocess.run([ffmpeg_path, "-hide_banner", "-h", "filter=aresample"], capture_output=True, text=True, timeout=6)
        out = (res.stdout or "") + (res.stderr or "")
        if "soxr" in out.lower():
            return True
    except Exception:
        pass
    try:
        res2 = subprocess.run([ffmpeg_path, "-hide_banner", "-version"], capture_output=True, text=True, timeout=6)
        out2 = (res2.stdout or "") + (res2.stderr or "")
        if "--enable-libsoxr" in out2.lower() or "libsoxr" in out2.lower():
            return True
    except Exception:
        pass
    return False

_HAS_SOXR = _ffmpeg_supports_soxr()

if _HAS_SOXR:
    _dbg("[Init] FFmpeg supports libsoxr (will use soxr resampler)")
else:
    _dbg("[Init] FFmpeg libsoxr not detected; will use aresample filter fallback")

def soxr_filter(out_sr):
    if _HAS_SOXR:
        return f"aresample=resampler=soxr:osr={int(out_sr)}:comp_duration=0"
    return None

def build_audio_resample_args(target_sr):
    if _HAS_SOXR:
        return ['-af', f"aresample=resampler=soxr:osr={int(target_sr)}:comp_duration=0", '-ac', '1']
    else:
        return ['-af', f"aresample={int(target_sr)}:comp_duration=0", '-ac', '1']

# ---------------- Encoder detection (improved ordered by GPU preference) -------------------------
_ENCODER_LOCK = threading.Lock()
_ENCODER_CHOICE = None

def _test_encoder_run(ffmpeg_path, encoder_name, timeout=20, test_w=128, test_h=128):
    """
    Smoke-test an encoder by encoding a short synthetic clip.
    Use a larger default test size (128x128), because some hardware encoders reject tiny frames (e.g. 16x16).
    Returns (success: bool, stderr_snippet: str).
    """
    try:
        cmd = [
            ffmpeg_path, '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', f"color=size={test_w}x{test_h}:duration=0.06:rate=25:color=black",
            '-pix_fmt', 'yuv420p',
            '-c:v', encoder_name,
            '-f', 'null', '-'
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        out = (proc.stdout or b"").decode('utf-8', errors='ignore')
        err = (proc.stderr or b"").decode('utf-8', errors='ignore')
        _dbg(f"[TestEncoder] cmd={' '.join(cmd)} returncode={proc.returncode} stderr_len={len(err)}")
        if proc.returncode == 0:
            return True, ""
        snippet = err.strip()[:2000] or out.strip()[:2000]
        return False, snippet
    except subprocess.TimeoutExpired:
        _dbg(f"[TestEncoder] smoke-test timed out for {encoder_name}")
        return False, "timeout"
    except Exception as e:
        _dbg(f"[TestEncoder] smoke-test exception for {encoder_name}: {e}")
        return False, str(e)

def _ffmpeg_has_encoder(ffmpeg_path, encoder_name):
    try:
        res = subprocess.run([ffmpeg_path, "-hide_banner", "-encoders"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=6)
        enc = (res.stdout or "").lower()
        return encoder_name.lower() in enc
    except Exception:
        return False

def detect_best_encoder():
    """
    Prefer GPU encoders when AUTO_VIDEO_PREFER_GPU=1:
      - NVENC (h264_nvenc) for NVIDIA
      - AMF (h264_amf) for AMD
      - QSV (h264_qsv) for Intel
    Honor AUTO_VIDEO_FORCE_ENCODER if set and supported by ffmpeg.
    Fallback to libx264 if no GPU encoder usable.

    NOTE: improved smoke-test (uses 128x128, try 256x256 if needed, logs stderr snippet).
    """
    global _ENCODER_CHOICE
    with _ENCODER_LOCK:
        if _ENCODER_CHOICE is not None:
            return _ENCODER_CHOICE
        ffmpeg_path = get_ffmpeg_path()
        _dbg(f"[DetectEncoder] ffmpeg path = {ffmpeg_path}")
        # default fallback
        _ENCODER_CHOICE = "libx264"
        if not os.path.exists(ffmpeg_path):
            _dbg("[DetectEncoder] ffmpeg not found on path or bundled; defaulting to libx264")
            return _ENCODER_CHOICE

        # Gather encoders list once
        try:
            res = subprocess.run([ffmpeg_path, "-hide_banner", "-encoders"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8)
            enc = (res.stdout or "").lower()
        except Exception:
            enc = ""

        # If user supplied a forced encoder (env), prefer it if available and testable
        if _AUTO_VIDEO_FORCE_ENCODER:
            fe = _AUTO_VIDEO_FORCE_ENCODER.strip()
            if fe.lower() in enc:
                _dbg(f"[DetectEncoder] AUTO_VIDEO_FORCE_ENCODER requested: {fe} found in ffmpeg encoders, attempting smoke-test")
                ok, snippet = _test_encoder_run(ffmpeg_path, fe, timeout=20, test_w=128, test_h=128)
                if ok:
                    _ENCODER_CHOICE = fe
                    _dbg(f"[DetectEncoder] AUTO_VIDEO_FORCE_ENCODER -> using {fe}")
                    return _ENCODER_CHOICE
                else:
                    _dbg(f"[DetectEncoder] AUTO_VIDEO_FORCE_ENCODER '{fe}' smoke-test failed (128x128) snippet: {snippet}")
                    # try larger frame
                    ok2, snippet2 = _test_encoder_run(ffmpeg_path, fe, timeout=25, test_w=256, test_h=256)
                    if ok2:
                        _ENCODER_CHOICE = fe
                        _dbg(f"[DetectEncoder] AUTO_VIDEO_FORCE_ENCODER -> using {fe} (passed at 256x256)")
                        return _ENCODER_CHOICE
                    _dbg(f"[DetectEncoder] AUTO_VIDEO_FORCE_ENCODER '{fe}' still failed at 256x256 snippet: {snippet2}")
            else:
                _dbg(f"[DetectEncoder] AUTO_VIDEO_FORCE_ENCODER '{fe}' not found in ffmpeg encoders list; ignoring")

        # If user does not want GPU pref, just use libx264
        if not _AUTO_VIDEO_PREFER_GPU:
            _ENCODER_CHOICE = "libx264"
            _dbg("[DetectEncoder] AUTO_VIDEO_PREFER_GPU disabled -> using libx264")
            return _ENCODER_CHOICE

        # prefer nvenc -> amf -> qsv
        try:
            if "h264_nvenc" in enc:
                ok, snippet = _test_encoder_run(ffmpeg_path, "h264_nvenc", timeout=20, test_w=128, test_h=128)
                if ok:
                    _ENCODER_CHOICE = "h264_nvenc"
                    _dbg("[DetectEncoder] chosen encoder: h264_nvenc")
                    return _ENCODER_CHOICE
                _dbg(f"[DetectEncoder] h264_nvenc smoke failed (128x128): {snippet}; trying 256x256")
                ok2, snippet2 = _test_encoder_run(ffmpeg_path, "h264_nvenc", timeout=25, test_w=256, test_h=256)
                if ok2:
                    _ENCODER_CHOICE = "h264_nvenc"
                    _dbg("[DetectEncoder] chosen encoder: h264_nvenc (passed 256x256)")
                    return _ENCODER_CHOICE
                _dbg(f"[DetectEncoder] h264_nvenc failed at 256x256: {snippet2}")
            if "h264_amf" in enc:
                ok, snippet = _test_encoder_run(ffmpeg_path, "h264_amf", timeout=20, test_w=128, test_h=128)
                if ok:
                    _ENCODER_CHOICE = "h264_amf"
                    _dbg("[DetectEncoder] chosen encoder: h264_amf")
                    return _ENCODER_CHOICE
            if "h264_qsv" in enc:
                ok, snippet = _test_encoder_run(ffmpeg_path, "h264_qsv", timeout=20, test_w=128, test_h=128)
                if ok:
                    _ENCODER_CHOICE = "h264_qsv"
                    _dbg("[DetectEncoder] chosen encoder: h264_qsv")
                    return _ENCODER_CHOICE
            _ENCODER_CHOICE = "libx264"
        except Exception:
            _ENCODER_CHOICE = "libx264"
        _dbg(f"[DetectEncoder] chosen encoder: {_ENCODER_CHOICE}")
        return _ENCODER_CHOICE

def _start_encoder_probe_background():
    def worker():
        detect_best_encoder()
    t = threading.Thread(target=worker, daemon=True)
    t.start()
_start_encoder_probe_background()

def run_ffmpeg_with_fallback(cmd, encoder_gpu, fallback_encoder="libx264", si=None, log_callback=None):
    try:
        _dbg(f"[FFmpeg] running: {' '.join(cmd)}", log_callback=log_callback)
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=si, creationflags=(CREATE_NO_WINDOW if sys.platform=="win32" else 0))
        return True
    except subprocess.CalledProcessError as e:
        try:
            stderr = e.stderr.decode('utf-8', errors='ignore') if isinstance(e.stderr, (bytes, bytearray)) else str(e.stderr)
        except Exception:
            stderr = str(e)
        if log_callback:
            try: log_callback(f"[FFmpeg] failed: {stderr}")
            except Exception: pass
        if "-c:v" in cmd and encoder_gpu:
            try:
                idx = cmd.index("-c:v")
                if cmd[idx+1] == encoder_gpu:
                    cmd2 = list(cmd)
                    cmd2[idx+1] = fallback_encoder
                    try:
                        subprocess.run(cmd2, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=si, creationflags=(CREATE_NO_WINDOW if sys.platform=="win32" else 0))
                        if log_callback:
                            try: log_callback(f"[FFmpeg] fallback to {fallback_encoder} succeeded")
                            except Exception: pass
                        return True
                    except subprocess.CalledProcessError as e2:
                        try:
                            stderr2 = e2.stderr.decode('utf-8', errors='ignore') if isinstance(e2.stderr, (bytes, bytearray)) else str(e2.stderr)
                        except Exception:
                            stderr2 = str(e2)
                        if log_callback:
                            try: log_callback(f"[FFmpeg] fallback failed: {stderr2}")
                            except Exception: pass
                        return False
            except Exception:
                pass
        return False

# ---------------- Audio helpers -----------------------------------------
def get_audio_sample_rate(path):
    ffprobe_path = get_ffprobe_path()
    if not ffprobe_path or not os.path.exists(ffprobe_path):
        return None
    try:
        cmd = [ffprobe_path, '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=sample_rate',
               '-of', 'default=noprint_wrappers=1:nokey=1', normalize_path_for_ffmpeg(path)]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        s = res.stdout.strip()
        if s:
            return int(float(s))
    except Exception:
        pass
    return None

def get_audio_channels(path):
    ffprobe_path = get_ffprobe_path()
    if not ffprobe_path or not os.path.exists(ffprobe_path):
        return None
    try:
        cmd = [ffprobe_path, '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=channels',
               '-of', 'default=noprint_wrappers=1:nokey=1', normalize_path_for_ffmpeg(path)]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        s = res.stdout.strip()
        if s:
            return int(float(s))
    except Exception:
        pass
    return None

def get_audio_codec(path):
    ffprobe_path = get_ffprobe_path()
    if not ffprobe_path or not os.path.exists(ffprobe_path):
        return None
    try:
        cmd = [ffprobe_path, '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=codec_name',
               '-of', 'default=noprint_wrappers=1:nokey=1', normalize_path_for_ffmpeg(path)]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        s = (res.stdout or "").strip()
        return s or None
    except Exception:
        return None

def get_silence_wav_path(duration, sample_rate=24000):
    sr = int(sample_rate or 24000)
    silence_path = os.path.join(output_temp_dir, f"silence_{duration:.2f}_{sr}.wav")
    if not os.path.exists(silence_path):
        ffmpeg_path = get_ffmpeg_path()
        subprocess.run([ffmpeg_path, '-y', '-threads', _FFMPEG_THREADS_STR, '-f', 'lavfi', '-i', f"anullsrc=r={sr}:cl=mono", '-t', str(duration),
                        '-q:a', '9', '-acodec', 'pcm_s16le', silence_path], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return silence_path

TRIM_TTS_TRAILING_SILENCE = False
TRIM_THRESHOLD_DB = -45
TRIM_MIN_SILENCE_SEC = 0.08

def trim_trailing_silence(input_wav, output_wav, threshold_db=TRIM_THRESHOLD_DB, min_silence=TRIM_MIN_SILENCE_SEC):
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg or not os.path.exists(ffmpeg):
        return False
    af = f"silenceremove=stop_periods=1:stop_duration={min_silence}:stop_threshold={threshold_db}dB"
    try:
        subprocess.run([ffmpeg, '-y', '-threads', _FFMPEG_THREADS_STR, '-i', normalize_path_for_ffmpeg(input_wav), '-af', af, normalize_path_for_ffmpeg(output_wav)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return os.path.exists(output_wav) and os.path.getsize(output_wav) > 512
    except Exception:
        return False

async def concat_audio_with_silence(audio_path, silence_duration, log_callback=None):
    if silence_duration <= 0:
        return audio_path

    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path or not os.path.exists(ffmpeg_path):
        return audio_path

    try:
        sr = get_audio_sample_rate(audio_path) or MIN_SR_ENFORCE
    except Exception:
        sr = MIN_SR_ENFORCE
    TARGET_SR = max(int(sr), MIN_SR_ENFORCE)
    TARGET_CH = 1

    base = os.path.splitext(os.path.basename(audio_path))[0]
    resampled = os.path.join(output_temp_dir, f"{base}_res_{TARGET_SR}hz.wav")
    silence_src = get_silence_wav_path(silence_duration, sample_rate=TARGET_SR)
    concat_list = os.path.join(output_temp_dir, f"concat_{base}_{int(time.time())}.txt")
    padded_out = os.path.join(output_temp_dir, f"{base}_padded.wav")

    try:
        _dbg(f"[concat_audio_with_silence] Re-encoding {audio_path} -> {resampled} @ {TARGET_SR}Hz", log_callback=log_callback)
        cmd = [ffmpeg_path, '-y', '-threads', _FFMPEG_THREADS_STR, '-i', normalize_path_for_ffmpeg(audio_path)]
        cmd += build_audio_resample_args(TARGET_SR)
        cmd += ['-c:a', 'pcm_s16le', normalize_path_for_ffmpeg(resampled)]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as ex:
        _dbg(f"[concat_audio_with_silence] Re-encode failed: {ex}", log_callback=log_callback)
        try:
            if os.path.exists(resampled):
                os.remove(resampled)
        except Exception:
            pass
        return audio_path

    try:
        with open(concat_list, "w", encoding="utf-8") as f:
            f.write(f"file '{normalize_path_for_ffmpeg(resampled)}'\n")
            f.write(f"file '{normalize_path_for_ffmpeg(silence_src)}'\n")
        cmd = [ffmpeg_path, '-y', '-threads', _FFMPEG_THREADS_STR, '-f', 'concat', '-safe', '0', '-i', concat_list]
        cmd += build_audio_resample_args(TARGET_SR)
        cmd += ['-c:a', 'pcm_s16le', normalize_path_for_ffmpeg(padded_out)]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            if os.path.exists(resampled):
                os.remove(resampled)
        except Exception:
            pass
        try:
            if os.path.exists(concat_list):
                os.remove(concat_list)
        except Exception:
            pass
        if os.path.exists(padded_out) and os.path.getsize(padded_out) > 512:
            _dbg(f"[concat_audio_with_silence] Result: {padded_out} size={os.path.getsize(padded_out)} sr={get_audio_sample_rate(padded_out)}", log_callback=log_callback)
            return padded_out
    except Exception as ex:
        _dbg(f"[concat_audio_with_silence] concat failed: {ex}", log_callback=log_callback)
        try:
            fallback_out = os.path.join(output_temp_dir, f"{base}_padded_fallback.wav")
            cmd = [ffmpeg_path, '-y', '-threads', _FFMPEG_THREADS_STR, '-i', normalize_path_for_ffmpeg(resampled), '-i', normalize_path_for_ffmpeg(silence_src),
                   '-filter_complex', '[0:a][1:a]concat=n=2:v=0:a=1[out]', '-map', '[out]']
            cmd += build_audio_resample_args(TARGET_SR)
            cmd += ['-c:a', 'pcm_s16le', normalize_path_for_ffmpeg(fallback_out)]
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                if os.path.exists(resampled):
                    os.remove(resampled)
            except Exception:
                pass
            if os.path.exists(fallback_out) and os.path.getsize(fallback_out) > 512:
                _dbg(f"[concat_audio_with_silence] Fallback result: {fallback_out} size={os.path.getsize(fallback_out)} sr={get_audio_sample_rate(fallback_out)}", log_callback=log_callback)
                return fallback_out
        except Exception as ex2:
            _dbg(f"[concat_audio_with_silence] Fallback also failed: {ex2}", log_callback=log_callback)
            pass

    return audio_path

def get_audio_duration(path):
    ffprobe = get_ffprobe_path()
    if ffprobe is None or not os.path.exists(ffprobe):
        return 5.0
    try:
        cmd = [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
               '-of', 'default=noprint_wrappers=1:nokey=1', normalize_path_for_ffmpeg(path)]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception:
        return 5.0

def compute_md5(path):
    try:
        h = hashlib.md5()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None

# ---------------- AquesTalk helpers / sanitizers ------------------------
_RETAIN_JP = re.compile(r'[^\u3000-\u30FF\u4E00-\u9FFF\uFF01-\uFF60\u3001\u3002\u30FB\u30FC\s、。！？]')

def sanitize_for_aquestalk_fallback(text: str) -> str:
    if not text:
        return text
    s = _RETAIN_JP.sub('', text)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

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
    return ''.join(out_chars)

def sanitize_yomi_keep_katakana(yomi: str) -> str:
    if not yomi:
        return yomi
    s = hira_to_kata(yomi)
    s = s.replace(',', '、').replace('.', '。').replace('?', '？').replace('!', '！')
    s = s.replace(';', '、').replace(':', '、')
    s = s.replace('“', '').replace('”', '').replace('‘', '').replace('’', '')
    s = s.replace('"', '').replace("'", '')
    s = re.sub(r'[^ァ-ヴー\u3000\s、。！？]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def to_fullwidth_digits(s: str) -> str:
    if not s:
        return s
    return s.translate(str.maketrans("0123456789", "０１２３４５６７８９"))

# ---------------- Aggressive sanitize (new) ----------------------------
_AGGRESSIVE_REMOVE_RE = re.compile(r'[^\u3000-\u30FF\u4E00-\u9FFF\uFF01-\uFF60\u3001\u3002\u30FB\u30FC\s、。！？ーァ-ヴー]')
_AGGRESSIVE_PUNCT_RE = re.compile(r'[「」『』【】＜＞〈〉《》\[\]\(\)<>「」\{\}<>]')

def aggressive_sanitize(text: str) -> str:
    """
    Aggressive sanitize for AquesTalk 105 fallback:
    - Convert ASCII digits to fullwidth
    - Remove common bracket/quote characters and Latin letters aggressively
    - Convert hiragana to katakana for yomi-style variants
    Returns sanitized text (may be empty if nothing left).
    """
    if not text:
        return text
    # fullwidth digits
    t = to_fullwidth_digits(text)
    # remove bracket-like punctuation
    t = _AGGRESSIVE_PUNCT_RE.sub('', t)
    # remove ASCII letters and many punctuation that may cause 105
    t = _AGGRESSIVE_REMOVE_RE.sub('', t)
    t = re.sub(r'\s+', ' ', t).strip()
    # as extra variant, also produce Katakana-only variant
    kat = hira_to_kata(t)
    kat = re.sub(r'[^ァ-ヴー\u3000\s、。！？]', '', kat)
    kat = re.sub(r'\s+', ' ', kat).strip()
    # return the more "Japanese-only" form
    return kat or t

# ---------------- MeCab CLI fallback ------------------------------
def get_mecab_yomi_via_exe(text: str, base_dir=BASE_DIR, log_callback=None, timeout=6):
    if not text:
        return None
    exe = None
    try:
        exe = find_mecab_executable(base_dir) if 'find_mecab_executable' in globals() else None
    except Exception:
        exe = None
    if not exe:
        candidate = os.path.join(base_dir, "MeCab", "bin", "mecab.exe")
        if os.path.exists(candidate):
            exe = candidate
    if not exe or not os.path.exists(exe):
        if log_callback:
            log_callback("[MeCab-CLI] mecab executable not found for -Oyomi fallback")
        return None
    try:
        p = subprocess.Popen([exe, "-Oyomi"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate(input=text.encode("utf-8"), timeout=timeout)
        try:
            y = out.decode("utf-8").strip()
        except Exception:
            try:
                y = out.decode("cp932").strip()
            except Exception:
                y = out.decode("utf-8", errors="ignore").strip()
        if log_callback:
            log_callback(f"[MeCab-CLI] -Oyomi returned len={len(y)}")
        return y
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except Exception:
            pass
        if log_callback:
            log_callback("[MeCab-CLI] -Oyomi timed out")
        return None
    except Exception as e:
        if log_callback:
            log_callback(f"[MeCab-CLI] -Oyomi failed: {e}")
        return None

# ---------------- overlay_icon_ab implementation ----------------
def overlay_icon_ab(
    input_video_path,
    speak_role,
    output_path,
    icon_a_dir,
    icon_b_dir,
    icon_pos_a=(30, 0),
    icon_pos_b=(30, 0),
    icon_size=(240, 240),
    subtitle_height=120,
    video_height=720,
    padding=20,
    duration=None,
    log_callback=None
):
    from pathlib import Path
    ffmpeg_path = get_ffmpeg_path()
    ffprobe_path = get_ffprobe_path()
    if ffmpeg_path is None or not os.path.exists(ffmpeg_path):
        raise FileNotFoundError("ffmpeg.exe not found")
    if ffprobe_path is None or not os.path.exists(ffprobe_path):
        raise FileNotFoundError("ffprobe.exe not found")
    icon_a = Path(icon_a_dir) / ("talk.mov" if speak_role == "A" else "idle.mov")
    icon_b = Path(icon_b_dir) / ("idle.mov" if speak_role == "A" else "talk.mov")
    if not icon_a.exists() or not icon_b.exists():
        raise FileNotFoundError(f"Icon files missing: {icon_a} / {icon_b}")
    if duration is None:
        try:
            result = subprocess.run([ffprobe_path, '-v', 'error', '-show_entries', 'format=duration',
                                     '-of', 'default=noprint_wrappers=1:nokey=1', normalize_path_for_ffmpeg(str(input_video_path))],
                                    capture_output=True, text=True, check=True)
            duration = float(result.stdout.strip() or "0.01")
        except Exception:
            duration = 0.01
    icon_y = video_height - subtitle_height - icon_size[1] - padding
    if icon_y < 0:
        icon_y = padding
    filter_complex = f"[0:v][1:v]overlay={icon_pos_a[0]}:{icon_y}[tmp1];[tmp1][2:v]overlay=W-w-{icon_pos_b[0]}:{icon_y}[vout]"

    try:
        input_codec = get_audio_codec(input_video_path)
        input_sr = get_audio_sample_rate(input_video_path)
    except Exception:
        input_codec = None
        input_sr = None

    cmd = [
        ffmpeg_path, "-y",
        "-threads", _FFMPEG_THREADS_STR,
        "-i", normalize_path_for_ffmpeg(str(input_video_path)),
        "-stream_loop", "-1", "-t", f"{duration:.3f}", "-i", normalize_path_for_ffmpeg(str(icon_a)),
        "-stream_loop", "-1", "-t", f"{duration:.3f}", "-i", normalize_path_for_ffmpeg(str(icon_b)),
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "0:a?"
    ]

    try:
        input_ch = get_audio_channels(input_video_path)
    except Exception:
        input_ch = None

    if input_codec and input_codec.lower() in ('aac',) and input_sr and int(input_sr) >= MIN_SR_ENFORCE and (input_ch == 1 or input_ch is None):
        cmd += ['-c:a', 'copy']
    else:
        if _HAS_SOXR:
            cmd += ['-af', soxr_filter(MIN_SR_ENFORCE), '-ac', '1', '-c:a', 'aac', '-b:a', '128k']
        else:
            cmd += ['-af', f"aresample={MIN_SR_ENFORCE}:comp_duration=0", '-ac', '1', '-c:a', 'aac', '-b:a', '128k']

    # video encoder choice: use detected encoder (pref gpu) if available, otherwise libx264
    encoder_choice = detect_best_encoder()
    cmd += ["-c:v", encoder_choice, normalize_path_for_ffmpeg(str(output_path))]

    _dbg(f"[overlay_icon_ab] running ffmpeg for overlay (input_codec={input_codec} input_sr={input_sr}) encoder={encoder_choice}", log_callback=log_callback)
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=si, creationflags=(CREATE_NO_WINDOW if sys.platform=="win32" else 0))

# ---------------- per-sentence logging helper --------------------------
def _log_sentence_result(index, original, prepped, yomi_raw, yomi_clean, text_to_synth, voice_name, result, extra_msg=None):
    line = f"câu {index} => MeCab: {repr(yomi_raw)[:200]} => AquesTalk: {result}"
    if extra_msg:
        line += f" ({extra_msg})"
    try:
        print(line)
    except Exception:
        pass
    try:
        fn = os.path.join(output_temp_dir, "aquestalk_sentence_log.txt")
        with open(fn, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.write("  original: " + (original or "") + "\n")
            f.write("  prepped: " + (prepped or "") + "\n")
            f.write("  yomi_raw: " + (yomi_raw or "") + "\n")
            f.write("  yomi_clean: " + (yomi_clean or "") + "\n")
            f.write("  text_to_synth: " + (text_to_synth or "") + "\n")
            if extra_msg:
                f.write("  extra: " + extra_msg + "\n")
            f.write("---\n")
    except Exception:
        pass

# ---------------- heuristic for problematic originals ----------------
_problematic_re = re.compile(r'[A-Za-z0-9\[\]\(\)<>@#\$%\^&\*\\\/~`_=\+\|\:;\"\'\<\>]|[“”‘’…\-–—]')
def original_is_likely_problematic(original: str) -> bool:
    if not original:
        return False
    if _problematic_re.search(original):
        return True
    if re.search(r'\d[一-龯]|[一-龯]\d', original):
        return True
    if re.search(r'\d+[万億兆]', original):
        return True
    return False

# ---------------- Clause-based AquesTalk synthesis helper ----------------
# (Full implementation included earlier in v24; kept exactly as in v24 to preserve behavior.)
async def synthesize_aquestalk_clauses(original_text, voice, out_wav, speed, log_callback=None, index=None, pause_map=None):
    if not _HAS_AQUESTALK:
        return False

    DEFAULT_CLAUSE_PAUSE = 0.3
    if pause_map is None:
        pause_map = {
            "、": DEFAULT_CLAUSE_PAUSE, ",": DEFAULT_CLAUSE_PAUSE,
            "。": DEFAULT_CLAUSE_PAUSE, ".": DEFAULT_CLAUSE_PAUSE, "．": DEFAULT_CLAUSE_PAUSE,
            "！": DEFAULT_CLAUSE_PAUSE, "!": DEFAULT_CLAUSE_PAUSE,
            "？": DEFAULT_CLAUSE_PAUSE, "?": DEFAULT_CLAUSE_PAUSE,
            "；": DEFAULT_CLAUSE_PAUSE, ";": DEFAULT_CLAUSE_PAUSE,
            "，": DEFAULT_CLAUSE_PAUSE
        }

    parts = re.split(r'([、。．!.?！？,，;；])', original_text)
    clauses = []
    for i in range(0, len(parts), 2):
        part = parts[i].strip()
        delim = parts[i+1] if i+1 < len(parts) else ""
        if part or delim:
            clauses.append((part, delim))
    if not clauses:
        clauses = [(original_text.strip(), "")]

    temp_files = []
    pause_after_list = []

    async def _try_synth_one_clause(text_to_try, outfile):
        try:
            await synthesize_aquestalk_to_file_async(text_to_try, outfile, str(voice), speed)
            if not os.path.exists(outfile) or os.path.getsize(outfile) <= 512:
                return False, "no-output-or-too-small"
            return True, None
        except Exception as e:
            return False, str(e or "")

    try:
        for i, (clause_text, delim) in enumerate(clauses):
            synth_text = re.sub(r'[、，,]+$','', clause_text).strip() or clause_text or ""
            tmp_out_base = os.path.join(output_temp_dir, f"aquestalk_clause_{uuid.uuid4().hex}_{i}")
            tmp_out = tmp_out_base + ".wav"

            if log_callback:
                log_callback(f"[AquesTalk-clause] idx={index} clause={i+1}/{len(clauses)} delim={repr(delim)} synth_len={len(synth_text)}")

            candidates = []
            candidates.append(("original", synth_text))

            try:
                y = None
                try:
                    y = mecab_yomi(synth_text, base_dir=BASE_DIR, log_callback=log_callback)
                except Exception:
                    y = None
                if not y:
                    y = get_mecab_yomi_via_exe(synth_text, base_dir=BASE_DIR, log_callback=log_callback, timeout=6)
                if y:
                    yk = sanitize_yomi_keep_katakana(y)
                    if yk and len(yk) >= 1:
                        candidates.append(("mecab_yomi_kana", yk))
            except Exception:
                pass

            try:
                ag = aggressive_sanitize(synth_text)
                if ag and ag not in [c[1] for c in candidates]:
                    candidates.append(("aggressive", ag))
            except Exception:
                pass

            clause_out = None
            clause_ok = False
            last_err = None

            for cand_name, cand_text in candidates:
                if log_callback:
                    log_callback(f"[AquesTalk-clause] idx={index} clause={i+1} trying candidate={cand_name} len={len(cand_text)}")
                ok, emsg = await _try_synth_one_clause(cand_text, tmp_out)
                if ok:
                    norm_tf = tmp_out_base + f"_norm_{MIN_SR_ENFORCE}.wav"
                    try:
                        ffmpeg = get_ffmpeg_path()
                        cmd = [ffmpeg, '-y', '-threads', _FFMPEG_THREADS_STR, '-i', normalize_path_for_ffmpeg(tmp_out)]
                        cmd += build_audio_resample_args(MIN_SR_ENFORCE)
                        cmd += ['-c:a', 'pcm_s16le', normalize_path_for_ffmpeg(norm_tf)]
                        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        try:
                            os.remove(tmp_out)
                        except Exception:
                            pass
                        temp_files.append(norm_tf)
                        clause_out = norm_tf
                    except Exception:
                        temp_files.append(tmp_out)
                        clause_out = tmp_out
                    clause_ok = True
                    if log_callback:
                        log_callback(f"[AquesTalk-clause] idx={index} clause={i+1} OK candidate={cand_name}")
                    break
                else:
                    last_err = emsg or "synth-failed"
                    if log_callback:
                        log_callback(f"[AquesTalk-clause] idx={index} clause={i+1} candidate={cand_name} failed: {last_err}")
                    await asyncio.sleep(0.18)

            if not clause_ok:
                for tf in temp_files:
                    try: os.remove(tf)
                    except Exception: pass
                if log_callback:
                    log_callback(f"[AquesTalk-clause] Failed to synth clause {i+1}/{len(clauses)} for idx={index}; last_err={last_err}")
                return False

            pause_after = DEFAULT_CLAUSE_PAUSE
            if delim and delim in pause_map:
                try:
                    pause_after = float(pause_map[delim])
                except Exception:
                    pause_after = DEFAULT_CLAUSE_PAUSE
            pause_after_list.append(pause_after)

        ffmpeg = get_ffmpeg_path()
        silence_cache = {}
        cmd = [ffmpeg, '-y', '-threads', _FFMPEG_THREADS_STR]
        input_count = 0
        for idx_cf, clause_file in enumerate(temp_files):
            cmd += ['-i', normalize_path_for_ffmpeg(clause_file)]
            input_count += 1
            if idx_cf < len(temp_files) - 1:
                pause_dur = pause_after_list[idx_cf] if idx_cf < len(pause_after_list) else DEFAULT_CLAUSE_PAUSE
                silence_path = silence_cache.get(pause_dur)
                if not silence_path:
                    silence_path = get_silence_wav_path(pause_dur, sample_rate=MIN_SR_ENFORCE)
                    silence_cache[pause_dur] = silence_path
                cmd += ['-i', normalize_path_for_ffmpeg(silence_path)]
                input_count += 1

        inputs_labels = "".join(f"[{i}:a]" for i in range(input_count))
        if _HAS_SOXR:
            resample_part = f"aresample=resampler=soxr:osr={MIN_SR_ENFORCE}:comp_duration=0"
        else:
            resample_part = f"aresample={MIN_SR_ENFORCE}:comp_duration=0"

        filter_complex = f"{inputs_labels}concat=n={input_count}:v=0:a=1[outa];[outa]{resample_part}[outa2]"
        cmd += ['-filter_complex', filter_complex, '-map', '[outa2]', '-c:a', 'pcm_s16le', normalize_path_for_ffmpeg(out_wav)]

        _dbg(f"[AquesTalk-clause] Running final concat+resample ffmpeg cmd (clauses={len(temp_files)}, inputs={input_count})", log_callback=log_callback)
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        for nf in temp_files:
            try:
                if nf.endswith(f"_norm_{MIN_SR_ENFORCE}.wav") and os.path.exists(nf):
                    os.remove(nf)
            except Exception:
                pass

        _dbg(f"[AquesTalk-clause] clause concat -> {out_wav} size={os.path.getsize(out_wav)} sr={get_audio_sample_rate(out_wav)}", log_callback=log_callback)
        return os.path.exists(out_wav) and os.path.getsize(out_wav) > 512

    except Exception as e:
        _dbg(f"[AquesTalk-clause] unexpected error: {e}", log_callback=log_callback)
        for tf in temp_files:
            try: os.remove(tf)
            except Exception: pass
        return False

# ----------------- New helpers for retry on 105 errors -------------------
def _generate_alternative_texts_for_105(prepped: str, yomi_raw: str, yomi_clean: str, log_callback=None):
    alts = []
    seen = set()
    def add(s):
        if not s:
            return
        if s in seen:
            return
        seen.add(s)
        alts.append(s)
    add(yomi_clean)
    add(prepped)
    try:
        k = hira_to_kata(prepped)
        k2 = re.sub(r'[^ァ-ヴー\u3000\s、。！？]', '', k)
        add(k2)
    except Exception:
        pass
    try:
        add(to_fullwidth_digits(prepped))
    except Exception:
        pass
    try:
        s = re.sub(r'[A-Za-z0-9\[\]\(\)<>@#\$%\^&\*\\\/~`_=\+\|\:;\"\'\-\–\—…]', '', prepped)
        s = re.sub(r'\s+', ' ', s).strip()
        add(s)
    except Exception:
        pass
    try:
        ycli = get_mecab_yomi_via_exe(prepped, base_dir=BASE_DIR, log_callback=log_callback, timeout=6)
        if ycli:
            ycli_k = sanitize_yomi_keep_katakana(ycli)
            add(ycli_k)
    except Exception:
        pass
    return alts

# ---------------- generate_tts_audio (AquesTalk enhanced) ------------
# (Full function included as in v24; unchanged behavior.)
async def generate_tts_audio(sentence, speaker_id, output_path, rate=1.0, voice_source="Voicevox",
                             max_retries=30, log_callback=None, index=None, config=None):
    def _record_failed_sentence(txt, voice_name, idx, emsg):
        try:
            fn = os.path.join(output_temp_dir, "errors_aquestalk_failed.txt")
            with open(fn, "a", encoding="utf-8") as f:
                f.write(f"idx={idx} voice={voice_name} err={emsg[:200]} text={txt}\n")
        except Exception:
            pass

    if not (voice_source and str(voice_source).lower().startswith("aques")):
        for attempt in range(1, max_retries + 1):
            if voice_source and str(voice_source).lower() == "edge-tts":
                success = await generate_edge_tts_audio(sentence, speaker_id, output_path, rate)
            else:
                success = await generate_voicevox_audio(sentence, speaker_id, output_path, rate)
            if success:
                if log_callback:
                    log_callback(f"câu {index} => VoiceVox/Edge synth OK")
                return True
            else:
                if log_callback:
                    log_callback(f"câu {index} => VoiceVox/Edge synth attempt {attempt}/{max_retries} => thất bại")
                await asyncio.sleep(1.5)
        if log_callback:
            log_callback(f"câu {index} => VoiceVox/Edge synth => FAILED after {max_retries}")
        return False

    if not _HAS_AQUESTALK:
        if log_callback:
            log_callback(f"[AquesTalk] helper not available for idx={index}")
        _log_sentence_result(index, sentence, None, None, None, None, str(speaker_id), "FAILED", "AquesTalk missing")
        return False

    voice_name = str(speaker_id or "f1")
    out_wav = output_path if output_path.lower().endswith(".wav") else output_path.rsplit(".", 1)[0] + ".wav"
    speed = max(30, min(400, int(rate * 100)))

    try:
        prepped = to_fullwidth_digits(sentence)
    except Exception:
        prepped = sentence

    # ----------------- New: apply normalization (aq_normalize) to prepped -----------------
    normalized_prepped = None
    try:
        if normalize_for_aquestalk:
            to_hira_flag = False
            if isinstance(config, dict) and config.get("aquestalk_force_hiragana", False):
                to_hira_flag = True
            try:
                normalized_prepped = normalize_for_aquestalk(prepped, to_hiragana=to_hira_flag)
            except Exception:
                normalized_prepped = None
            if normalized_prepped and len(normalized_prepped) >= 1:
                prepped = normalized_prepped
    except Exception:
        normalized_prepped = None
    # -------------------------------------------------------------------------

    force_clause = False
    try:
        if config and isinstance(config, dict) and config.get("force_clause", False):
            force_clause = True
        if os.environ.get("AQUESTALK_ALWAYS_CLAUSE", "0") == "1":
            force_clause = True
    except Exception:
        force_clause = False

    if force_clause:
        if log_callback:
            log_callback(f"[AquesTalk] force_clause requested for idx={index}; using clause-based synth")
        ok_clause = await synthesize_aquestalk_clauses(prepped, speaker_id, out_wav, speed, log_callback=log_callback, index=index)
        if ok_clause:
            return True

    yomi_raw = None
    try:
        yomi_raw = mecab_yomi(prepped, base_dir=BASE_DIR, log_callback=log_callback)
    except Exception:
        yomi_raw = None

    if yomi_raw and re.search(r'[\u4E00-\u9FFF]', yomi_raw):
        yomi_cli = get_mecab_yomi_via_exe(prepped, base_dir=BASE_DIR, log_callback=log_callback)
        if yomi_cli:
            if re.search(r'[ぁ-ゔァ-ヴー]', yomi_cli) and (len(yomi_cli) >= max(3, len(yomi_raw)//2)):
                if log_callback:
                    log_callback(f"[MeCab-fallback] using CLI -Oyomi result (len {len(yomi_cli)}) instead of helper output")
                yomi_raw = yomi_cli

    # ------------------ Build yomi_clean and normalize it (important) ------------------
    yomi_clean = None
    if yomi_raw:
        yomi_clean = sanitize_yomi_keep_katakana(yomi_raw)
        if not yomi_clean or len(yomi_clean) < 2:
            tmp = hira_to_kata(yomi_raw)
            tmp = re.sub(r'[^ァ-ヴー\u3000\s、。！？]', '', tmp)
            tmp = re.sub(r'\s+', ' ', tmp).strip()
            if tmp and len(tmp) >= 2:
                yomi_clean = tmp

    normalized_yomi = None
    try:
        if normalize_for_aquestalk and yomi_clean:
            try:
                normalized_yomi = normalize_for_aquestalk(yomi_clean, to_hiragana=False)
            except Exception:
                normalized_yomi = None
            if normalized_yomi and len(normalized_yomi) >= 1:
                yomi_clean = normalized_yomi
    except Exception:
        normalized_yomi = None
    # ------------------------------------------------------------------------------

    sanitized_original = sanitize_for_aquestalk_fallback(prepped)

    prefer_yomi_first = False
    if yomi_clean and len(yomi_clean) >= 4:
        prefer_yomi_first = True
    elif original_is_likely_problematic(prepped):
        prefer_yomi_first = True

    if os.environ.get("AQUESTALK_FORCE_ORIGINAL") == "1":
        prefer_yomi_first = False

    if prefer_yomi_first:
        base_attempt_texts = [t for t in ([yomi_clean, prepped, sanitized_original]) if t]
    else:
        base_attempt_texts = [t for t in ([prepped, yomi_clean, sanitized_original]) if t]

    # --- NEW: normalize all candidates before trying (extra safety) ---
    if normalize_for_aquestalk:
        norm_candidates = []
        seen = set()
        for t in base_attempt_texts:
            try:
                tn = normalize_for_aquestalk(t, to_hiragana=False) or t
            except Exception:
                tn = t
            if tn and tn not in seen:
                seen.add(tn)
                norm_candidates.append(tn)
            # include original unnormalized as fallback
            if t and t not in seen:
                seen.add(t)
                norm_candidates.append(t)
        base_attempt_texts = norm_candidates or base_attempt_texts
    # --------------------------------------------------------------------

    debug_input_fn = os.path.join(output_temp_dir, f"failed_input_idx{index}_voice{voice_name}.txt")
    try:
        with open(debug_input_fn, "w", encoding="utf-8") as df:
            df.write("original:\n" + (prepped or "") + "\n\n")
            df.write("normalized_prepped:\n" + (normalized_prepped or "") + "\n\n")
            df.write("yomi_raw:\n" + (yomi_raw or "") + "\n\n")
            df.write("yomi_clean:\n" + (yomi_clean or "") + "\n\n")
            df.write("normalized_yomi:\n" + (normalized_yomi or "") + "\n\n")
            df.write("sanitized_original:\n" + (sanitized_original or "") + "\n\n")
            df.write("attempt_order:\n")
            for t in base_attempt_texts:
                df.write("----\nlen=%d\n%s\n\n" % (len(t), t))
    except Exception:
        pass

    if log_callback:
        try:
            log_callback(f"câu {index} => MeCab: {'OK' if yomi_raw else 'None'}")
            log_callback(f"[AquesTalk] idx={index} voice={voice_name} prefer_yomi_first={prefer_yomi_first}")
            log_callback(f"[AquesTalk] original (len={len(prepped)}): {prepped}")
            log_callback(f"[AquesTalk] normalized_prepped (len={len(normalized_prepped) if normalized_prepped else 0}): {normalized_prepped or ''}")
            log_callback(f"[AquesTalk] yomi_raw (len={len(yomi_raw) if yomi_raw else 0}): {yomi_raw or ''}")
            log_callback(f"[AquesTalk] yomi_clean (len={len(yomi_clean) if yomi_clean else 0}): {yomi_clean or ''}")
            log_callback(f"[AquesTalk] normalized_yomi (len={len(normalized_yomi) if normalized_yomi else 0}): {normalized_yomi or ''}")
            log_callback(f"[AquesTalk] base attempt_order: {len(base_attempt_texts)} candidates")
        except Exception:
            pass

    # IMPORTANT: voice_candidates only includes original voice unless config allows otherwise
    voice_candidates = [voice_name]
    try:
        allow_voice_fallback = False
        if config and isinstance(config, dict):
            allow_voice_fallback = bool(config.get("aquestalk_try_other_voices", False))
        if allow_voice_fallback:
            if 'list_aquestalk_voices' in globals() and callable(list_aquestalk_voices):
                try:
                    allvoices = list_aquestalk_voices(try_short_test=False)
                    for v in allvoices:
                        if v not in voice_candidates:
                            voice_candidates.append(v)
                except Exception:
                    pass
    except Exception:
        pass

    tried_clause_fallback = False
    # Allow overriding number of retries per text via config; if aggressive requested we increase it.
    PER_TEXT_RETRIES = int(config.get("aquestalk_per_text_retries", 2)) if config and isinstance(config, dict) else 2
    aggressive_retry_enabled = bool(config.get("aquestalk_aggressive_retry", False)) if config and isinstance(config, dict) else False
    if aggressive_retry_enabled:
        PER_TEXT_RETRIES = max(PER_TEXT_RETRIES, 4)
    BACKOFF_BASE = float(config.get("aquestalk_backoff_base", 0.35)) if config and isinstance(config, dict) else 0.35

    # Track which aggressive alts we already injected to avoid duplication
    injected_aggressive = set()

    for voice_to_try in voice_candidates:
        voice_label = str(voice_to_try)
        attempt_texts = list(base_attempt_texts)
        if not attempt_texts:
            attempt_texts = [prepped]

        for idx_try, orig_text_try in enumerate(list(attempt_texts), start=1):
            text_try = orig_text_try
            for trial in range(1, PER_TEXT_RETRIES + 1):
                try:
                    if log_callback:
                        log_callback(f"[AquesTalk] Synth start: voice={voice_label} idx={index} attempt_order={idx_try}/{len(attempt_texts)} try#{trial} text_len={len(text_try)}")
                    await synthesize_aquestalk_to_file_async(text_try, out_wav, str(voice_to_try), speed)

                    try:
                        raw_out = out_wav.rsplit(".", 1)[0] + "_raw_aqt.wav"
                        try:
                            _sh = __import__('shutil')
                            _sh.copy(out_wav, raw_out)
                        except Exception:
                            pass
                        _dbg(f"[AquesTalk-debug] saved raw synth -> {raw_out}", log_callback=log_callback)
                    except Exception:
                        pass

                    if os.path.exists(out_wav):
                        size = os.path.getsize(out_wav)
                        duration = None
                        try:
                            duration = get_audio_duration(out_wav)
                        except Exception:
                            duration = None

                        if size > 512:
                            out_sr_raw = get_audio_sample_rate(out_wav)
                            _dbg(f"[AquesTalk] Synth produced {out_wav} size={size} sr={out_sr_raw}", log_callback=log_callback)
                            try:
                                tmp_res = out_wav.rsplit(".", 1)[0] + f"_resampled_{MIN_SR_ENFORCE}.wav"
                                ffmpeg = get_ffmpeg_path()
                                cmd = [ffmpeg, '-y', '-threads', _FFMPEG_THREADS_STR, '-i', normalize_path_for_ffmpeg(out_wav)]
                                cmd += build_audio_resample_args(MIN_SR_ENFORCE)
                                cmd += ['-c:a', 'pcm_s16le', normalize_path_for_ffmpeg(tmp_res)]
                                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                                if os.path.exists(tmp_res) and os.path.getsize(tmp_res) > 512:
                                    try:
                                        os.replace(tmp_res, out_wav)
                                    except Exception:
                                        try:
                                            os.remove(out_wav)
                                            os.rename(tmp_res, out_wav)
                                        except Exception:
                                            pass
                                    out_sr_after = get_audio_sample_rate(out_wav)
                                    _dbg(f"[AquesTalk] Re-encoded synth -> {out_wav} sr={out_sr_after} size={os.path.getsize(out_wav)}", log_callback=log_callback)
                            except Exception as ex:
                                _dbg(f"[AquesTalk] Re-encode after synth failed: {ex}", log_callback=log_callback)

                            expected = max(0.6, len(prepped) * 0.06)
                            if duration and duration < max(0.45, expected * 0.55):
                                if not tried_clause_fallback:
                                    tried_clause_fallback = True
                                    if log_callback:
                                        log_callback(f"[AquesTalk] Detected truncation (dur={duration:.2f}s < expected={expected:.2f}s). Trying clause-based fallback idx={index}")
                                    ok_clause = await synthesize_aquestalk_clauses(prepped, voice_to_try, out_wav, speed, log_callback=log_callback, index=index)
                                    if ok_clause:
                                        if log_callback:
                                            log_callback(f"câu {index} => OK (clause-concat fallback)")
                                        _log_sentence_result(index, sentence, prepped, yomi_raw, yomi_clean, prepped, str(voice_to_try), "OK", "clause-concat")
                                        return True
                                _record_failed_sentence(text_try, str(voice_to_try), index, "short_wav_truncated")
                                await asyncio.sleep(BACKOFF_BASE * trial)
                                continue

                            if log_callback:
                                msg = f"câu {index} => OK (wav size={size}"
                                if duration:
                                    msg += f", duration={duration:.2f}s"
                                msg += f", voice={voice_label})"
                                log_callback(msg)
                            _log_sentence_result(index, sentence, prepped, yomi_raw, yomi_clean, text_try, str(voice_to_try), "OK")
                            return True
                        else:
                            reason = f"output-too-small (size={size})"
                            _record_failed_sentence(text_try, str(voice_to_try), index, reason)
                            if log_callback:
                                log_callback(f"câu {index} => FAILED ({reason}); debug -> {debug_input_fn}")
                            _log_sentence_result(index, sentence, prepped, yomi_raw, yomi_clean, text_try, str(voice_to_try), "FAILED", reason)
                            await asyncio.sleep(BACKOFF_BASE * trial)
                            continue
                    else:
                        reason = "no-output-file"
                        _record_failed_sentence(text_try, str(voice_to_try), index, reason)
                        if log_callback:
                            log_callback(f"câu {index} => FAILED (no output); debug -> {debug_input_fn}")
                        _log_sentence_result(index, sentence, prepped, yomi_raw, yomi_clean, text_try, str(voice_to_try), "FAILED", reason)
                        await asyncio.sleep(BACKOFF_BASE * trial)
                        continue

                except Exception as ex:
                    emsg = str(ex) or ""
                    tb = traceback.format_exc()
                    if log_callback:
                        log_callback(f"[AquesTalk] Synth error for idx={index} attempt_order={idx_try} try#{trial} voice={voice_label}: {emsg[:400]}")
                    # If 105 / undefined reading, inject alternatives
                    if any(k in emsg for k in ("未定義", "読み記号", "105", "未定義の読み")):
                        _record_failed_sentence(text_try, str(voice_to_try), index, emsg)
                        if log_callback:
                            log_callback(f"[AquesTalk] Detected 105/undefined reading on idx={index}, generating alternative candidate texts")
                        # Standard alts
                        alts = _generate_alternative_texts_for_105(prepped, yomi_raw, yomi_clean, log_callback=log_callback)
                        # If aggressive configured, produce aggressive sanitized variants and prepend
                        if aggressive_retry_enabled:
                            ag_key = (index, voice_to_try)
                            if ag_key not in injected_aggressive:
                                injected_aggressive.add(ag_key)
                                ag_text = aggressive_sanitize(prepped)
                                if ag_text and ag_text not in alts:
                                    alts.insert(0, ag_text)
                                    if log_callback:
                                        log_callback(f"[AquesTalk] Injected aggressive sanitized variant (len={len(ag_text)}) for idx={index}")
                        # Insert alternatives right after current try position
                        for a in reversed(alts):  # reversed so first alt becomes next attempt
                            if a and a not in attempt_texts:
                                attempt_texts.insert(idx_try, a)
                        await asyncio.sleep(BACKOFF_BASE * trial)
                        continue
                    else:
                        _record_failed_sentence(text_try, str(voice_to_try), index, emsg)
                        try:
                            with open(os.path.join(output_temp_dir, "errors_aquestalk_failed_debug.txt"), "a", encoding="utf-8") as f:
                                f.write("----\n")
                                f.write(f"idx={index} attempt_order={idx_try} try#{trial} voice={str(voice_to_try)}\n")
                                f.write("exception:\n")
                                f.write(tb + "\n")
                                f.write("text_tried:\n")
                                f.write(text_try + "\n\n")
                        except Exception:
                            pass
                        break

    # Final clause-based attempt if not tried
    if not tried_clause_fallback:
        if log_callback:
            log_callback(f"[AquesTalk] Final clause-based attempt for idx={index}")
        ok_clause = await synthesize_aquestalk_clauses(prepped, voice_name, out_wav, speed, log_callback=log_callback, index=index)
        if ok_clause:
            return True

    if log_callback:
        log_callback(f"[AquesTalk] All attempts failed for idx={index}; debug input file: {debug_input_fn}")
    return False

# -------------------------
# VoiceVox / Edge helpers (unchanged)
# -------------------------
async def generate_voicevox_audio(sentence, speaker_id, output_path, rate=1.0):
    VOICEVOX_API_BASE = "http://127.0.0.1:50021"
    try:
        query_response = await asyncio.to_thread(lambda: requests.post(
            f"{VOICEVOX_API_BASE}/audio_query",
            params={"text": sentence, "speaker": speaker_id},
            timeout=30
        ))
        query_response.raise_for_status()
        audio_query = query_response.json()
        audio_query["speedScale"] = rate
        if not output_path.lower().endswith(".wav"):
            output_path = output_path.rsplit(".", 1)[0] + ".wav"
        audio_response = await asyncio.to_thread(lambda: requests.post(
            f"{VOICEVOX_API_BASE}/synthesis",
            params={"speaker": speaker_id},
            json=audio_query,
            timeout=60
        ))
        audio_response.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(audio_response.content)
        return True
    except Exception as e:
        print("VoiceVox error:", e)
        return False

async def generate_edge_tts_audio(sentence, speaker_id, output_path, rate=1.0):
    try:
        import edge_tts
        temp_mp3 = output_path.rsplit(".", 1)[0] + "_tmp.mp3"
        percent = int(round((rate - 1) * 100))
        rate_str = f"+{percent}%" if percent >= 0 else f"{percent}%"
        communicate = edge_tts.Communicate(text=sentence, voice=speaker_id, rate=rate_str)
        await communicate.save(temp_mp3)
        ffmpeg_path = get_ffmpeg_path()
        output_wav = output_path.rsplit(".", 1)[0] + ".wav"
        subprocess.run([ffmpeg_path, "-y", "-threads", _FFMPEG_THREADS_STR, "-i", temp_mp3, output_wav], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.path.exists(output_wav) and os.path.getsize(output_wav) > 1024:
            os.remove(temp_mp3)
            return True
        return False
    except Exception as e:
        print("Edge-TTS error:", e)
        return False

# -------------------------
# Rendering pipeline + wrapper (full)
# -------------------------
def split_sentences(text):
    return [s.strip() for s in re.split(r'[\u3002\uFF0E.!?\n]', text) if s.strip()]

def wrap_text(draw, text, font, max_width):
    lines = []
    line = ''
    for ch in text:
        test_line = line + ch
        try:
            length = draw.textlength(test_line, font=font)
        except Exception:
            length = font.getsize(test_line)[0]
        if length <= max_width:
            line = test_line
        else:
            if ch in "。.!?":
                line = test_line
                continue
            if line:
                lines.append(line)
            line = ch
    if line:
        lines.append(line)
    return lines

async def render_sentence(
    index, sentence, voice, img_or_video, font, draw, ffmpeg_path,
    font_path, subtitle_color, stroke_color, bg_color, effect, encoder,
    volume_factor, bg_opacity, voice_speed, stroke_width, sem,
    video_speed=1.0, is_video_input=False, voice_source="Voicevox",
    effects_dir=None, overlay_effect="none", pause_duration=0.7, video_effect="none",
    progress_queue=None, log_callback=None, config=None
):
    async with sem:
        sentence = sentence.lstrip('\ufeff\u200b').strip()
        audio_path = os.path.join(output_temp_dir, f"line_{index}.wav")
        if log_callback:
            try: log_callback(f"[Render] idx={index} start TTS")
            except Exception: pass
        _dbg(f"[Render] idx={index} calling generate_tts_audio -> {audio_path}", log_callback=log_callback)
        success = await generate_tts_audio(sentence, voice, audio_path, voice_speed, voice_source=voice_source, max_retries=6, log_callback=log_callback, index=index, config=config)
        if not success or not os.path.exists(audio_path):
            if log_callback:
                try: log_callback(f"Xử lý câu {index} => thất bại (audio error)")
                except Exception: pass
            return None

        padded_audio_path = await concat_audio_with_silence(audio_path, pause_duration, log_callback=log_callback)

        sr = get_audio_sample_rate(padded_audio_path) or MIN_SR_ENFORCE
        if sr < MIN_SR_ENFORCE:
            sr = MIN_SR_ENFORCE
        _dbg(f"[Render] idx={index} padded audio: {padded_audio_path} sr={sr} size={os.path.getsize(padded_audio_path)}", log_callback=log_callback)

        if _HAS_SOXR:
            aresample_filter = f"aresample=resampler=soxr:osr={int(sr)}:comp_duration=0"
        else:
            aresample_filter = f"aresample={int(sr)}:comp_duration=0"

        audio_opts = ['-ac', '1', '-c:a', 'aac', '-b:a', '128k']

        duration = get_audio_duration(padded_audio_path)
        subtitle_full_width = config.get("subtitle_full_width", False) if config else False
        effect_key = (effect or "").lower().replace(" ", "")
        vf_parts = []
        num_frames = max(1, int(duration * 25))
        if not is_video_input:
            vf_parts.append("scale=1280:720:force_original_aspect_ratio=increase")
            vf_parts.append("crop=1280:720")
            if effect_key == "zoom":
                vf_parts.append(f"zoompan=z='min(zoom+0.0007,1.3)':d={num_frames}:s=1280x720")
            elif effect_key == "pan":
                vf_parts.append(f"zoompan=z=1.0:x='if(eq(n,0),0,x+1)':y='if(eq(n,0),0,y+1)':d={num_frames}:s=1280x720")
            elif effect_key == "zoom+pan":
                vf_parts.append(f"zoompan=z='min(zoom+0.0007,1.3)':x='if(eq(n,0),iw/2,x+(iw-iw/zoom)/{num_frames}/4)':y='if(eq(n,0),ih/2,y+(ih-ih/zoom)/{num_frames}/4)':d={num_frames}:s=1280x720")
            vf_parts.append("pad=1280:720:(ow-iw)/2:(oh-ih)/2")
            vf_chain = ",".join(vf_parts)
        else:
            vf_chain = "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720"

        wrapped = wrap_text(draw, sentence, font, max_width=1100)
        if not wrapped:
            wrapped = [""]
        try:
            line_heights = [draw.textbbox((0,0), l, font=font)[3] for l in wrapped]
        except Exception:
            line_heights = [font.getsize(l)[1] for l in wrapped]
        total_height = sum(line_heights) + (len(wrapped)-1)*10
        try:
            max_line_width = max(draw.textlength(line, font=font) for line in wrapped)
        except Exception:
            max_line_width = max(font.getsize(line)[0] for line in wrapped)
        sub_image_width = max(int(max_line_width) + 80, 200)
        sub_image_height = max(total_height + 40, 80)
        img_sub = Image.new("RGBA", (sub_image_width, sub_image_height), (0,0,0,0))
        draw_sub = ImageDraw.Draw(img_sub)
        try:
            bg_rgb = Image.new("RGB", (1,1), bg_color).getpixel((0,0))
        except Exception:
            bg_rgb = (0,0,0)
        draw_sub.rectangle([(0,0), img_sub.size], fill=(*bg_rgb, int(bg_opacity)))
        y = 20
        for line in wrapped:
            try:
                x = int((img_sub.size[0] - draw.textlength(line, font=font)) // 2)
            except Exception:
                x = int((img_sub.size[0] - font.getsize(line)[0]) // 2)
            draw_sub.text((x,y), line, font=font, fill=subtitle_color, stroke_width=stroke_width, stroke_fill=stroke_color)
            try:
                y += draw.textbbox((0,0), line, font=font)[3] + 10
            except Exception:
                y += font.getsize(line)[1] + 10
        sub_path = os.path.join(output_temp_dir, f"subtitle_{index}.png")
        img_sub.save(sub_path)

        temp_out = os.path.join(output_temp_dir, f"temp_{index}.mp4")
        encoder_preset_option = ["-preset", "fast"]
        encoder_choice = detect_best_encoder()
        if encoder_choice in ["h264_nvenc", "h264_amf", "h264_qsv"]:
            encoder_preset_option = []
        # set threads cap for ffmpeg
        ff_threads_arg = ['-threads', _FFMPEG_THREADS_STR]

        if is_video_input:
            norm_video_path = normalize_path_for_ffmpeg(img_or_video)
            norm_sub_path = normalize_path_for_ffmpeg(sub_path)
            norm_audio = normalize_path_for_ffmpeg(padded_audio_path)
            overlay_y = "(main_h-overlay_h)" if subtitle_full_width else "(main_h-overlay_h)-30"

            filter_complex = (
                f"[0:v]{vf_chain}[vbg];"
                f"[vbg][2:v]overlay=(main_w-overlay_w)/2:{overlay_y}:enable='between(t,0,{duration:.2f})'[v];"
                f"[1:a]volume={volume_factor},{aresample_filter}[outa]"
            )
            cmd = [get_ffmpeg_path(), '-y'] + ff_threads_arg + ['-i', norm_video_path, '-i', norm_audio, '-i', norm_sub_path,
                   '-filter_complex', filter_complex, '-map', '[v]', '-map', '[outa]', '-c:v', encoder_choice, '-r', '25'] + encoder_preset_option + audio_opts + ['-shortest', temp_out]
        else:
            norm_img_path = normalize_path_for_ffmpeg(img_or_video)
            norm_sub_path = normalize_path_for_ffmpeg(sub_path)
            norm_audio = normalize_path_for_ffmpeg(padded_audio_path)
            overlay_y = "(main_h-overlay_h)" if subtitle_full_width else "(main_h-overlay_h)-30"

            filter_complex = (
                f"[0:v]{vf_chain}[v_bg];"
                f"[v_bg][2:v]overlay=(main_w-overlay_w)/2:{overlay_y}:enable='between(t,0,{duration:.2f})'[v];"
                f"[1:a]volume={volume_factor},{aresample_filter}[outa]"
            )
            cmd = [get_ffmpeg_path(), '-y'] + ff_threads_arg + ['-loop', '1', '-i', norm_img_path, '-i', norm_audio, '-i', norm_sub_path,
                   '-filter_complex', filter_complex, '-map', '[v]', '-map', '[outa]', '-c:v', encoder_choice, '-r', '25'] + encoder_preset_option + audio_opts + ['-shortest', temp_out]

        _dbg(f"[Render] idx={index} ffmpeg cmd length {len(cmd)} encoder={encoder_choice} -threads={_FFMPEG_THREADS_STR}", log_callback=log_callback)
        ok = await asyncio.get_event_loop().run_in_executor(executor, lambda: run_ffmpeg_with_fallback(cmd, encoder_gpu=encoder_choice, fallback_encoder="libx264", si=si, log_callback=log_callback))
        if ok and os.path.exists(temp_out) and os.path.getsize(temp_out) > 1024:
            if log_callback:
                try: log_callback(f"[Render] idx={index} ffmpeg OK")
                except Exception: pass

            try:
                extracted = temp_out.rsplit(".", 1)[0] + "_extracted.wav"
                ffmpeg_path = get_ffmpeg_path()
                _dbg(f"[Debug-Extract] extracting audio from {temp_out} -> {extracted}", log_callback=log_callback)
                try:
                    cmd = [ffmpeg_path, '-y', '-threads', _FFMPEG_THREADS_STR, '-i', normalize_path_for_ffmpeg(temp_out), '-vn']
                    cmd += ['-ar', str(int(sr)), '-ac', '1', '-acodec', 'pcm_s16le', normalize_path_for_ffmpeg(extracted)]
                    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    padded_sr = get_audio_sample_rate(padded_audio_path)
                    padded_dur = get_audio_duration(padded_audio_path)
                    padded_size = os.path.getsize(padded_audio_path) if os.path.exists(padded_audio_path) else 0
                    padded_md5 = compute_md5(padded_audio_path)

                    extracted_sr = get_audio_sample_rate(extracted)
                    extracted_dur = get_audio_duration(extracted)
                    extracted_size = os.path.getsize(extracted) if os.path.exists(extracted) else 0
                    extracted_md5 = compute_md5(extracted)

                    match = (padded_md5 is not None and extracted_md5 is not None and padded_md5 == extracted_md5)

                    _dbg(f"[Debug-Extract] padded: path={padded_audio_path} sr={padded_sr} dur={padded_dur:.3f} size={padded_size} md5={padded_md5}", log_callback=log_callback)
                    _dbg(f"[Debug-Extract] extracted: path={extracted} sr={extracted_sr} dur={extracted_dur:.3f} size={extracted_size} md5={extracted_md5}", log_callback=log_callback)
                    _dbg(f"[Debug-Extract] md5_match={match}", log_callback=log_callback)

                    if not match:
                        _dbg("[Debug-Extract] WARNING: padded WAV and extracted WAV differ (MD5 mismatch). This indicates audio was modified during encoding to AAC / muxing.", log_callback=log_callback)
                    else:
                        _dbg("[Debug-Extract] OK: padded WAV and extracted WAV match", log_callback=log_callback)
                except subprocess.CalledProcessError as ex:
                    _dbg(f"[Debug-Extract] ffmpeg extract failed: {ex}", log_callback=log_callback)
                except Exception as e:
                    _dbg(f"[Debug-Extract] extract unexpected error: {e}", log_callback=log_callback)
            except Exception as e:
                _dbg(f"[Debug-Extract] outer extract error: {e}", log_callback=log_callback)

            return temp_out
        else:
            if log_callback:
                try: log_callback(f"Xử lý câu {index} => thất bại (FFmpeg error)")
                except Exception: pass
            return None

# High-level wrapper used by GUI/main app
async def render_sentence_dialogue(index, sentence, config, image_paths, output_path, add_log=None):
    if add_log is None:
        def _p(s):
            print(s)
        add_log = _p

    if not image_paths:
        raise ValueError("image_paths must not be empty")

    bg_path = image_paths[index % len(image_paths)]
    bg_lower = os.path.splitext(bg_path)[1].lower()
    is_video_input = bg_lower in ['.mp4', '.mov', '.avi', '.mkv', '.webm']
    voice_source   = config.get("voice_source", "Voicevox")
    speaker_id     = config.get("speaker_id")
    voice_speed    = float(config.get("voice_speed", 1.0))
    font_path      = config.get("font_path")
    try:
        font_size_cfg = int(config.get("font_size", 48)) if config else 48
    except Exception:
        font_size_cfg = 48
    volume_percent = int(config.get("volume", 100))
    bg_opacity     = int(config.get("bg_opacity", 200))
    subtitle_color = config.get("subtitle_color", "#FFFF00")
    stroke_color   = config.get("stroke_color", "#000000")
    stroke_width   = int(config.get("stroke_size", 2))
    bg_color       = config.get("bg_color", "#000000")
    video_effect   = config.get("video_effect", "none")
    pause_sec      = float(config.get("pause_sec", 0.7))
    effect         = config.get("effect", "zoom")
    encoder        = detect_best_encoder()
    try:
        font = ImageFont.truetype(font_path, font_size_cfg)
    except Exception:
        try:
            font = ImageFont.truetype(font_path, 48)
        except Exception:
            font = ImageFont.load_default()
    draw = ImageDraw.Draw(Image.new('RGBA', (10, 10)))
    sem = asyncio.Semaphore(1)

    tmp_path = await render_sentence(
        index=index,
        sentence=sentence,
        voice=speaker_id,
        img_or_video=bg_path,
        font=font,
        draw=draw,
        ffmpeg_path=get_ffmpeg_path(),
        font_path=font_path,
        subtitle_color=subtitle_color,
        stroke_color=stroke_color,
        bg_color=bg_color,
        effect=effect,
        encoder=encoder,
        volume_factor=volume_percent / 100.0,
        bg_opacity=bg_opacity,
        voice_speed=voice_speed,
        stroke_width=stroke_width,
        sem=sem,
        video_speed=1.0,
        is_video_input=is_video_input,
        voice_source=voice_source,
        effects_dir=EFFECTS_DIR,
        overlay_effect="none",
        video_effect=video_effect,
        pause_duration=pause_sec,
        progress_queue=None,
        log_callback=add_log,
        config=config
    )

    if tmp_path is None or not os.path.exists(tmp_path):
        raise RuntimeError(f"Failed to render sentence {index}: no output returned from engine")

    icon_a_dir = config.get("icon_a_dir")
    icon_b_dir = config.get("icon_b_dir")
    speak_role = config.get("speak_role", "A")

    if icon_a_dir and icon_b_dir:
        temp_with_icon = output_path + "_withicon.mp4"
        duration = get_audio_duration(os.path.join(output_temp_dir, f"line_{index}.wav"))
        try:
            overlay_icon_ab(
                input_video_path=tmp_path,
                speak_role=speak_role,
                output_path=temp_with_icon,
                icon_a_dir=icon_a_dir,
                icon_b_dir=icon_b_dir,
                icon_pos_a=(30, 0),
                icon_pos_b=(30, 0),
                icon_size=(240, 240),
                subtitle_height=120,
                video_height=720,
                padding=20,
                duration=duration,
                log_callback=add_log
            )
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            if os.path.abspath(temp_with_icon) != os.path.abspath(output_path):
                _shutil.move(temp_with_icon, output_path)
        except Exception as ex:
            tb = traceback.format_exc()
            try:
                add_log(f"[Render] idx={index} overlay skipped/failed: {ex}")
                add_log(tb)
            except Exception:
                print(f"[Render] idx={index} overlay skipped/failed: {ex}")
                print(tb)
            try:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                if os.path.abspath(tmp_path) != os.path.abspath(output_path):
                    _shutil.move(tmp_path, output_path)
            except Exception as e2:
                try:
                    add_log(f"[Render] idx={index} fallback move failed: {e2}")
                except Exception:
                    print(f"[Render] idx={index} fallback move failed: {e2}")
    else:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        if os.path.abspath(tmp_path) != os.path.abspath(output_path):
            _shutil.move(tmp_path, output_path)

    return output_path

# End of file