#!/usr/bin/env python3
# test_mecab.py
# Kiểm tra MeCab + mecab-python3 trong Python
# Chạy: "C:\...\Python313-32\python.exe" test_mecab.py

import sys

try:
    import MeCab
except Exception as e:
    print("MeCab binding IMPORT ERROR:", e)
    sys.exit(1)

s = "なあ霊夢、ルークスが受注開始からわずか1か月で1万台超えって話、もう聞いたか？"
print("Input:", s)
tagger = MeCab.Tagger()
# In toàn bộ parse (morph + features)
print("MeCab parse output:")
print(tagger.parse(s))

# Lấy reading (pronunciation) từng node
node = tagger.parseToNode(s)
readings = []
while node:
    if node.surface:
        feat = node.feature or ''
        cols = feat.split(',')
        pron = None
        if len(cols) > 7 and cols[7] != '*':
            pron = cols[7]
        elif len(cols) > 6 and cols[6] != '*':
            pron = cols[6]
        else:
            pron = node.surface
        readings.append(pron)
    node = node.next

katakana = ''.join(readings)
print("Katakana reading:", katakana)
# Nếu muốn hiragana:
try:
    import jaconv
    print("Hiragana:", jaconv.kata2hira(katakana))
except Exception:
    pass