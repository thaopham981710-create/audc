def normalize_for_aquestalk(text: str, to_hiragana: bool = False) -> str:
    """
    Normalize katakana/hiragana text to reduce 'undefined symbol (105)' errors.
    - Replace characters known to cause issues in some AquesTalk voices.
    - Optionally convert katakana -> hiragana (to_hiragana=True).
    """
    import re, unicodedata
    try:
        import jaconv
    except Exception:
        jaconv = None

    if not text:
        return text

    # Unicode normalize
    s = unicodedata.normalize("NFKC", text)

    # common mapping that fixes many AquesTalk voice issues
    mapping = {
        "ヂ": "ジ",
        "ヅ": "ズ",
        "ヴ": "ブ",
        "ゔ": "ぶ",
        "・": "、",
        "〜": "ー",
        "‐": "ー",
    }
    for k, v in mapping.items():
        s = s.replace(k, v)

    # remove invisible/control chars
    s = re.sub(r'[\u0000-\u001F\u007F-\u009F]', '', s)

    # remove any ascii letters (A-Z, a-z) that might remain, or map them if you prefer
    s = re.sub(r'[A-Za-z]', '', s)

    # collapse multiple spaces
    s = re.sub(r'\s+', ' ', s).strip()

    # optionally convert katakana -> hiragana (some voices expect hiragana)
    if to_hiragana and jaconv:
        s = jaconv.kata2hira(s)

    return s