"""Writes Hindi (Devanagari) and Punjabi (Gurmukhi) text in Roman letters the way
people type Hinglish, and converts between the two Indian scripts."""

from __future__ import annotations

import re
import unicodedata

CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh",
        "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh",
        "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "ळ": "l",
        "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h", "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z",
        "ड़": "r", "ढ़": "rh", "फ़": "f", "य़": "y"}
VOWELS = {"अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri", "ए": "e", "ऐ": "ai",
          "ओ": "o", "औ": "au", "ऍ": "e", "ऑ": "o"}
MATRAS = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
          "ॅ": "e", "ॉ": "o"}
NUKTA = {"क": "क़", "ख": "ख़", "ग": "ग़", "ज": "ज़", "ड": "ड़", "ढ": "ढ़", "फ": "फ़", "य": "य़", "स": "श", "ल": "ळ"}
COMMON = {"में": "mein", "नहीं": "nahi", "नही": "nahi", "यह": "yeh", "वह": "woh", "है": "hai", "हैं": "hain"}
VIRAMA, ANUSVARA, CANDRA, VISARGA, NUK = "्", "ं", "ँ", "ः", "़"
DEVA = re.compile(r"[ऀ-ॿ]")
GURU = re.compile(r"[਀-੿]")
LATIN = re.compile(r"[A-Za-z]")
ARABIC = re.compile(r"[\u0600-\u06FF]")
AR_CONS = {"ب": "ब", "پ": "प", "ت": "त", "ٹ": "ट", "ث": "स", "ج": "ज", "چ": "च", "ح": "ह", "خ": "ख़", "د": "द",
           "ڈ": "ड", "ذ": "ज़", "ر": "र", "ڑ": "ड़", "ز": "ज़", "ژ": "झ", "س": "स", "ش": "श", "ص": "स", "ض": "ज़",
           "ط": "त", "ظ": "ज़", "غ": "ग़", "ف": "फ़", "ق": "क़", "ک": "क", "ك": "क", "گ": "ग", "ل": "ल", "م": "म",
           "ن": "न", "ہ": "ह", "ه": "ह", "ة": "ह"}
AR_ASPIRATE = {"ब": "भ", "प": "फ", "त": "थ", "ट": "ठ", "ज": "झ", "च": "छ", "द": "ध", "ड": "ढ", "ड़": "ढ़",
               "क": "ख", "ग": "घ", "र": "र्ह", "ल": "ल्ह", "म": "म्ह", "न": "न्ह"}


def guru_to_deva(text: str) -> str:
    """Gurmukhi → Devanagari (the two scripts line up letter for letter, except a few signs)."""
    out = []
    chars = list(text)
    i = 0
    bearers = {("ੲ", "ਿ"): "इ", ("ੲ", "ੀ"): "ई", ("ੲ", "ੇ"): "ए", ("ੲ", "ੈ"): "ऐ",
               ("ੳ", "ੁ"): "उ", ("ੳ", "ੂ"): "ऊ", ("ੳ", "ੋ"): "ओ"}
    while i < len(chars):
        c = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if (c, nxt) in bearers:
            out.append(bearers[(c, nxt)])
            i += 2
            continue
        if c == "ੱ":  # addak: doubles the next consonant
            if nxt and 0x0A15 <= ord(nxt) <= 0x0A39:
                out.append(chr(ord(nxt) - 0x100) + VIRAMA)
            i += 1
            continue
        if c == "ੰ":
            out.append(ANUSVARA)
        elif c in "ੲੳ":
            out.append("इ" if c == "ੲ" else "उ")
        elif c == "ੵ":
            out.append("्य")
        elif 0x0A00 <= ord(c) <= 0x0A6F:
            out.append(chr(ord(c) - 0x100))
        else:
            out.append(c)
        i += 1
    return "".join(out)


AR_WORDS = {"میں": "में", "ہے": "है", "ہیں": "हैं", "یہ": "यह", "وہ": "वह", "نہیں": "नहीं", "کیا": "क्या",
            "ہوں": "हूँ", "میرا": "मेरा", "میری": "मेरी", "تھا": "था", "تھی": "थी", "اور": "और", "کے": "के",
            "کی": "की", "کا": "का", "سے": "से", "کو": "को", "پر": "पर", "بہت": "बहुत", "ہم": "हम", "آپ": "आप"}


