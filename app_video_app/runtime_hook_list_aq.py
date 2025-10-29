# runtime_hook_list_aq.py
# Debug runtime hook to log what's extracted under sys._MEIPASS for aquestalk.
# This is harmless and helps confirm DLLs are present in the onefile extracted folder.
import sys, os, traceback

try:
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', None)
        if base:
            aq = os.path.join(base, 'aquestalk')
            log_path = os.path.join(base, 'meipass_aquestalk_list.txt')
            try:
                with open(log_path, 'w', encoding='utf-8') as f:
                    f.write(f"_MEIPASS: {base}\n")
                    if os.path.isdir(aq):
                        f.write("aquestalk tree:\n")
                        for root, dirs, files in os.walk(aq):
                            rel = os.path.relpath(root, base)
                            f.write(f"DIR: {rel}\n")
                            for fn in files:
                                f.write(f"  {fn}\n")
                    else:
                        f.write("aquestalk not found under _MEIPASS\n")
            except Exception as e:
                try:
                    # fallback: print to stderr (visible if console)
                    sys.stderr.write("runtime_hook_list_aq write error: " + str(e) + "\n")
                except Exception:
                    pass
except Exception:
    try:
        sys.stderr.write("runtime_hook_list_aq unexpected error:\\n" + traceback.format_exc() + "\\n")
    except Exception:
        pass