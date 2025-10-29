# Quick diagnostic for AquesTalk in project folder.
# Usage (run with Python 32-bit):
# "C:\...\python.exe" check_aquestalk.py

import os
import sys
import time
import threading

base = os.path.dirname(os.path.abspath(__file__))
# path where you said the folder is:
candidate_dir = os.path.join(base, "aquestalk", "aquestalk")
if not os.path.isdir(candidate_dir):
    candidate_dir = os.path.join(base, "aquestalk")  # fallback
print("Candidate aquestalk dir:", candidate_dir)
if os.path.isdir(candidate_dir):
    print("Files in aquestalk dir:")
    for fn in sorted(os.listdir(candidate_dir)):
        print("  ", fn)
else:
    print("No aquestalk directory found at expected locations.")

# add to DLL search path (Windows)
if os.name == "nt" and os.path.isdir(candidate_dir):
    try:
        os.add_dll_directory(candidate_dir)
        print("Added to DLL search path via os.add_dll_directory()")
    except Exception as e:
        print("os.add_dll_directory failed:", e)
        os.environ["PATH"] = candidate_dir + os.pathsep + os.environ.get("PATH", "")
        print("Prepended to PATH env var.")

# Try import and load voices
try:
    import aquestalk
    print("Imported aquestalk module OK.")
except Exception as e:
    print("Failed to import aquestalk:", repr(e))
    sys.exit(1)

def try_load_voice(name, timeout_s=3):
    try:
        aq = aquestalk.load(name)
    except Exception as e:
        return False, f"load failed: {e}"
    # try quick synth on separate thread with timeout
    ok = [False]
    err = [None]
    def worker():
        try:
            # some wrappers use synthe or synthe_raw; try synthe first
            try:
                r = aq.synthe("こんにちは")
                ok[0] = True
            except Exception:
                try:
                    _ = aq.synthe_raw("こんにちは")
                    ok[0] = True
                except Exception as ee:
                    err[0] = f"synth failed: {ee}"
        except Exception as e:
            err[0] = repr(e)
    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return False, "synth timeout"
    if ok[0]:
        return True, "ok"
    return False, err[0] or "unknown"

for i in range(1, 21):
    name = f"f{i}"
    print(f"Probing voice {name} ...", end=" ", flush=True)
    ok, info = try_load_voice(name)
    print("=>", ok, info)
    if ok:
        # stop after first good voice
        break
print("Diagnostic finished.")