def arabic_to_deva(text: str) -> str:
    text = re.sub(r"[\u0600-\u06FF]+", lambda m: AR_WORDS.get(m.group(0), m.group(0)), text)
    """Urdu / Shahmukhi letters → Devanagari. The speech model sometimes writes spoken Hindi or Punjabi
    in Urdu script (they sound the same); this brings it back to an Indian script."""
    out: list[str] = []
    prev_cons = False
    for i, c in enumerate(text):
        nxt = text[i + 1] if i + 1 < len(text) else " "
        at_start = not prev_cons and (not out or not DEVA.match(out[-1][-1:]))
        if c == "ھ" and out and out[-1] in AR_ASPIRATE:
            out[-1] = AR_ASPIRATE[out[-1]]
            continue
        if c in AR_CONS:
            if c in "ہه" and prev_cons and not ARABIC.match(nxt):
                out.append("ा")  # word-final h after a consonant is an 'a' sound: یہ → ये
                prev_cons = False
                continue
            out.append(AR_CONS[c])
            prev_cons = True
        elif c in "اآ":
            out.append("आ" if (c == "آ" or at_start) else "ा")
            prev_cons = False
        elif c == "و":
            out.append("व" if at_start else "ो" if prev_cons else "ओ")
            prev_cons = False
        elif c in "یيى":
            out.append("य" if at_start or not ARABIC.match(nxt) and not prev_cons else "ी")
            prev_cons = False
        elif c == "ے":
            out.append("े" if prev_cons else "ए")
            prev_cons = False
        elif c == "ں":
            out.append("ं")
        elif c in "ئءعٔ":
            if at_start:
                out.append("अ")
            prev_cons = False
        elif ARABIC.match(c):  # short-vowel marks and other signs
            continue
        else:
            out.append(c)
            prev_cons = False
    return re.sub("\u093c+", "\u093c", unicodedata.normalize("NFD", "".join(out)))


def to_script(text: str, target: str) -> str:
    """Rewrites Indian-language text in the target script: "hi" Devanagari or "pa" Gurmukhi."""
    if ARABIC.search(text):
        text = arabic_to_deva(text)
    if target == "pa":
        return deva_to_guru(text) if DEVA.search(text) else text
    return guru_to_deva(text) if GURU.search(text) else text


def deva_to_guru(text: str) -> str:
    out = []
    for c in text:
        if c == ANUSVARA:
            out.append("ੰ")
        elif 0x0900 <= ord(c) <= 0x096F and c not in "ऋृॅॉऍऑ":
            g = chr(ord(c) + 0x100)
            out.append(g)
        elif c in "ॅ":
            out.append("ੇ")
        elif c in "ॉ":
            out.append("ੋ")
        elif c == "ृ":
            out.append("੍ਰਿ")
        else:
            out.append(c)
    g = re.sub("\u0A4D(?=[\u0A15-\u0A27\u0A2A-\u0A2E\u0A32-\u0A38])", "", "".join(out))  # no half letters
    return re.sub("([\u0A3E\u0A40\u0A47\u0A48\u0A4B\u0A4C])\u0A70", "\\1\u0A02", g)  # bindi after vowel


def _syllables(word: str) -> list[list]:
    """[consonant(s) or vowel, vowel sound, nasal] per syllable; vowel None = the built-in short 'a'."""
    syl: list[list] = []
    chars = list(word)
    i = 0
    while i < len(chars):
        c = chars[i]
        if i + 1 < len(chars) and chars[i + 1] == NUK and c in NUKTA:
            c = NUKTA[c]
            i += 1
        if c in CONS:
            if syl and syl[-1][1] == VIRAMA:
                syl[-1][0] += CONS[c]
                syl[-1][1] = None
            else:
                syl.append([CONS[c], None, ""])
        elif c in VOWELS:
            syl.append(["", VOWELS[c], ""])
        elif c in MATRAS and syl:
            syl[-1][1] = MATRAS[c]
        elif c == VIRAMA and syl:
            syl[-1][1] = VIRAMA
        elif c in (ANUSVARA, CANDRA) and syl:
            syl[-1][2] = "n"
        elif c == VISARGA and syl:
            syl[-1][2] = "h"
        elif c.isdigit():
            syl.append([str(int(c)), "", ""])
        i += 1
    return syl


