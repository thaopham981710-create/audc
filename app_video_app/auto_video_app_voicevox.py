#!/usr/bin/env python3
# auto_video_app_voicevox.py
# Main GUI application for Auto Video App
# Full file (UI preserved) with CPU/GPU thread optimization integration.
# - Reads AUTO_VIDEO_MAX_THREADS and AUTO_VIDEO_PREFER_GPU env vars (defaults: 24, 1)
# - Uses _MAX_THREADS to limit parallel rendering tasks in GUI
# - Keeps LogManager and UI behavior from previous v19/v20 code
# - When spawning render tasks, passes max_workers based on AUTO_VIDEO_MAX_THREADS

import os
import sys
import csv
import asyncio
import threading
import tempfile
import subprocess
import re
import json
import time
from datetime import datetime
from pathlib import Path

# Read environment tuning values (consistent with video_worker.py)
AUTO_VIDEO_MAX_THREADS = max(1, int(os.environ.get("AUTO_VIDEO_MAX_THREADS", "24")))
AUTO_VIDEO_PREFER_GPU = os.environ.get("AUTO_VIDEO_PREFER_GPU", "1") == "1"

if sys.platform == "win32":
    SI = subprocess.STARTUPINFO()
    try:
        SI.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        SI.wShowWindow = subprocess.SW_HIDE
        CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
    except Exception:
        CREATE_NO_WINDOW = 0
else:
    SI = None
    CREATE_NO_WINDOW = 0

import tkinter as tk
import tkinter.simpledialog as simpledialog
from tkinter import ttk, filedialog, colorchooser, messagebox
from activation_manager import activate_key, check_key_status
import requests

from video_worker import render_sentence_dialogue, normalize_path_for_ffmpeg

APP_TITLE = "Auto Video Hội Thoại 2 Nhân Vật (A/B - Code by Vũ Đức)"

def _icons_root():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return Path(os.path.join(base_dir, "icons"))

def _list_icon_chars(side_dir: Path):
    if not side_dir.exists():
        return []
    return sorted([p.name for p in side_dir.iterdir() if p.is_dir()])

def _resolve_icon_char(side_root: Path, char_name: str, default_align: str):
    if not char_name:
        return None
    d = side_root / char_name
    meta = {
        "align": default_align,
        "offset": [40, 30] if default_align == "left" else [-40, 30],
        "scale_height_ratio": 0.25,
        "mouth_cycle": ["talk_0","talk_1"],
        "blink_cycle": ["blink_0","blink_1"]
    }
    mp = d / "meta.json"
    try:
        if mp.exists():
            meta.update(json.loads(mp.read_text(encoding="utf-8")))
    except Exception:
        pass

    def _exists(p):
        return str(p) if p.exists() else None

    base  = _exists(d / "base.png")
    talks = [p for p in [d / "talk_0.png", d / "talk_1.png"] if p.exists()]
    blinks= [p for p in [d / "blink_0.png", d / "blink_1.png"] if p.exists()]
    return {"base": base, "talk": [str(x) for x in talks], "blink": [str(x) for x in blinks], "meta": meta}

KEY_FILE = os.path.expanduser("~/.auto_video_app_activation.key")

def prompt_for_key(root):
    for _ in range(3):
        key = simpledialog.askstring("Kích hoạt", "Nhập mã kích hoạt ứng dụng:", parent=root)
        if not key:
            messagebox.showerror("Lỗi", "Bạn phải nhập mã kích hoạt để sử dụng ứng dụng.")
            return False
        name = simpledialog.askstring("Thiết bị", "Nhập tên thiết bị (tùy chọn):", parent=root)
        try:
            result = activate_key(key, device_name=name or "")
        except Exception as e:
            result = {"status": "fail", "message": f"Lỗi kết nối: {e}"}
        if result.get("status") == "ok":
            try:
                with open(KEY_FILE, "w", encoding="utf-8") as f:
                    f.write(key)
            except Exception:
                pass
            messagebox.showinfo("Thành công", "Kích hoạt thành công!\nHạn: %s" % result.get("expire_time", "Không rõ"))
            return True
        else:
            messagebox.showerror("Lỗi kích hoạt", result.get("message", "Không xác định."))
    return False

def check_activation(root):
    saved_key = ""
    if os.path.exists(KEY_FILE):
        try:
            with open(KEY_FILE, "r", encoding="utf-8") as f:
                saved_key = f.read().strip()
        except Exception:
            saved_key = ""
    if saved_key:
        try:
            result = check_key_status(saved_key)
        except Exception as e:
            result = {"status": "fail", "message": f"Lỗi kết nối: {e}"}
        if result.get("status") == "ok":
            return True
        try:
            os.remove(KEY_FILE)
        except Exception:
            pass
    return prompt_for_key(root)

class PaletteClassic:
    BG_APP   = "#f2f4f7"
    FRAME_BG = "#d6d2c9"
    BORDER   = "#b8b3a9"
    TEXT_MUTE= "#6b6b6b"
    BTN_PRIMARY = "#2e7d32"
    BTN_PRIMARY_ACTIVE = "#226a27"
    BTN_STEEL   = "#4e6674"
    BTN_STEEL_ACTIVE = "#415764"

