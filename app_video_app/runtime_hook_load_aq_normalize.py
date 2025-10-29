# runtime_hook_load_aq_normalize.py
# Robust runtime hook to load external aq_normalize.py (prefer exe-dir), with safe import logic.
# Avoids using importlib.machinery directly to prevent "module 'importlib' has no attribute 'machinery'".
import sys
import os
import traceback
import importlib.util

def _try_load_path(path):
    try:
        # read bytes and try decodings to detect encoding issues
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        print(f"[runtime-hook] cannot read {path}: {e}")
        return False, f"read-failed:{e}"

    for enc in ("utf-8-sig", "utf-8", "cp932", "latin-1"):
        try:
            src = raw.decode(enc)
        except Exception:
            continue
        # Syntax check
        try:
            codeobj = compile(src, path, "exec")
        except SyntaxError as se:
            print(f"[runtime-hook] aq_normalize compile SyntaxError with encoding={enc}: {se}")
            # print excerpt of surrounding lines for easier debugging
            try:
                lines = src.splitlines()
                lineno = getattr(se, "lineno", 0) or 0
                start = max(0, lineno - 6)
                end = min(len(lines), lineno + 6)
                print(f"[runtime-hook] Showing lines {start+1}..{end} (error at line {lineno}):")
                for i in range(start, end):
                    prefix = ">> " if (i+1) == lineno else "   "
                    print(f"{prefix}{i+1:04d}: {lines[i]!r}")
            except Exception:
                pass
            # try next encoding
            continue
        # If compile ok, exec into module and register
        try:
            spec = importlib.util.spec_from_file_location("aq_normalize", path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                sys.modules["aq_normalize"] = mod
                print(f"[runtime-hook] aq_normalize loaded from {path} with encoding={enc}")
                return True, f"loaded:{enc}"
            else:
                # fallback: exec in a new module namespace
                mod = type(sys)("aq_normalize")
                exec(codeobj, mod.__dict__)
                sys.modules["aq_normalize"] = mod
                print(f"[runtime-hook] aq_normalize exec-loaded from {path} with encoding={enc}")
                return True, f"exec-loaded:{enc}"
        except Exception as e:
            print(f"[runtime-hook] aq_normalize exec failed encoding={enc}: {e}")
            print(traceback.format_exc())
            continue
    return False, "no-encoding-worked"

def _add_native_dirs(base):
    try:
        if not base:
            return
        cand = [
            os.path.join(base, "MeCab", "bin"),
            os.path.join(base, "ffmpeg"),
            os.path.join(base, "aquestalk"),
            os.path.join(base, "_internal", "MeCab", "bin"),
            os.path.join(base, "_internal", "ffmpeg"),
            os.path.join(base, "_internal", "aquestalk"),
        ]
        # also add any subfolder inside aquestalk
        aqroot = os.path.join(base, "aquestalk")
        if os.path.isdir(aqroot):
            for entry in os.listdir(aqroot):
                p = os.path.join(aqroot, entry)
                if os.path.isdir(p):
                    cand.append(p)
        seen = set()
        for d in cand:
            nd = os.path.normpath(d)
            if not nd or nd in seen:
                continue
            seen.add(nd)
            if os.path.isdir(nd):
                try:
                    if hasattr(os, "add_dll_directory"):
                        os.add_dll_directory(nd)
                except Exception:
                    pass
                p = os.environ.get("PATH", "")
                if nd not in p:
                    os.environ["PATH"] = nd + os.pathsep + p
    except Exception:
        pass

def _try_locations():
    # 1) exe dir (where sys.argv[0] points to)
    try:
        exe = sys.argv[0] if sys.argv and sys.argv[0] else None
        if exe:
            exe_dir = os.path.dirname(os.path.abspath(exe))
            cand = os.path.join(exe_dir, "aq_normalize.py")
            if os.path.isfile(cand):
                ok, info = _try_load_path(cand)
                if ok:
                    _add_native_dirs(exe_dir)
                    return True, f"exe-dir:{info}"
                else:
                    print(f"[runtime-hook] aq_normalize load from exe-dir failed: {info}")
    except Exception as e:
        print("[runtime-hook] exe-dir check error:", e)

    # 2) cwd
    try:
        cand = os.path.join(os.getcwd(), "aq_normalize.py")
        if os.path.isfile(cand):
            ok, info = _try_load_path(cand)
            if ok:
                _add_native_dirs(os.getcwd())
                return True, f"cwd:{info}"
            else:
                print(f"[runtime-hook] aq_normalize load from cwd failed: {info}")
    except Exception as e:
        print("[runtime-hook] cwd check error:", e)

    # 3) PyInstaller _MEIPASS extracted dir
    try:
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                cand = os.path.join(meipass, "aq_normalize.py")
                if os.path.isfile(cand):
                    ok, info = _try_load_path(cand)
                    if ok:
                        _add_native_dirs(meipass)
                        return True, f"_meipass:{info}"
                    else:
                        print(f"[runtime-hook] aq_normalize load from _MEIPASS failed: {info}")
    except Exception as e:
        print("[runtime-hook] _MEIPASS check error:", e)

    return False, "not-found"

# Run early
try:
    loaded, info = _try_locations()
    if loaded:
        print(f"[runtime-hook] aq_normalize present: {info}")
    else:
        print(f"[runtime-hook] aq_normalize not loaded: {info}")
except Exception:
    print("[runtime-hook] unexpected error:", traceback.format_exc())