def _roman_word(word: str) -> str:
    if word in COMMON:
        return COMMON[word]
    syl = _syllables(word)
    if not syl:
        return word
    keep = [True] * len(syl)
    last = len(syl) - 1
    if syl[last][1] is None and syl[last][0] and not syl[last][2] and len(syl) > 1:
        keep[last] = False  # final short 'a' isn't spoken: "dost", not "dosta"
    for i in range(last - 1, 0, -1):  # medial: dekhana → dekhna
        prev_has_vowel = syl[i - 1][1] not in (VIRAMA,) and (syl[i - 1][1] is not None or keep[i - 1])
        if syl[i][1] is None and not syl[i][2] and prev_has_vowel and syl[i + 1][1] not in (VIRAMA,) \
                and (syl[i + 1][1] is not None or keep[i + 1]):
            keep[i] = False
    out = []
    for (cons, vowel, nasal), k in zip(syl, keep):
        v = "a" if vowel is None and k else "" if vowel in (None, VIRAMA) else vowel
        out.append(cons + v + nasal)
    return re.sub(r"chch", "ch", "".join(out))


def romanize(text: str) -> str:
    """Hindi or Punjabi text in Roman (English) letters: "यह मेरा दिन है" → "yah mera din hai"."""
    if ARABIC.search(text):
        text = arabic_to_deva(text)
    if GURU.search(text):
        text = guru_to_deva(text)
    text = unicodedata.normalize("NFD", text)
    if not DEVA.search(text):
        return text
    text = text.replace("।", ".").replace("॥", ".")
    return re.sub(r"[ऀ-ॣ०-ॿ]+", lambda m: _roman_word(m.group(0)), text)


AR_LATIN = {"ا": "a", "آ": "a", "ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s", "ج": "j", "چ": "ch", "ح": "h",
            "خ": "kh", "د": "d", "ڈ": "d", "ذ": "z", "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh",
            "ص": "s", "ض": "z", "ط": "t", "ظ": "z", "ع": "a", "غ": "g", "ف": "f", "ق": "q", "ک": "k", "ك": "k",
            "گ": "g", "ل": "l", "م": "m", "ن": "n", "ں": "n", "و": "o", "ہ": "h", "ه": "h", "ھ": "h", "ی": "i",
            "ي": "i", "ى": "i", "ے": "e", "ئ": "i", "ء": ""}


def _latin_letters(run: str) -> str:
    """Letters of another script inside an English word, one Roman letter each (no added vowels)."""
    out = []
    for c in unicodedata.normalize("NFD", guru_to_deva(run)):
        if c in AR_LATIN:
            out.append(AR_LATIN[c])
        elif c in CONS:
            out.append(CONS[c])
        elif c in VOWELS:
            out.append(VOWELS[c])
        elif c in MATRAS:
            out.append(MATRAS[c])
        elif c in (ANUSVARA, CANDRA):
            out.append("n")
    return "".join(out)


def tidy(text: str) -> str:
    """The speech model can slip letters of another script into a line ("اsك not" for "ask not").
    Puts every letter of the line into the line's main script."""
    latin, deva, guru, arab = (len(r.findall(text)) for r in (LATIN, DEVA, GURU, ARABIC))
    if not (arab or (latin and (deva or guru))):
        return text
    if latin >= deva + guru + arab:
        return re.sub(r"[\u0600-\u06FF\u0900-\u097F\u0A00-\u0A7F]+", lambda m: _latin_letters(m.group(0)), text)
    if not arab:
        return text
    return to_script(text, "pa" if guru > deva else "hi")


def script_of(text: str) -> str:
    counts = {"hi": len(DEVA.findall(text)), "pa": len(GURU.findall(text)), "en": len(LATIN.findall(text)),
              "ur": len(ARABIC.findall(text))}
    return max(counts, key=counts.get) if any(counts.values()) else "en"
