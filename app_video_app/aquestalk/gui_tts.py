#!/usr/bin/env python3
# gui_tts.py
# Simple Tkinter GUI for AquesTalk-python with improved MeCab fallback and debug logging.
#
# Requirements (recommended):
#   pip install jaconv simpleaudio
# Optional MeCab:
#   pip install mecab-python3  (or have mecab.exe installed and on PATH)
#
# Save this file beside your "aquestalk" package and run with the same Python
# you use for the rest of the repo.

import sys
import io
import os
import wave
import tempfile
import threading
import subprocess
import shutil
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import jaconv
except Exception:
    jaconv = None

# Prefer mecab-python3 if installed
try:
    import MeCab
    _HAS_MECAB = True
except Exception:
    MeCab = None
    _HAS_MECAB = False

try:
    import simpleaudio as sa
    _HAS_SIMPLEAUDIO = True
except Exception:
    _HAS_SIMPLEAUDIO = False

# import your aquestalk wrapper (assumes package dir "aquestalk" in repo)
try:
    import aquestalk
    from aquestalk.aquestalk import AquesTalkError
except Exception:
    aquestalk = None
    AquesTalkError = Exception

import re
_ALLOWED_RE = re.compile(r'[^\u3040-\u309F\u30A0-\u30FF\u3001\u3002\uFF1F\uFF01\u300C\u300D\u30FB\u3000\uFF0C\uFF08\uFF09\u300E\u300F\u30FC\s]')

def mecab_to_hiragana(text):
    # Use mecab-python3 if available
    if _HAS_MECAB and MeCab is not None:
        tagger = MeCab.Tagger()
        node = tagger.parseToNode(text)
        parts = []
        while node:
            if node.surface:
                feature = node.feature or ''
                cols = feature.split(',')
                pron = None
                if len(cols) > 7 and cols[7] and cols[7] != '*':
                    pron = cols[7]
                elif len(cols) > 6 and cols[6] and cols[6] != '*':
                    pron = cols[6]
                else:
                    pron = node.surface
                parts.append(pron)
            node = node.next
        katakana = ''.join(parts)
        if jaconv:
            return jaconv.kata2hira(katakana)
        else:
            return katakana
    # Fallback: try calling mecab executable (works on Windows if mecab.exe installed)
    if shutil.which('mecab'):
        return mecab_reading_via_subprocess_utf8(text, mecab_path='mecab')
    # No mecab available: return input (user must enter kana)
    return text

