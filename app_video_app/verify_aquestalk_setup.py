#!/usr/bin/env python3
"""
Verify AquesTalk installation and directory structure.
Run this script to check if AquesTalk voices are properly set up.
"""
import os
import sys

def verify_aquestalk_setup():
    """Verify the AquesTalk directory structure and files."""
    print("=" * 60)
    print("AquesTalk Setup Verification")
    print("=" * 60)
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    aquestalk_dir = os.path.join(base_dir, "aquestalk")
    voices_dir = os.path.join(aquestalk_dir, "aquestalk")
    
    issues = []
    
    # Check main directory
    print(f"\n1. Checking main directory: {aquestalk_dir}")
    if not os.path.isdir(aquestalk_dir):
        issues.append("❌ Main aquestalk directory not found!")
        print(f"   ❌ Directory does not exist")
    else:
        print(f"   ✓ Directory exists")
    
    # Check voices directory
    print(f"\n2. Checking voices directory: {voices_dir}")
    if not os.path.isdir(voices_dir):
        issues.append("❌ Voices directory (aquestalk/aquestalk) not found!")
        print(f"   ❌ Directory does not exist")
    else:
        print(f"   ✓ Directory exists")
        
        # List voices
        voices = []
        for entry in sorted(os.listdir(voices_dir)):
            entry_path = os.path.join(voices_dir, entry)
            if os.path.isdir(entry_path):
                voices.append(entry)
        
        print(f"\n3. Available voices ({len(voices)} found):")
        if voices:
            for voice in voices:
                voice_path = os.path.join(voices_dir, voice)
                dll_path = os.path.join(voice_path, "AquesTalk.dll")
                if os.path.isfile(dll_path):
                    print(f"   ✓ {voice} (DLL found)")
                else:
                    print(f"   ⚠ {voice} (DLL missing)")
                    issues.append(f"⚠ Voice '{voice}' is missing AquesTalk.dll")
        else:
            print("   ❌ No voices found!")
            issues.append("❌ No voice directories found in aquestalk/aquestalk/")
    
    # Check support DLLs
    print(f"\n4. Checking support DLL files:")
    required_dlls = ["AqKanji2Koe.dll", "AqResample.dll", "AqUsrDic.dll"]
    for dll in required_dlls:
        dll_path = os.path.join(aquestalk_dir, dll)
        if os.path.isfile(dll_path):
            print(f"   ✓ {dll}")
        else:
            print(f"   ❌ {dll} (missing)")
            issues.append(f"❌ Support DLL missing: {dll}")
    
    # Check dictionary
    print(f"\n5. Checking dictionary files:")
    dict_dir = os.path.join(aquestalk_dir, "aq_dic")
    if os.path.isdir(dict_dir):
        print(f"   ✓ Dictionary directory exists")
        dict_bin = os.path.join(dict_dir, "aqdic.bin")
        if os.path.isfile(dict_bin):
            print(f"   ✓ Dictionary binary (aqdic.bin) found")
        else:
            print(f"   ⚠ Dictionary binary (aqdic.bin) missing")
            issues.append("⚠ Dictionary binary missing (may cause issues)")
    else:
        print(f"   ⚠ Dictionary directory missing")
        issues.append("⚠ Dictionary directory missing")
    
    # Summary
    print("\n" + "=" * 60)
    if not issues:
        print("✓ All checks passed! AquesTalk is properly set up.")
        print("\nThe following voices are available:")
        if 'voices' in locals():
            for voice in voices:
                print(f"  - {voice}")
        return 0
    else:
        print("❌ Some issues were found:")
        for issue in issues:
            print(f"  {issue}")
        print("\nPlease fix the issues above before using AquesTalk.")
        return 1

if __name__ == "__main__":
    sys.exit(verify_aquestalk_setup())