# ----------------- LogManager (compact summarizer) ---------------------
class LogManager:
    _AQT_SYNTH_ERROR_RE = re.compile(r'\[AquesTalk\] Synth error for idx=(\d+).*?(105|未定義|読み記号|未定義の読み)', re.IGNORECASE)
    _ALL_ATTEMPTS_FAILED_RE = re.compile(r'All attempts failed for idx=(\d+); debug input file:\s*(\S+)', re.IGNORECASE)
    _SENTENCE_RESULT_VN_OK = re.compile(r'câu\s*(\d+)\s*=>\s*thành công', re.IGNORECASE)
    _SENTENCE_RESULT_VN_FAIL = re.compile(r'câu\s*(\d+)\s*=>\s*thất bại', re.IGNORECASE)
    _SENTENCE_RESULT_OK = re.compile(r'câu\s*(\d+)\s*=>\s*OK', re.IGNORECASE)
    _SENTENCE_FAILED_RENDER = re.compile(r'Xử lý câu\s*(\d+)\s*lỗi:\s*(.+)', re.IGNORECASE)
    _DEBUG_MD5 = re.compile(r'\[Debug-Extract\].*md5_match=(True|False)', re.IGNORECASE)
    _AQT_CLAUSE_EXC = re.compile(r'\[AquesTalk-clause\] synth exception .*: (.+)', re.IGNORECASE)
    _AQT_CLAUSE_INFO = re.compile(r'\[AquesTalk-clause\] idx=(\d+).*clause=(\d+)/(\d+).*synth_len=(\d+)', re.IGNORECASE)
    _SYNTH_START = re.compile(r'\[AquesTalk\] Synth start: voice=(\w+) idx=(\d+) attempt_order=(\d+)', re.IGNORECASE)
    _PRODUCED = re.compile(r'\[AquesTalk\] Synth produced\s*(\S+)')
    _REENCODE = re.compile(r'\[AquesTalk\] Re-encoded synth ->\s*(\S+)')

    def __init__(self, text_widget, detailed_by_default=False):
        self.text_widget = text_widget
        self.compact = True
        self.raw_log_path = os.path.join(tempfile.gettempdir(), f"auto_video_app_rawlog_{int(time.time())}.log")
        self.per_sentence = {}
        self.global_warnings = set()
        self._lock = threading.Lock()
        try:
            with open(self.raw_log_path, "a", encoding="utf-8") as f:
                f.write(f"--- Raw log started at {datetime.utcnow().isoformat()}Z ---\n")
        except Exception:
            pass

    def _save_raw(self, line: str):
        try:
            with open(self.raw_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def handle_raw(self, line: str):
        if not line:
            return
        self._save_raw(line)
        parsed = False
        with self._lock:
            m = self._AQT_SYNTH_ERROR_RE.search(line)
            if m:
                idx = int(m.group(1))
                err_code = m.group(2)
                s = self.per_sentence.setdefault(idx, {"attempts": [], "errors": [], "final": None, "debug_files": set(), "messages": []})
                s["errors"].append(f"AquesTalk_error:{err_code}")
                s["messages"].append(line)
                parsed = True

            m = self._ALL_ATTEMPTS_FAILED_RE.search(line)
            if m:
                idx = int(m.group(1))
                fn = m.group(2)
                s = self.per_sentence.setdefault(idx, {"attempts": [], "errors": [], "final": None, "debug_files": set(), "messages": []})
                s["final"] = "thất bại(all attempts)"
                s["debug_files"].add(fn)
                s["messages"].append(line)
                parsed = True

            m = self._SENTENCE_RESULT_VN_OK.search(line) or self._SENTENCE_RESULT_OK.search(line)
            if m:
                idx = int(m.group(1))
                s = self.per_sentence.setdefault(idx, {"attempts": [], "errors": [], "final": None, "debug_files": set(), "messages": []})
                s["final"] = "thành công"
                s["messages"].append(line)
                parsed = True

            m = self._SENTENCE_RESULT_VN_FAIL.search(line)
            if m:
                idx = int(m.group(1))
                s = self.per_sentence.setdefault(idx, {"attempts": [], "errors": [], "final": None, "debug_files": set(), "messages": []})
                s["final"] = "thất bại"
                s["messages"].append(line)
                parsed = True

            m = self._SENTENCE_FAILED_RENDER.search(line)
            if m:
                idx = int(m.group(1))
                err = m.group(2).strip()
                s = self.per_sentence.setdefault(idx, {"attempts": [], "errors": [], "final": None, "debug_files": set(), "messages": []})
                s["final"] = f"thất bại({err})"
                s["messages"].append(line)
                parsed = True

            m = self._SYNTH_START.search(line)
            if m:
                voice = m.group(1)
                idx = int(m.group(2))
                attempt = int(m.group(3))
                s = self.per_sentence.setdefault(idx, {"attempts": [], "errors": [], "final": None, "debug_files": set(), "messages": []})
                s["attempts"].append({"attempt": attempt, "voice": voice, "raw": []})
                s["messages"].append(line)
                parsed = True

            m = self._AQT_CLAUSE_INFO.search(line)
            if m:
                idx = int(m.group(1))
                clause_info = f"clause {m.group(2)}/{m.group(3)} len={m.group(4)}"
                s = self.per_sentence.setdefault(idx, {"attempts": [], "errors": [], "final": None, "debug_files": set(), "messages": []})
                s["messages"].append(clause_info)
                parsed = True

            m = self._AQT_CLAUSE_EXC.search(line)
            if m:
                exc = m.group(1)
                idx_search = re.search(r'idx=(\d+)', line)
                if idx_search:
                    idx = int(idx_search.group(1))
                    s = self.per_sentence.setdefault(idx, {"attempts": [], "errors": [], "final": None, "debug_files": set(), "messages": []})
                    s["errors"].append(f"clause_exc:{exc[:60]}")
                    s["messages"].append(line)
                else:
                    self.global_warnings.add(f"clause_exc:{exc[:200]}")
                parsed = True

            m = self._PRODUCED.search(line)
            if m:
                fn = m.group(1)
                idx_search = re.search(r'idx=(\d+)', line)
                if idx_search:
                    idx = int(idx_search.group(1))
                    s = self.per_sentence.setdefault(idx, {"attempts": [], "errors": [], "final": None, "debug_files": set(), "messages": []})
                    s["messages"].append(f"produced:{fn}")
                parsed = True

            m = self._REENCODE.search(line)
            if m:
                fn = m.group(1)
                idx_search = re.search(r'idx=(\d+)', line)
                if idx_search:
                    idx = int(idx_search.group(1))
                    s = self.per_sentence.setdefault(idx, {"attempts": [], "errors": [], "final": None, "debug_files": set(), "messages": []})
                    s["messages"].append(f"reencoded:{fn}")
                parsed = True

            m = self._DEBUG_MD5.search(line)
            if m:
                val = m.group(1)
                self.global_warnings.add(f"md5_match={val}")
                parsed = True

            if not parsed:
                idx_search = re.search(r'idx=(\d+)', line)
                if idx_search:
                    idx = int(idx_search.group(1))
                    s = self.per_sentence.setdefault(idx, {"attempts": [], "errors": [], "final": None, "debug_files": set(), "messages": []})
                    s["messages"].append(line)
                else:
                    s = self.per_sentence.setdefault(-1, {"attempts": [], "errors": [], "final": None, "debug_files": set(), "messages": []})
                    s["messages"].append(line)

        self._refresh_display()

    def _render_summary_lines(self):
        lines = []
        keys = sorted(k for k in self.per_sentence.keys() if k != -1)
        for k in keys:
            entry = self.per_sentence[k]
            parts = []
            parts.append(f"Câu {k+1 if k>=0 else k} (idx={k}):")
            msgs_concat = "\n".join([m for m in entry.get("messages", []) if isinstance(m, str)])
            found_success = False
            if re.search(r'(=>\s*thành công|=>\s*OK\b|OK\s*\(wav|OK\s*\()', msgs_concat, re.IGNORECASE):
                found_success = True

            if found_success:
                parts.append("thành công")
            elif entry.get("final"):
                parts.append(entry["final"])
            else:
                if entry["errors"]:
                    parts.append("errors=" + ",".join(entry["errors"][-3:]))
                if entry["attempts"]:
                    last = entry["attempts"][-1]
                    vv = f"attempts={len(entry['attempts'])}"
                    if last.get("voice"):
                        vv += f",voice={last['voice']}"
                    parts.append(vv)
            if entry.get("debug_files"):
                parts.append("debug_files=" + ",".join(sorted(entry["debug_files"])))
            lines.append(" ".join(parts))
        if self.global_warnings:
            gw = "GLOBAL WARN: " + "; ".join(sorted(self.global_warnings))
            lines.insert(0, gw)
        return lines

    def _refresh_display(self):
        try:
            def _do():
                self.text_widget.config(state="normal")
                self.text_widget.delete("1.0", "end")
                if self.compact:
                    for ln in self._render_summary_lines():
                        self.text_widget.insert("end", ln + "\n")
                else:
                    try:
                        with open(self.raw_log_path, "r", encoding="utf-8") as f:
                            alltxt = f.read().strip().splitlines()
                        tail = alltxt[-500:]
                        for ln in tail:
                            self.text_widget.insert("end", ln + "\n")
                    except Exception:
                        for k in sorted(self.per_sentence.keys()):
                            for m in self.per_sentence[k]["messages"][-10:]:
                                self.text_widget.insert("end", (m[:800] if isinstance(m, str) else str(m)) + "\n")
                self.text_widget.see("end")
                self.text_widget.config(state="disabled")
            try:
                self.text_widget.after(1, _do)
            except Exception:
                _do()
        except Exception:
            pass

    def toggle_compact(self, v: bool):
        with self._lock:
            self.compact = bool(v)
        self._refresh_display()

    def export_summary(self, out_path: str):
        with self._lock:
            lines = []
            lines.append(f"Summary exported at {datetime.utcnow().isoformat()}Z")
            lines.extend(self._render_summary_lines())
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    for ln in lines:
                        f.write(ln + "\n")
                return True
            except Exception:
                return False

# ----------------- End LogManager -------------------------------------

class AutoVideoApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1200x780")
        self.root.minsize(1120, 720)
        self.root.state('zoomed')
        P = PaletteClassic
        self.root.configure(bg=P.BG_APP)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=P.BG_APP)
        style.configure("TLabelframe", background=P.BG_APP, relief="groove", borderwidth=1)
        style.configure("TLabelframe.Label", background=P.BG_APP, foreground="black")
        style.configure("TLabel", background=P.BG_APP, foreground="black")
        style.configure("Muted.TLabel", background=P.BG_APP, foreground=P.TEXT_MUTE)
        style.configure("TButton", padding=6)
        style.configure("Primary.TButton",
                        padding=10, font=("Segoe UI", 10, "bold"),
                        foreground="white", background=P.BTN_PRIMARY)
        style.map("Primary.TButton", background=[("active", P.BTN_PRIMARY_ACTIVE), ("pressed", P.BTN_PRIMARY_ACTIVE)])
        style.configure("Steel.TButton", padding=8, foreground="white", background=P.BTN_STEEL)
        style.map("Steel.TButton", background=[("active", P.BTN_STEEL_ACTIVE), ("pressed", P.BTN_STEEL_ACTIVE)])
        self.output_name = tk.StringVar(value="video_hoithoai.mp4")
        self.output_dir = os.path.expanduser("~/Downloads")
        self.voicevox_speakers = []
        self.edge_tts_speakers = [{"name": "en-US-JennyNeural", "id": "en-US-JennyNeural"}]
        self.image_paths = []
        self.csv_path = ""
        self.effect_var = tk.StringVar(value="Zoom")
        self.video_effect_var = tk.StringVar(value="Giữ nguyên")
        self._swatch = {}
        self.container = ttk.Frame(self.root)
        self.container.pack(fill="both", expand=True, padx=10, pady=10)
        self.left_panel = ttk.Frame(self.container)
        self.left_panel.pack(side="left", fill="both", expand=True)
        self.right_panel = ttk.Frame(self.container, width=380)
        self.right_panel.pack(side="right", fill="both", padx=(12, 0))
        self.log_group = ttk.LabelFrame(self.right_panel, text="Bảng log chi tiết / tóm tắt")
        self.log_group.pack(fill="both", expand=True)
        self.log_text = tk.Text(self.log_group, bg="white", state="disabled")
        self.log_text.pack(fill="both", expand=True)

        # Initialize LogManager
        self.log_manager = LogManager(self.log_text, detailed_by_default=False)
        # Control row for compact/detailed and export
        c_row = ttk.Frame(self.right_panel)
        c_row.pack(fill="x", pady=(4, 6))
        self.compact_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(c_row, text="Log tóm tắt (Compact)", variable=self.compact_var,
                        command=lambda: self.log_manager.toggle_compact(self.compact_var.get())).pack(side="left", padx=(6,4))
        ttk.Button(c_row, text="Lưu tóm tắt", command=self._export_summary, style="Steel.TButton").pack(side="left", padx=(4,4))
        ttk.Button(c_row, text="Mở raw log", command=self._open_raw_log, style="Steel.TButton").pack(side="left", padx=(4,4))

        # re-entrancy guard + run id
        self._running = False
        self._run_id = 0
        self.create_btn = None  # will be set in build_action_row

        self.build_input_card(self.left_panel, P)
        self.build_character_card(self.left_panel, "A", P)
        self.build_character_card(self.left_panel, "B", P)
        self.build_output_card(self.left_panel, P)
        self.build_action_row(self.left_panel)
        self.status = ttk.Label(self.left_panel, text="Sẵn sàng…", style="Muted.TLabel")
        self.status.pack(fill="x", pady=(4, 0))
        threading.Thread(target=self.load_voicevox_speakers, daemon=True).start()
        threading.Thread(target=self.probe_and_merge_aquestalk_voices, daemon=True).start()

    # (UI builder methods omitted here for brevity — unchanged from your version)
    def build_card(self, parent, title, P):
        outer = ttk.LabelFrame(parent, text=title)
        outer.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(outer, bg=P.FRAME_BG, bd=1, highlightbackground=P.BORDER, highlightthickness=1)
        inner.pack(fill="x", padx=8, pady=8)
        return outer, inner

    def build_input_card(self, parent, P):
        _, row = self.build_card(parent, "Đầu vào", P)
        ttk.Label(row, text="Loại nguồn:", background=P.FRAME_BG).grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.input_type = tk.StringVar(value="Ảnh")
        self.input_type_option = ttk.Combobox(
            row, values=["Ảnh", "Video"], state="readonly", width=10, textvariable=self.input_type
        )
        self.input_type_option.grid(row=0, column=1, sticky="w", padx=6, pady=6)
        self.input_type_option.bind("<<ComboboxSelected>>", self.on_input_type_change)
        ttk.Button(row, text="📄 Chọn file CSV", command=self.select_csv, style="Steel.TButton").grid(
            row=0, column=2, sticky="w", padx=6, pady=6
        )
        self.csv_label = ttk.Label(row, text="Chưa chọn file CSV", style="Muted.TLabel", background=P.FRAME_BG)
        self.csv_label.grid(row=0, column=3, sticky="w", padx=6, pady=6)
        self.effect_label = ttk.Label(row, text="Hiệu ứng ảnh:", background=P.FRAME_BG)
        self.effect_label.grid(row=1, column=0, sticky="w", padx=6, pady=6)
        self.effect_option = ttk.Combobox(
            row, values=["Tĩnh", "Zoom", "Pan", "Zoom + Pan"], state="readonly", width=15, textvariable=self.effect_var
        )
        self.effect_option.grid(row=1, column=1, sticky="w", padx=6, pady=6)
        self.video_effect_label = ttk.Label(row, text="Hiệu ứng video:", background=P.FRAME_BG)
        self.video_effect_option = ttk.Combobox(
            row, values=["Giữ nguyên", "Mờ nhẹ", "Đen trắng", "Làm tối nhẹ"],
            state="readonly", width=15, textvariable=self.video_effect_var
        )
        self.pick_btn = ttk.Button(row, text="🖼️ Chọn ảnh", command=self.select_images, style="Steel.TButton")
        self.pick_btn.grid(row=1, column=2, sticky="w", padx=6, pady=6)
        self.image_label = ttk.Label(row, text="0 ảnh đã chọn", style="Muted.TLabel", background=P.FRAME_BG)
        self.image_label.grid(row=1, column=3, sticky="w", padx=6, pady=6)
        self.on_input_type_change()
        for i in range(5):
            row.grid_columnconfigure(i, weight=1)

    def _make_swatch(self, parent, key, color, row, col, pady=6):
        canvas = tk.Canvas(parent, width=18, height=18, bg=parent["bg"], highlightthickness=0)
        canvas.grid(row=row, column=col, sticky="w", padx=(6, 0), pady=pady)
        self._swatch[key] = canvas
        self._paint_swatch(canvas, color)
        return canvas

    @staticmethod
    def _paint_swatch(canvas: tk.Canvas, color: str):
        canvas.delete("all")
        r = 5
        x0, y0, x1, y1 = 1, 1, 17, 17
        canvas.create_oval(x0, y0, x1, y1, outline="#ffffff", fill="#ffffff", width=2)
        canvas.create_oval(x0+2, y0+2, x1-2, y1-2, outline="#2b2b2b", fill=color, width=1)

    def build_character_card(self, parent, prefix, P):
        _, row = self.build_card(parent, f"Cài đặt nhân vật {prefix}", P)
        ttk.Label(row, text="Nguồn voice: Voicevox", background=P.FRAME_BG).grid(row=0, column=0, sticky="w", padx=6, pady=6)
        ttk.Label(row, text="Giọng:", background=P.FRAME_BG).grid(row=0, column=2, sticky="w", padx=6, pady=6)
        setattr(self, f"voice_option_{prefix}", ttk.Combobox(row, values=["(đang tải…)"], state="readonly", width=28))
        getattr(self, f"voice_option_{prefix}").grid(row=0, column=3, sticky="w", padx=6, pady=6)
        ttk.Label(row, text="Tốc độ:", background=P.FRAME_BG).grid(row=1, column=0, sticky="w", padx=6, pady=6)
        setattr(self, f"voice_speed_{prefix}", tk.DoubleVar(value=1.0))
        ttk.Entry(row, textvariable=getattr(self, f"voice_speed_{prefix}"), width=8).grid(
            row=1, column=1, sticky="w", padx=6, pady=6
        )
        ttk.Label(row, text="Nghỉ (s):", background=P.FRAME_BG).grid(row=1, column=2, sticky="w", padx=6, pady=6)
        setattr(self, f"pause_sec_{prefix}", tk.DoubleVar(value=0.7))
        ttk.Entry(row, textvariable=getattr(self, f"pause_sec_{prefix}"), width=8).grid(
            row=1, column=3, sticky="w", padx=6, pady=6
        )
        ttk.Label(row, text="Font:", background=P.FRAME_BG).grid(row=2, column=2, sticky="w", padx=6, pady=6)
        font_dir = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
        font_list = [f for f in os.listdir(font_dir) if f.lower().endswith((".ttf", ".ttc", ".otf"))] if os.path.isdir(font_dir) else []
        setattr(self, f"font_option_{prefix}",
                ttk.Combobox(row, values=sorted(font_list) or ["Arial.ttf"], state="readonly", width=28))
        getattr(self, f"font_option_{prefix}").set(
            "YuGothB.ttc" if "YuGothB.ttc" in font_list else (font_list[0] if font_list else "Arial.ttf")
        )
        getattr(self, f"font_option_{prefix}").grid(row=2, column=3, sticky="w", padx=6, pady=6)
        ttk.Label(row, text="Âm lượng (%):", background=P.FRAME_BG).grid(row=2, column=0, sticky="w", padx=6, pady=6)
        setattr(self, f"volume_entry_{prefix}", tk.StringVar(value="300"))
        ttk.Entry(row, textvariable=getattr(self, f"volume_entry_{prefix}"), width=8).grid(
            row=2, column=1, sticky="w", padx=6, pady=6
        )

        ttk.Label(row, text="Cỡ chữ (px):", background=P.FRAME_BG).grid(row=3, column=0, sticky="w", padx=6, pady=6)
        setattr(self, f"font_size_{prefix}", tk.IntVar(value=40))
        ttk.Entry(row, textvariable=getattr(self, f"font_size_{prefix}"), width=8).grid(
            row=3, column=1, sticky="w", padx=6, pady=6
        )

        ttk.Label(row, text="Màu chữ:", background=P.FRAME_BG).grid(row=3, column=2, sticky="w", padx=6, pady=6)
        setattr(self, f"subtitle_color_{prefix}", "#FFFFFF")
        btn_sub = tk.Button(row, text="Chọn",
                            command=lambda p=prefix: self.pick_color(f"subtitle_color_{p}", f"btn_sub_{p}"))
        btn_sub.configure(bg=getattr(self, f"subtitle_color_{prefix}"))
        btn_sub.grid(row=3, column=3, sticky="w", padx=(6, 0), pady=6)
        setattr(self, f"btn_sub_{prefix}", btn_sub)
        ttk.Label(row, text="Viền chữ:", background=P.FRAME_BG).grid(row=4, column=0, sticky="w", padx=6, pady=6)
        setattr(self, f"stroke_color_{prefix}", "#000000")
        btn_stk = tk.Button(row, text="Chọn",
                            command=lambda p=prefix: self.pick_color(f"stroke_color_{p}", f"btn_stk_{p}"))
        btn_stk.configure(bg=getattr(self, f"stroke_color_{prefix}"))
        btn_stk.grid(row=4, column=1, sticky="w", padx=(6, 0), pady=6)
        setattr(self, f"btn_stk_{prefix}", btn_stk)
        ttk.Label(row, text="Size viền:", background=P.FRAME_BG).grid(row=4, column=2, sticky="w", padx=6, pady=6)
        setattr(self, f"stroke_size_{prefix}", tk.IntVar(value=3))
        ttk.Entry(row, textvariable=getattr(self, f"stroke_size_{prefix}"), width=8).grid(
            row=4, column=3, sticky="w", padx=6, pady=6
        )
        ttk.Label(row, text="Nền phụ đề:", background=P.FRAME_BG).grid(row=5, column=0, sticky="w", padx=6, pady=6)
        setattr(self, f"bg_color_{prefix}", "#000000")
        btn_bg = tk.Button(row, text="Chọn",
                           command=lambda p=prefix: self.pick_color(f"bg_color_{p}", f"btn_bg_{p}"))
        btn_bg.configure(bg=getattr(self, f"bg_color_{prefix}"))
        btn_bg.grid(row=5, column=1, sticky="w", padx=(6, 0), pady=6)
        setattr(self, f"btn_bg_{prefix}", btn_bg)
        ttk.Label(row, text="Độ trong suốt nền:", background=P.FRAME_BG).grid(row=5, column=2, sticky="w", padx=6, pady=6)
        setattr(self, f"bg_opacity_{prefix}", tk.IntVar(value=200))
        ttk.Scale(row, from_=0, to=255, orient="horizontal", variable=getattr(self, f"bg_opacity_{prefix}")).grid(
            row=5, column=3, sticky="ew", padx=6, pady=6
        )
        setattr(self, f"subtitle_full_width_{prefix}", tk.BooleanVar(value=False))
        ttk.Checkbutton(
            row,
            text="Nền phụ đề full width (3 dòng)",
            variable=getattr(self, f"subtitle_full_width_{prefix}"),
            onvalue=True,
            offvalue=False
        ).grid(row=5, column=4, sticky="w", padx=(6, 0), pady=6)
        try:
            icons_root = _icons_root()
            side_dir = icons_root / prefix
            _char_list = _list_icon_chars(side_dir) or ["(không có)"]
        except Exception:
            _char_list = ["(không có)"]
        setattr(self, f"icon_char_{prefix}", tk.StringVar(value=_char_list[0]))
        ttk.Label(row, text="Nhân vật (Icon):", background=P.FRAME_BG).grid(row=8, column=0, sticky="w", padx=6, pady=4)
        ttk.Combobox(row, state="readonly", width=18, values=_char_list,
                    textvariable=getattr(self, f"icon_char_{prefix}")).grid(row=8, column=1, sticky="w", padx=6, pady=4)

    def build_output_card(self, parent, P):
        _, row = self.build_card(parent, "Đầu ra", P)
        ttk.Label(row, text="Tên video:", background=P.FRAME_BG).grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.output_name = tk.StringVar(value="video_hoithoai.mp4")
        ttk.Entry(row, textvariable=self.output_name, width=40).grid(row=0, column=1, sticky="w", padx=6, pady=6)
        ttk.Label(row, text="Chất lượng:", background=P.FRAME_BG).grid(row=0, column=2, sticky="w", padx=6, pady=6)
        self.encoder_preset_var = tk.StringVar(value="p5")
        self.encoder_preset_box = ttk.Combobox(row, state="readonly", width=12,
                                               values=["p3 (Nhanh)", "p5 (Cân bằng)", "p7 (Đẹp)"],
                                               textvariable=self.encoder_preset_var)
        self.encoder_preset_box.grid(row=0, column=3, sticky="w", padx=6, pady=6)
        def _on_preset_change(event=None):
            v = self.encoder_preset_var.get()
            if v.startswith("p3"): self.encoder_preset_var.set("p3")
            elif v.startswith("p7"): self.encoder_preset_var.set("p7")
            else: self.encoder_preset_var.set("p5")
        self.encoder_preset_box.bind("<<ComboboxSelected>>", _on_preset_change)
        ttk.Button(row, text="📁 Chọn thư mục lưu", command=self.select_output_dir, style="Steel.TButton").grid(
            row=0, column=2, sticky="w", padx=6, pady=6
        )
        self.output_dir_label = ttk.Label(row, text=f"Thư mục: {self.output_dir}", style="Muted.TLabel", background=P.FRAME_BG)
        self.output_dir_label.grid(row=0, column=3, sticky="w", padx=6, pady=6)
        for i in range(4):
            row.grid_columnconfigure(i, weight=1)

    def build_action_row(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(2, 6))
        self.create_btn = ttk.Button(row, text="🟩 TẠO VIDEO NGAY!", command=self.on_create, width=24, style="Primary.TButton")
        self.create_btn.pack(side="left", padx=(0, 8))
        ttk.Button(row, text="🗑️ XÓA FILE TẠM", command=self.clean_temp, width=20, style="Steel.TButton").pack(
            side="left"
        )
        self.progress = ttk.Progressbar(row, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(12, 0))

    def on_input_type_change(self, event=None):
        mode = self.input_type.get()
        if mode == "Video":
            try:
                self.effect_label.grid_remove()
                self.effect_option.grid_remove()
            except Exception:
                pass
            try:
                self.video_effect_label.grid(row=1, column=0, sticky="w", padx=6, pady=6)
                self.video_effect_option.grid(row=1, column=1, sticky="w", padx=6, pady=6)
            except Exception:
                pass
            self.pick_btn.config(text="🎞️ Chọn video")
        else:
            try:
                self.video_effect_label.grid_remove()
                self.video_effect_option.grid_remove()
            except Exception:
                pass
            try:
                self.effect_label.grid(row=1, column=0, sticky="w", padx=6, pady=6)
                self.effect_option.grid(row=1, column=1, sticky="w", padx=6, pady=6)
            except Exception:
                pass
            self.pick_btn.config(text="🖼️ Chọn ảnh")

    # Replaced add_log: route through LogManager
    def add_log(self, s: str):
        try:
            self.log_manager.handle_raw(s)
        except Exception:
            try:
                self.log_text.config(state="normal")
                self.log_text.insert("end", s + "\n")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
            except Exception:
                pass

    def _export_summary(self):
        p = filedialog.asksaveasfilename(title="Lưu tóm tắt log", defaultextension=".txt", filetypes=[("Text", "*.txt")])
        if not p:
            return
        ok = self.log_manager.export_summary(p)
        if ok:
            messagebox.showinfo("Lưu tóm tắt", f"Đã lưu tóm tắt: {p}")
        else:
            messagebox.showerror("Lỗi", "Không thể lưu tóm tắt.")

    def _open_raw_log(self):
        try:
            path = self.log_manager.raw_log_path
            if os.path.exists(path):
                if sys.platform == "win32":
                    os.startfile(path)
                else:
                    subprocess.run(["xdg-open", path])
            else:
                messagebox.showinfo("Raw log", "File raw log chưa được tạo.")
        except Exception as e:
            messagebox.showerror("Lỗi", str(e))

    def load_voicevox_speakers(self):
        try:
            r = requests.get("http://127.0.0.1:50021/speakers", timeout=3)
            if r.status_code == 200:
                data = r.json()
                lst = []
                for sp in data:
                    name = sp.get("name", "")
                    for s in sp.get("styles", []):
                        lst.append({"name": f"{name} ({s.get('name','')})", "id": int(s.get("id", 0))})
                self.voicevox_speakers = lst
                self.refresh_voice_list("A")
                self.refresh_voice_list("B")
        except Exception:
            pass

    def refresh_voice_list(self, prefix):
        combo = getattr(self, f"voice_option_{prefix}")
        combo['values'] = [v['name'] for v in self.voicevox_speakers] or ['(chưa có)']
        if self.voicevox_speakers:
            combo.current(0)

    def probe_and_merge_aquestalk_voices(self):
        avail = []
        base = os.path.dirname(os.path.abspath(__file__))
        candidate_dir = os.path.join(base, "aquestalk", "aquestalk")
        if not os.path.isdir(candidate_dir):
            candidate_dir = os.path.join(base, "aquestalk")
        self.add_log(f"[AquesTalk] Probe folder: {candidate_dir}")
        if os.path.isdir(candidate_dir):
            try:
                try:
                    os.add_dll_directory(candidate_dir)
                except Exception:
                    os.environ["PATH"] = candidate_dir + os.pathsep + os.environ.get("PATH", "")
                subs = sorted([d for d in os.listdir(candidate_dir) if os.path.isdir(os.path.join(candidate_dir, d))])
                self.add_log("[AquesTalk] Subfolders: " + ", ".join(subs[:50]))
                for s in subs:
                    if s and (s[0].lower() in ("f","m","r","j") or s.isalnum()):
                        avail.append(s)
            except Exception as e:
                self.add_log(f"[AquesTalk] list subfolders error: {e}")
        try:
            from synth_aquestalk import list_aquestalk_voices
            try:
                probe = list_aquestalk_voices(try_short_test=False)
                if probe:
                    avail = probe
                    self.add_log(f"[AquesTalk] synth_aquestalk probe returned: {', '.join(avail)}")
            except Exception as e:
                self.add_log(f"[AquesTalk] synth_aquestalk probe error: {e}")
        except Exception:
            pass

        aq_tagged = [f"[AquesTalk] {v}" for v in avail] if avail else []
        def update_ui():
            for p in ("A", "B"):
                try:
                    combo = getattr(self, f"voice_option_{p}")
                    existing = list(combo['values']) if combo['values'] else []
                    vv_existing = [v for v in existing if not (isinstance(v, str) and v.startswith("[AquesTalk]"))]
                    vv_names = [v['name'] for v in self.voicevox_speakers] if self.voicevox_speakers else vv_existing
                    new_vals = vv_names + aq_tagged if vv_names else (aq_tagged or ["(chưa có)"])
                    cur = combo.get()
                    combo['values'] = new_vals
                    if cur in new_vals:
                        combo.set(cur)
                    else:
                        combo.set(new_vals[0] if new_vals else "")
                except Exception:
                    pass
            if aq_tagged:
                self.add_log("[AquesTalk] Voices: " + ", ".join(avail))
            else:
                self.add_log("[AquesTalk] Không tìm thấy giọng AquesTalk (kiểm tra thư mục / DLL / kiến trúc Python).")
        self.root.after(0, update_ui)

    def select_csv(self):
        p = filedialog.askopenfilename(title="Chọn file CSV", filetypes=[("CSV", "*.csv")])
        if p:
            self.csv_path = p
            self.csv_label.config(text=os.path.basename(p))

    def select_images(self):
        mode = self.input_type.get()
        if mode == "Video":
            ft = [("Video", "*.mp4;*.mov;*.mkv;*.avi;*.webm")]
            title = "Chọn video"
        else:
            ft = [("Ảnh", "*.jpg;*.jpeg;*.png;*.bmp;*.webp")]
            title = "Chọn ảnh"
        files = filedialog.askopenfilenames(title=title, filetypes=ft)
        if files:
            self.image_paths = list(files)
            self.image_label.config(text=f"{len(self.image_paths)} mục đã chọn")

    def select_output_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.output_dir = d
            self.output_dir_label.config(text=f"Thư mục: {self.output_dir}")

    def pick_color(self, varname, btn_attr):
        c = colorchooser.askcolor()[1]
        if c:
            setattr(self, varname, c)
            btn = getattr(self, btn_attr)
            try:
                btn.configure(bg=c)
            except Exception:
                pass

    def clean_temp(self):
        temp = tempfile.gettempdir()
        removed = 0
        for fn in os.listdir(temp):
            if fn.startswith(("line_", "subtitle_", "temp_", "dialogue_", "concat_", "pad_", "line_pad_")):
                try:
                    os.remove(os.path.join(temp, fn))
                    removed += 1
                except Exception:
                    pass
        messagebox.showinfo("Dọn", f"Đã xóa {removed} file tạm.") 

    def on_create(self):
        if getattr(self, "_running", False):
            messagebox.showinfo("Đang chạy", "Đang có tiến trình tạo video. Vui lòng đợi hoàn tất hoặc hủy trước khi bắt đầu lần nữa.")
            return
        threading.Thread(target=lambda: asyncio.run(self.create_video_dialogue()), daemon=True).start()

    async def create_video_dialogue(self):
        if getattr(self, "_running", False):
            return
        self._running = True
        self._run_id += 1
        run_id = self._run_id
        try:
            try:
                if self.create_btn:
                    self.create_btn.config(state="disabled")
            except Exception:
                pass

            self.add_log(f"[RUN {run_id}] started")
            if not self.csv_path:
                messagebox.showerror("Lỗi", "Vui lòng chọn file CSV.")
                return
            if not self.image_paths:
                messagebox.showerror("Lỗi", "Vui lòng chọn ít nhất một ảnh/video nền.")
                return
            lines = []
            with open(self.csv_path, encoding="utf-8") as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) >= 2:
                        role = row[0].strip().upper()
                        text = ",".join(row[1:]).strip()
                        if role in ("A", "B") and text:
                            lines.append({"role": role, "text": text})
            if not lines:
                messagebox.showerror("Lỗi", "CSV không hợp lệ (cột 1=A/B, cột 2=câu).")
                return
            icons_root = _icons_root()
            A_name = getattr(self, "icon_char_A", tk.StringVar(value="(không có)")).get()
            B_name = getattr(self, "icon_char_B", tk.StringVar(value="(không có)")).get()
            A_icon_dir = str(icons_root / "A" / (A_name if A_name != "(không có)" else "1"))
            B_icon_dir = str(icons_root / "B" / (B_name if B_name != "(không có)" else "1"))
            A_cfg = _resolve_icon_char(icons_root / "A", A_name if A_name != "(không có)" else None, "left")
            B_cfg = _resolve_icon_char(icons_root / "B", B_name if B_name != "(không có)" else None, "right")
            icons_cfg = {"A": A_cfg, "B": B_cfg}
            configs = {}
            for prefix in ["A", "B"]:
                voice_choice = getattr(self, f"voice_option_{prefix}").get()
                voice_source = "Voicevox"
                speaker = None
                if isinstance(voice_choice, str) and voice_choice.startswith("[AquesTalk]"):
                    voice_source = "AquesTalk"
                    speaker = voice_choice.replace("[AquesTalk] ", "").strip()
                else:
                    vlist = self.voicevox_speakers or []
                    speaker = next((s['id'] for s in vlist if s['name'] == voice_choice), None)
                font_dir = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
                font_path = os.path.join(font_dir, getattr(self, f"font_option_{prefix}").get())
                try:
                    font_size_val = int(getattr(self, f"font_size_{prefix}").get())
                except Exception:
                    font_size_val = 40

                configs[prefix] = {
                    "voice_source": voice_source,
                    "pause_sec": float(getattr(self, f"pause_sec_{prefix}").get()),
                    "video_effect": self.video_effect_var.get(),
                    "speaker_id": speaker,
                    "voice_speed": float(getattr(self, f"voice_speed_{prefix}").get()),
                    "font_path": font_path,
                    "font_size": font_size_val,
                    "volume": int(getattr(self, f"volume_entry_{prefix}").get()),
                    "bg_opacity": int(getattr(self, f"bg_opacity_{prefix}").get()) if hasattr(self, f"bg_opacity_{prefix}") else 200,
                    "subtitle_color": getattr(self, f"subtitle_color_{prefix}") if hasattr(self, f"subtitle_color_{prefix}") else "#FFFFFF",
                    "stroke_color": getattr(self, f"stroke_color_{prefix}") if hasattr(self, f"stroke_color_{prefix}") else "#000000",
                    "stroke_size": int(getattr(self, f"stroke_size_{prefix}").get()) if hasattr(self, f"stroke_size_{prefix}") else 2,
                    "bg_color": getattr(self, f"bg_color_{prefix}") if hasattr(self, f"bg_color_{prefix}") else "#000000",
                    "encoder_preset": self.encoder_preset_var.get(),
                    "icons": icons_cfg,
                    "speak_role": prefix,
                    "icon_a_dir": A_icon_dir,
                    "icon_b_dir": B_icon_dir,
                    "subtitle_full_width": getattr(self, f"subtitle_full_width_{prefix}").get() if hasattr(self, f"subtitle_full_width_{prefix}") else False,
                    "effect": self.effect_var.get(),
                    # keep clause-based default for AquesTalk to enable splitting into vế
                    "force_clause": True,
                    # AquesTalk per-text retries and conservative flags
                    "aquestalk_try_other_voices": False,
                    "aquestalk_aggressive_retry": False,
                    "aquestalk_per_text_retries": 2
                }
            with self.log_manager._lock:
                self.log_manager.per_sentence.clear()
                self.log_manager.global_warnings.clear()
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.config(state="disabled")
            total = len(lines)
            done = 0
            self.progress['value'] = 0
            self.progress['maximum'] = 100
            temp_dir = tempfile.gettempdir()
            video_paths = []
            # Use AUTO_VIDEO_MAX_THREADS to determine parallelism (cap)
            max_workers = max(1, min(AUTO_VIDEO_MAX_THREADS, max(2, (os.cpu_count() or 4))))
            self.add_log(f"[RUN {run_id}] Sử dụng tối đa {max_workers} luồng FFmpeg/Voicevox… (AUTO_VIDEO_MAX_THREADS={AUTO_VIDEO_MAX_THREADS})")

            sem = asyncio.Semaphore(max_workers)

            def add_log_run(s: str):
                try:
                    self.add_log(f"[RUN {run_id}] {s}")
                except Exception:
                    try:
                        self.add_log(s)
                    except Exception:
                        pass

            async def process_one(idx, line):
                async with sem:
                    out_path = os.path.join(temp_dir, f"dialogue_{idx}.mp4")
                    try:
                        await render_sentence_dialogue(
                            index=idx,
                            sentence=line["text"],
                            config=configs[line["role"]],
                            image_paths=self.image_paths,
                            output_path=out_path,
                            add_log=add_log_run
                        )
                        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                            video_paths.append((idx, out_path))
                            self.add_log(f"[RUN {run_id}] Xử lý câu {idx+1} => thành công")
                        else:
                            self.add_log(f"[RUN {run_id}] Xử lý câu {idx+1} => thất bại (no output)")
                        nonlocal done
                        done += 1
                        pct = int(done * 100 / max(1, total))
                        self.progress['value'] = pct
                        self.status.config(text=f"Đang xử lý… {pct}%", foreground="blue")
                        self.root.update_idletasks()
                    except Exception as e:
                        self.add_log(f"[RUN {run_id}] Xử lý câu {idx+1} lỗi: {e}")

            tasks = [process_one(idx, line) for idx, line in enumerate(lines)]
            await asyncio.gather(*tasks)
            concat_list_file_path = os.path.join(temp_dir, "concat_dialogue.txt")
            sorted_videos = [p for _, p in sorted(video_paths, key=lambda t: t[0])]
            if not sorted_videos:
                messagebox.showerror("Lỗi", "Không có file đoạn video tạo được. Kiểm tra log.")
                self.status.config(text="Thất bại", foreground="red")
                return
            for p in sorted_videos:
                if not os.path.exists(p) or os.path.getsize(p) < 1024:
                    self.add_log(f"[RUN {run_id}] [Concat] Missing or invalid intermediate file: {p}")
                    messagebox.showerror("Lỗi", f"Missing or invalid intermediate file: {p}")
                    return
            with open(concat_list_file_path, "w", encoding="utf-8") as f:
                for p in sorted_videos:
                    f.write(f"file '{normalize_path_for_ffmpeg(os.path.abspath(p))}'\n")
            final_output = os.path.join(self.output_dir, self.output_name.get())
            base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            ffmpeg_path = os.path.join(base_dir, "ffmpeg", "ffmpeg.exe")
            try:
                concat_cmd = [
                    ffmpeg_path, "-y", "-threads", str(min(AUTO_VIDEO_MAX_THREADS, max(1, os.cpu_count() or 1))),
                    "-f", "concat", "-safe", "0",
                    "-i", normalize_path_for_ffmpeg(concat_list_file_path),
                    "-c", "copy", normalize_path_for_ffmpeg(final_output)
                ]
                subprocess.run(concat_cmd, check=True, stderr=subprocess.PIPE, stdout=subprocess.PIPE, startupinfo=SI, creationflags=(CREATE_NO_WINDOW if sys.platform=="win32" else 0))
                self.status.config(text=f"✅ Xong! Video đã lưu: {final_output}", foreground="darkgreen")
                messagebox.showinfo("Hoàn tất", f"Video đã lưu:\n{final_output}")
                self.add_log(f"[RUN {run_id}] finished successfully")
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.decode("utf-8", "ignore") if isinstance(e.stderr, (bytes, bytearray)) else str(e)
                self.status.config(text="Lỗi ghép video.", foreground="red")
                self.add_log(f"[RUN {run_id}] [FFmpeg] concat error: " + stderr)
                messagebox.showerror("Lỗi", stderr[:2000])
            except Exception as ex:
                self.add_log(f"[RUN {run_id}] [ERROR] {ex}")
                messagebox.showerror("Lỗi", str(ex))
            finally:
                self.progress['value'] = 100
        finally:
            try:
                if self.create_btn:
                    self.create_btn.config(state="normal")
            except Exception:
                pass
            self._running = False
            try:
                self.add_log(f"[RUN {run_id}] ended")
            except Exception:
                pass

def main():
    root = tk.Tk()
    root.withdraw()
    if not check_activation(root):
        sys.exit(1)
    root.deiconify()
    root.state('zoomed')
    app = AutoVideoApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()