def mecab_reading_via_subprocess_utf8(text, mecab_path='mecab'):
    # Call mecab via subprocess, pass UTF-8 input bytes, decode stdout as UTF-8
    input_bytes = text.encode('utf-8')
    proc = subprocess.Popen([mecab_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout_bytes, stderr_bytes = proc.communicate(input_bytes)
    if proc.returncode != 0:
        try:
            stderr_text = stderr_bytes.decode('utf-8', errors='replace')
        except Exception:
            stderr_text = repr(stderr_bytes)
        raise RuntimeError(f"mecab failed (returncode={proc.returncode}): {stderr_text.strip()}")
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
    if jaconv:
        return jaconv.kata2hira(katakana)
    else:
        return katakana

def sanitize_for_aquestalk(text):
    # Convert ascii hyphen to prolonged mark, ascii punctuation to Japanese punctuation,
    # convert digits to fullwidth, remove parentheses, remove unsupported chars, convert katakana->hiragana.
    if text is None:
        return ''
    text = text.replace('-', 'ー')
    text = text.replace(',', '、').replace('?', '？').replace('!', '！').replace('.', '。')
    if jaconv:
        text = jaconv.h2z(text, digit=True, ascii=False)
    text = re.sub(r'[\(\)（）\[\]［］]', '', text)
    cleaned = _ALLOWED_RE.sub('', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if jaconv:
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

def play_raw_wav_bytes(raw_bytes):
    try:
        with wave.open(io.BytesIO(raw_bytes), 'rb') as wf:
            frames = wf.readframes(wf.getnframes())
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
    except Exception as e:
        raise RuntimeError("Invalid WAV bytes: " + str(e))

    if _HAS_SIMPLEAUDIO:
        try:
            play_obj = sa.play_buffer(frames, nchannels, sampwidth, framerate)
            play_obj.wait_done()
            return
        except Exception:
            pass

    fd, tmp = tempfile.mkstemp(suffix='.wav')
    os.close(fd)
    try:
        save_raw_wav_bytes(raw_bytes, tmp)
        if sys.platform.startswith('win'):
            os.startfile(tmp)
        elif sys.platform == 'darwin':
            subprocess.call(['afplay', tmp])
        else:
            if shutil.which('paplay'):
                subprocess.call(['paplay', tmp])
            elif shutil.which('aplay'):
                subprocess.call(['aplay', tmp])
            else:
                subprocess.call(['xdg-open', tmp])
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass

class AquesTalkGUI:
    def __init__(self, root):
        self.root = root
        root.title("AquesTalk TTS - GUI")
        root.geometry("700x420")

        main = ttk.Frame(root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="Input text:").pack(anchor='w')
        self.text = tk.Text(main, height=10, wrap='word')
        self.text.pack(fill=tk.BOTH, expand=False)
        sample = "こんにちは、これはテストです。"
        self.text.insert('1.0', sample)

        ctrl = ttk.Frame(main)
        ctrl.pack(fill=tk.X, pady=6)

        ttk.Label(ctrl, text="Voice:").grid(row=0, column=0, sticky='w')
        self.voice_var = tk.StringVar()
        self.voice_combo = ttk.Combobox(ctrl, textvariable=self.voice_var, state='readonly', width=10)
        self.voice_combo.grid(row=0, column=1, sticky='w', padx=6)

        self.refresh_btn = ttk.Button(ctrl, text="Refresh voices", command=self.refresh_voices)
        self.refresh_btn.grid(row=0, column=2, padx=6)

        ttk.Label(ctrl, text="Speed:").grid(row=0, column=3, sticky='w', padx=(12,0))
        self.speed_var = tk.IntVar(value=100)
        self.speed_scale = ttk.Scale(ctrl, from_=50, to=200, orient='horizontal', variable=self.speed_var)
        self.speed_scale.grid(row=0, column=4, sticky='we', padx=6)
        ctrl.columnconfigure(4, weight=1)
        self.speed_label = ttk.Label(ctrl, textvariable=self.speed_var, width=4)
        self.speed_label.grid(row=0, column=5, sticky='e')

        buttons = ttk.Frame(main)
        buttons.pack(fill=tk.X, pady=6)
        self.play_btn = ttk.Button(buttons, text="Play", command=self.on_play)
        self.play_btn.pack(side=tk.LEFT, padx=6)
        self.save_btn = ttk.Button(buttons, text="Save...", command=self.on_save)
        self.save_btn.pack(side=tk.LEFT)
        self.status = ttk.Label(main, text="Ready", anchor='w')
        self.status.pack(fill=tk.X, pady=(8,0))

        self.refresh_voices()

    def set_status(self, msg):
        self.status.config(text=msg)
        self.root.update_idletasks()

    def refresh_voices(self):
        voices = []
        if aquestalk is None:
            messagebox.showerror("Error", "Cannot import aquestalk. Make sure the package is on PYTHONPATH.")
            self.voice_combo['values'] = []
            return
        for v in ['f1','f2','f3','f4','f5','f6','f7','f8']:
            try:
                _ = aquestalk.load(v)
                voices.append(v)
            except Exception:
                continue
        if not voices:
            voices = ['f1','f2','f3','f4','f5','f6','f7','f8']
        self.voice_combo['values'] = voices
        self.voice_combo.set(voices[0])

    def synthesize(self, text, voice, speed):
        if aquestalk is None:
            raise RuntimeError("aquestalk package not available")
        try:
            kana = mecab_to_hiragana(text)
        except Exception as e:
            # If mecab fails, fallback to original text but log the error
            kana = text
            print("mecab conversion failed:", str(e))

        sanitized = sanitize_for_aquestalk(kana)
        # Debug: print/log the kana and sanitized strings so we can see what AquesTalk receives
        print("=== Debug synth ===")
        print("raw input:", repr(text))
        print("kana:", repr(kana))
        print("sanitized:", repr(sanitized))
        self.set_status("Sanitized length: " + str(len(sanitized)))
        if not sanitized:
            raise RuntimeError("Sanitized text empty or invalid for AquesTalk. Try providing kana or install MeCab.")
        aq = aquestalk.load(voice)
        try:
            raw = aq.synthe_raw(sanitized, speed=int(speed))
        except TypeError:
            raw = aq.synthe_raw(sanitized)
        return raw

    def _synthesize_and_play(self, text, voice, speed):
        try:
            self.set_status("Synthesizing...")
            raw = self.synthesize(text, voice, speed)
            self.set_status("Playing...")
            play_raw_wav_bytes(raw)
            self.set_status("Ready")
        except AquesTalkError as ae:
            # Show sanitized string in the messagebox to help debugging
            try:
                msg = f"AquesTalkError: {ae}\n\nSanitized input (repr) shown in console."
            except Exception:
                msg = str(ae)
            self.set_status("AquesTalkError")
            traceback.print_exc()
            messagebox.showerror("AquesTalkError", msg)
        except Exception as e:
            self.set_status("Error: " + str(e))
            traceback.print_exc()
            messagebox.showerror("Error", str(e))

    def on_play(self):
        text = self.text.get('1.0', 'end').strip()
        if not text:
            messagebox.showwarning("No text", "Please enter text to synthesize.")
            return
        voice = self.voice_var.get() or 'f1'
        speed = self.speed_var.get()
        t = threading.Thread(target=self._synthesize_and_play, args=(text, voice, speed), daemon=True)
        t.start()

    def on_save(self):
        text = self.text.get('1.0', 'end').strip()
        if not text:
            messagebox.showwarning("No text", "Please enter text to synthesize.")
            return
        voice = self.voice_var.get() or 'f1'
        speed = self.speed_var.get()
        out = filedialog.asksaveasfilename(defaultextension=".wav", filetypes=[("WAV files","*.wav")])
        if not out:
            return

        def synth_and_write():
            try:
                self.set_status("Synthesizing...")
                raw = self.synthesize(text, voice, speed)
                self.set_status("Saving...")
                save_raw_wav_bytes(raw, out)
                self.set_status("Saved: " + out)
                messagebox.showinfo("Saved", "Saved WAV: " + out)
            except Exception as e:
                self.set_status("Error: " + str(e))
                traceback.print_exc()
                messagebox.showerror("Error", str(e))

        t = threading.Thread(target=synth_and_write, daemon=True)
        t.start()

def main():
    root = tk.Tk()
    app = AquesTalkGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()