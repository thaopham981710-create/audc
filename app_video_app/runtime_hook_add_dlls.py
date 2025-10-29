# runtime_hook_add_dlls.py
# PyInstaller runtime hook: run early when onefile EXE extracts to temp.
# Adds sys._MEIPASS to sys.path so Python can import bundled .py modules (e.g. aq_normalize.py),
# and adds likely native library folders to DLL search path and PATH so native DLLs/exes load.
import os
import sys

def _add_dir_to_dll_search(dirpath):
    try:
        if not dirpath:
            return
        d = os.path.normpath(dirpath)
        if os.path.isdir(d):
            try:
                if hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(d)
            except Exception:
                pass
            # also ensure subprocess can find dlls/exes
            p = os.environ.get("PATH", "")
            if d not in p:
                os.environ["PATH"] = d + os.pathsep + p
    except Exception:
        pass

def _safe_listdir(path):
    try:
        return os.listdir(path)
    except Exception:
        return []

def _scan_and_add(base):
    # 1) Ensure base is in sys.path so `import aq_normalize` works when bundled as data
    try:
        if base not in sys.path:
            sys.path.insert(0, base)
    except Exception:
        pass

    # 2) Candidate folders to add to DLL search path / PATH
    cand = []
    cand.append(os.path.join(base, "MeCab", "bin"))
    cand.append(os.path.join(base, "ffmpeg"))
    cand.append(os.path.join(base, "aquestalk"))
    cand.append(os.path.join(base, "_internal", "MeCab", "bin"))
    cand.append(os.path.join(base, "_internal", "aquestalk"))
    cand.append(os.path.join(base, "_internal", "ffmpeg"))

    # If aquestalk has many subfolders (voices), add them if they contain DLLs
    try:
        aqroot = os.path.join(base, "aquestalk")
        if os.path.isdir(aqroot):
            for entry in _safe_listdir(aqroot):
                p = os.path.join(aqroot, entry)
                if os.path.isdir(p):
                    cand.append(p)
                    # also include nested directories
                    for sub in _safe_listdir(p):
                        sp = os.path.join(p, sub)
                        if os.path.isdir(sp):
                            cand.append(sp)
    except Exception:
        pass

    # Add any immediate subfolder of base that contains .dll files
    try:
        for entry in _safe_listdir(base):
            p = os.path.join(base, entry)
            if os.path.isdir(p):
                for fn in _safe_listdir(p):
                    if fn.lower().endswith(".dll"):
                        cand.append(p)
                        break
    except Exception:
        pass

    # Unique and add
    seen = set()
    for d in cand:
        if not d:
            continue
        nd = os.path.normpath(d)
        if nd in seen:
            continue
        seen.add(nd)
        _add_dir_to_dll_search(nd)

# Run only for frozen onefile execution
try:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if base:
            _scan_and_add(base)
except Exception:
    pass