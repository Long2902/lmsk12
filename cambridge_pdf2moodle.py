
#!/usr/bin/env python3
"""
cambridge_pdf2moodle.py

Convert Cambridge-style IELTS scanned PDFs (image-only) into Moodle Question Bank XML
using Cloze (Embedded Answers) questions with a split-screen layout:
- Left: passage pages (as images)
- Right: question pages (as images) + answer inputs (Cloze fields)

Designed for self-hosted Moodle and personal practice.

Key idea:
- We OCR only small header regions to detect sections and question group instructions.
- We OCR answer-key pages to get correct answers, so the generated questions are auto-graded.
- We embed rendered page images into the Moodle XML (@@PLUGINFILE@@ references).

Usage (example):
    python cambridge_pdf2moodle.py \
        --pdf "Cambridge 19.pdf" \
        --tests 1 \
        --out out_cambridge19 \
        --mode reading

Then in Moodle:
    Course → Question bank → Import → Moodle XML → upload out_cambridge19/moodle_reading.xml

Dependencies:
    pip install pymupdf pillow pytesseract

System requirements:
    - Tesseract OCR must be installed and available on PATH.
      Linux: sudo apt-get install tesseract-ocr
      macOS: brew install tesseract
      Windows: install Tesseract, then set TESSERACT_CMD env var or edit config.

This script does NOT ship any copyrighted content. It only processes PDFs you own.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import dataclasses
import itertools
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import fitz  # PyMuPDF
from PIL import Image, ImageOps
import pytesseract
import requests


# -----------------------------
# Utilities
# -----------------------------

def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

_XML10_INVALID_RE = re.compile("[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]")

def strip_invalid_xml_chars(value: str) -> str:
    return _XML10_INVALID_RE.sub("", str(value or ""))

def _escape_cloze_answer(s: str) -> str:
    """
    Escape special chars for Cloze answers.
    MoodleDocs mentions: } # ~ / " and backslash (\\) may require escaping.
    We'll escape: \, ~, }, #, /
    For quotes, use &quot; to be safe.
    """
    s = s.replace("\\", "\\\\")
    s = s.replace("~", "\\~")
    s = s.replace("}", "\\}")
    s = s.replace("#", "\\#")
    s = s.replace("/", "\\/")
    s = s.replace('"', "&quot;")
    return s

def _escape_cloze_feedback(s: str) -> str:
    """Escape feedback text embedded inside a Cloze option, e.g. =A#feedback."""
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\n", "<br>")
    s = s.replace("\\", "\\\\")
    s = s.replace("~", "\\~")
    s = s.replace("}", "\\}")
    s = s.replace("#", "\\#")
    s = s.replace("/", "\\/")
    s = s.replace('"', "&quot;")
    return s



# Common OCR cleanup for answer key tokens
_TRAIL_PUNCT_RE = re.compile(r"[\.。,;:]+$")

# Collect fallback warnings when OCR answer does not match options.
_FALLBACK_LOG: list[str] = []

def _clean_key_answer(ans: str) -> str:
    """
    Clean common OCR artifacts in answer key strings, without being too destructive.
    - Collapse whitespace
    - Remove trailing punctuation like '.' ',' ';' ':'
    - Strip surrounding brackets
    """
    ans = (ans or "").strip()
    ans = ans.replace("—", "-").replace("–", "-")
    ans = re.sub(r"\s+", " ", ans).strip()
    # strip surrounding brackets
    ans = ans.strip("()[]{}")
    # remove trailing punctuation (OCR often adds a dot)
    ans = _TRAIL_PUNCT_RE.sub("", ans).strip()
    return ans

def _maybe_fix_letter_token(s: str) -> str:
    """
    Try to coerce a noisy OCR token into a single A-Z letter.
    Useful for Cambridge answer keys where answers are letters.
    """
    s = _clean_key_answer(s).upper()
    # keep only first alnum
    s2 = re.sub(r"[^A-Z0-9]", "", s)
    if not s2:
        return ""
    ch = s2[0]
    digit_map = {
        "8": "B",
        "0": "D",  # common confusion in scans; adjust if needed
        "5": "S",
        "2": "Z",
        "1": "I",
    }
    ch = digit_map.get(ch, ch)
    if re.match(r"^[A-Z]$", ch):
        return ch
    return ""

def _letters_range(a: str, b: str) -> List[str]:
    a = a.strip().upper()
    b = b.strip().upper()
    if len(a) != 1 or len(b) != 1:
        raise ValueError(f"Invalid letter endpoints: {a}-{b}")
    start = ord(a)
    end = ord(b)
    if start > end:
        start, end = end, start
    return [chr(c) for c in range(start, end + 1)]

def _all_2_letter_combos(letters: List[str]) -> List[str]:
    # Return "AB", "AC", ... sorted.
    combos = ["".join(c) for c in itertools.combinations(letters, 2)]
    return combos

def _img_to_jpeg_bytes(img: Image.Image, quality: int = 80) -> bytes:
    import io
    buf = io.BytesIO()
    # Convert to RGB for JPEG
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()

def _render_page(doc: fitz.Document, pno: int, zoom: float = 2.0, clip: Optional[fitz.Rect] = None) -> Image.Image:
    page = doc[pno]
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
    import io
    return Image.open(io.BytesIO(pix.tobytes("png")))

def _ocr_image(img: Image.Image, lang: str = "eng", psm: int = 6) -> str:
    # Basic OCR: grayscale + slightly higher contrast can help.
    gray = ImageOps.grayscale(img)
    # psm 6: assume a single uniform block of text.
    config = f"--psm {psm}"
    return pytesseract.image_to_string(gray, lang=lang, config=config)

def _ocr_page_region(
    doc: fitz.Document,
    pno: int,
    region: Tuple[float, float, float, float],
    zoom: float = 2.0,
    lang: str = "eng",
    psm: int = 6,
) -> str:
    """
    OCR a region of page defined as (x0,y0,x1,y1) fractions of page size.
    """
    page = doc[pno]
    w, h = page.rect.width, page.rect.height
    x0f, y0f, x1f, y1f = region
    clip = fitz.Rect(x0f*w, y0f*h, x1f*w, y1f*h)
    img = _render_page(doc, pno, zoom=zoom, clip=clip)
    return _ocr_image(img, lang=lang, psm=psm)

def _guess_test_num(text: str) -> Optional[int]:
    m = re.search(r"\bTest\s+([1-9]\d?)\b", text, flags=re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None

def _has_phrase(text: str, phrase: str) -> bool:
    return phrase.lower() in text.lower()

def _find_question_headers(ocr_text: str) -> List[Tuple[str, int, Optional[int]]]:
    """
    Parse all 'Questions ...' headers found in OCR text.

    Returns list of tuples: (kind, start, end_or_none)
    kind: 'range' or 'pair'
    For 'range': start, end
    For 'pair': start, end=None where start is first number, and we return second separately in parsing below.
    We'll actually encode pair as start and second in end.
    """
    headers: List[Tuple[str, int, Optional[int]]] = []
    lines = [l.strip() for l in ocr_text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if re.match(r"^Questions\b", line, flags=re.IGNORECASE):
            # Range: Questions 14-19
            m = re.search(r"Questions\s+(\d+)\s*[\-–—]\s*(\d+)", line, flags=re.IGNORECASE)
            if m:
                headers.append(("range", int(m.group(1)), int(m.group(2))))
                continue
            # Pair: Questions 20 and 21
            m = re.search(r"Questions\s+(\d+)\s+and\s+(\d+)", line, flags=re.IGNORECASE)
            if m:
                headers.append(("pair", int(m.group(1)), int(m.group(2))))
                continue
            # Single: Questions 8-13 might appear, covered above.
    return headers

def _detect_group_type(block_text: str) -> Tuple[str, Dict[str, str]]:
    """
    Determine group type from instruction block text.
    Returns (group_type, meta).

    group_type:
      - tfng
      - yesno
      - choose_one_word
      - letter_dropdown (A-G, A-J, etc)
      - mc_letters (A,B,C,D...)
      - choose_two_letters (A-E etc)
      - unknown

    meta may include:
      - letters: e.g. "A-E" or explicit list "A,B,C"
    """
    t = block_text.lower()

    # TFNG / YES-NO-NG
    if "not given" in t and "true" in t and "false" in t:
        return "tfng", {}
    if "not given" in t and "yes" in t and "no" in t:
        return "yesno", {}

    # Choose one word
    if "one word only" in t:
        return "choose_one_word", {}

    # Choose TWO letters
    if "choose two letters" in t or "choose two" in t and "letters" in t:
        # Try find A-E pattern
        m = re.search(r"([A-Z])\s*[\-–—]\s*([A-Z])", block_text)
        if m:
            return "choose_two_letters", {"letters": f"{m.group(1).upper()}-{m.group(2).upper()}"}
        # Or list like A, B, C
        m2 = re.search(r"letters?\s*,?\s*([A-Z](?:\s*,\s*[A-Z])+(?:\s*or\s*[A-Z])?)", block_text)
        if m2:
            return "choose_two_letters", {"letters": m2.group(1)}
        return "choose_two_letters", {}

    # Write the correct letter, A-G
    if "write the correct letter" in t:
        m = re.search(r"([A-Z])\s*[\-–—]\s*([A-Z])", block_text)
        if m:
            return "letter_dropdown", {"letters": f"{m.group(1).upper()}-{m.group(2).upper()}"}
        # fallback
        return "letter_dropdown", {}

    # Choose the correct letter, A,B,C or D
    if "choose the correct letter" in t:
        # Try "A, B or C"
        m = re.search(r"letter,\s*([A-Z])\s*,\s*([A-Z])\s*(?:,\s*([A-Z])\s*)?(?:,\s*([A-Z])\s*)?(?:or\s*([A-Z]))?", block_text)
        if m:
            letters = [g for g in m.groups() if g]
            letters = [x.upper() for x in letters]
            # In some OCR, "or D" might be in next token; this is best effort.
            return "mc_letters", {"letters_list": ",".join(letters)}
        # Or A–D pattern
        m2 = re.search(r"([A-Z])\s*[\-–—]\s*([A-Z])", block_text)
        if m2:
            return "mc_letters", {"letters": f"{m2.group(1).upper()}-{m2.group(2).upper()}"}
        return "mc_letters", {}

    # Complete the summary using the list of phrases, A–J
    if "list of" in t and "write the correct letter" in t:
        m = re.search(r"([A-Z])\s*[\-–—]\s*([A-Z])", block_text)
        if m:
            return "letter_dropdown", {"letters": f"{m.group(1).upper()}-{m.group(2).upper()}"}

    return "unknown", {}

# -----------------------------
# Data models
# -----------------------------

@dataclass
class PageScan:
    pno: int
    header_text: str
    test_num: Optional[int]
    has_reading_passage: Optional[int]  # 1,2,3 if found
    has_writing_task1: bool
    has_answer_keys: bool

@dataclass
class QuestionGroup:
    # Either range or pair
    qnums: List[int]  # For choose_two, this will include both numbers.
    group_type: str
    meta: Dict[str, str]
    raw_block: str

@dataclass
class PassageBundle:
    test_num: int
    passage_num: int
    qrange: Tuple[int, int]
    passage_pages: List[int]
    question_pages: List[int]
    groups: List[QuestionGroup]


def bundle_id(bundle: PassageBundle) -> str:
    return f"t{bundle.test_num}_p{bundle.passage_num}_q{bundle.qrange[0]}-{bundle.qrange[1]}"


def _format_answer_for_feedback(answer: Any) -> str:
    raw = strip_invalid_xml_chars(str(answer or "")).strip()
    if not raw:
        return ""
    parts = [p.strip() for p in re.split(r"\s*/\s*", raw) if p.strip()]
    if len(parts) >= 2 and all(re.fullmatch(r"[A-Z]", p) for p in parts):
        raw = " & ".join(parts)
    elif parts:
        raw = " / ".join(parts)
    return html.escape(raw)


def _feedback_text_from_item(item: Optional[Dict[str, Any]]) -> str:
    if not item:
        return ""
    ans = _format_answer_for_feedback(item.get("answer", ""))
    if not ans:
        return ""
    return ans


def _extract_text_from_pages(pdf_path: Path, pages: List[int], cache_dir: Path, prefix: str, lang: str = "eng") -> str:
    _ensure_dir(cache_dir / "text")
    cache_path = cache_dir / "text" / f"{prefix}.txt"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="ignore")
    doc = fitz.open(str(pdf_path))
    parts: List[str] = []
    try:
        for pno in pages:
            page = doc[pno]
            txt = (page.get_text("text") or "").strip()
            if len(txt) < 60:
                txt = _ocr_page_region(doc, pno, (0.0, 0.0, 1.0, 0.98), zoom=1.8, lang=lang, psm=6)
            parts.append(txt.strip())
    finally:
        doc.close()
    out = "\n\n".join([p for p in parts if p]).strip()
    cache_path.write_text(out, encoding="utf-8")
    return out


def build_answer_context(bundle: PassageBundle, singles: Dict[int, List[str]], pairs: List[Tuple[Tuple[int, int], List[str]]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    pair_map = {(min(a, b), max(a, b)): vals for (a, b), vals in pairs}
    for g in sorted(bundle.groups, key=lambda x: min(x.qnums) if x.qnums else 10**9):
        if g.group_type == "choose_two_letters" and len(g.qnums) == 2:
            a, b = sorted(g.qnums)
            vals = pair_map.get((a, b), [])
            answer = " / ".join([str(v).strip() for v in vals if str(v).strip()])
            items.append({"label": f"{a}-{b}", "qnums": [a, b], "group_type": g.group_type, "instruction": g.raw_block, "answer": answer})
        else:
            for q in sorted(g.qnums):
                vals = singles.get(q, []) or []
                answer = " / ".join([str(v).strip() for v in vals if str(v).strip()])
                items.append({"label": str(q), "qnums": [q], "group_type": g.group_type, "instruction": g.raw_block, "answer": answer})
    return items


def _extract_text_from_gemini_response(data: Dict[str, Any]) -> str:
    for cand in data.get("candidates", []):
        content = cand.get("content") or {}
        for part in content.get("parts", []):
            if "text" in part:
                return part["text"]
    return ""


def generate_explanations_gemini(passage_text: str, question_text: str, answer_items: List[Dict[str, Any]], api_key: str, model: str = "gemini-2.5-flash") -> Dict[str, Any]:
    if not api_key:
        raise RuntimeError("Thiếu GEMINI_API_KEY.")
    import json
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "answer": {"type": "string"},
                        "explanation_vi": {"type": "string"},
                        "explanation_en": {"type": "string"},
                        "evidence": {"type": ["string", "null"]},
                        "confidence": {"type": "number"}
                    },
                    "required": ["label", "answer", "explanation_vi", "explanation_en", "evidence", "confidence"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["items"],
        "additionalProperties": False
    }
    prompt = (
        "Bạn là trợ lý tạo lời giải IELTS Reading/Listening. Chỉ dùng source text, question và answer key được cung cấp. "
        "KHÔNG thay đổi đáp án. Hãy trả ra giải thích ngắn gọn cho từng label, bằng cả tiếng Việt và tiếng Anh, "
        "để dùng làm specific feedback trong Moodle Cloze.\n\n"
        f"SOURCE TEXT:\n{passage_text}\n\nQUESTION:\n{question_text}\n\nANSWER ITEMS JSON:\n{json.dumps(answer_items, ensure_ascii=False, indent=2)}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema},
    }
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    out_text = _extract_text_from_gemini_response(data)
    if not out_text:
        raise RuntimeError(f"Gemini không trả text hợp lệ: {data}")
    return json.loads(out_text)



_EASY_B1_KEYWORD_TOKENS = {
    "a", "an", "the", "and", "or", "but", "if", "because", "while", "before", "after",
    "people", "person", "thing", "things", "way", "time", "day", "year", "place", "area",
    "good", "bad", "big", "small", "high", "low", "new", "old", "young", "long", "short",
    "work", "study", "school", "home", "food", "water", "money", "family", "children", "child",
    "problem", "question", "answer", "idea", "result", "reason", "example", "change", "help", "important",
    "use", "make", "find", "show", "need", "look", "give", "take", "come", "go", "keep",
    "text", "passage", "section", "part", "page", "paragraph", "sentence", "word", "words",
    "reading", "listening", "table", "map", "plan", "diagram", "form", "note", "notes", "summary",
    "complete", "choose", "match", "write", "letter", "name", "number", "title", "list"
}

_SHORT_B2_OK_TOKENS = {
    "bias", "myth", "surge", "prone", "genre", "tense", "flare", "plight", "levy", "trait",
    "stark", "niche", "fraud", "gauge", "onus", "evoke", "curb", "merit", "novel", "scrap",
    "grave", "bleak", "wary", "drain", "spike", "strain", "yield", "sheer", "probe", "asset"
}


def _normalize_keyword_candidate(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()



def _keyword_occurs_in_source(keyword: str, source_text: str) -> bool:
    kw = _normalize_keyword_candidate(keyword)
    src = _normalize_keyword_candidate(source_text)
    return bool(kw) and bool(src) and f" {kw} " in f" {src} "



def _looks_too_basic_for_b2(keyword: str) -> bool:
    norm = _normalize_keyword_candidate(keyword)
    if not norm:
        return True
    tokens = [tok for tok in norm.split() if tok]
    if not tokens:
        return True
    if all(tok.isdigit() for tok in tokens):
        return True
    if len(tokens) == 1:
        tok = tokens[0]
        if tok in _EASY_B1_KEYWORD_TOKENS:
            return True
        if len(tok) <= 3:
            return True
        if len(tok) == 4 and tok not in _SHORT_B2_OK_TOKENS and tok in _EASY_B1_KEYWORD_TOKENS:
            return True
    if all(tok in _EASY_B1_KEYWORD_TOKENS for tok in tokens):
        return True
    return False



def _validate_keyword_items_b2(parsed: Dict[str, Any], source_text: str) -> Tuple[List[Dict[str, str]], List[str]]:
    items: List[Dict[str, str]] = []
    reasons: List[str] = []
    seen: set[str] = set()
    raw_items = parsed.get("items", []) or []
    if len(raw_items) != 5:
        reasons.append(f"Gemini trả {len(raw_items)} item thay vì đúng 5 item.")
    for idx, item in enumerate(raw_items, start=1):
        kw = strip_invalid_xml_chars(str(item.get("keyword", "")).strip())
        ph = strip_invalid_xml_chars(str(item.get("phonetic", "")).strip())
        vi = strip_invalid_xml_chars(str(item.get("meaning_vi", "")).strip())
        en = strip_invalid_xml_chars(str(item.get("meaning_en", "")).strip())
        cefr = strip_invalid_xml_chars(str(item.get("cefr_level", "")).strip().upper())
        kw_norm = _normalize_keyword_candidate(kw)
        if not kw_norm:
            reasons.append(f"Item {idx} thiếu keyword.")
            continue
        if kw_norm in seen:
            reasons.append(f"'{kw}' bị trùng lặp.")
            continue
        seen.add(kw_norm)
        if cefr not in {"B2", "C1"}:
            reasons.append(f"'{kw}' không được gắn mức B2/C1.")
            continue
        if not _keyword_occurs_in_source(kw, source_text):
            reasons.append(f"'{kw}' không xuất hiện nguyên văn trong source.")
            continue
        if _looks_too_basic_for_b2(kw):
            reasons.append(f"'{kw}' có vẻ quá cơ bản so với B2/IELTS 6.5+.")
            continue
        items.append({"keyword": kw, "phonetic": ph, "meaning_vi": vi, "meaning_en": en})
    return items, reasons



def _build_b2_keyword_prompt(source_text: str, question_text: str, rejected_notes: str = "") -> str:
    retry_note = ""
    if rejected_notes.strip():
        retry_note = (
            "\n\nCác item của lần sinh trước bị từ chối vì chưa đạt yêu cầu B2+/IELTS 6.5+: "
            f"{rejected_notes}. Hãy thay các item đó bằng từ/cụm từ khó hơn nhưng vẫn phải có trong source."
        )
    return (
        "Bạn là trợ lý chọn từ vựng học IELTS nâng cao. "
        "Hãy chọn đúng 5 lexical items xuất hiện NGUYÊN VĂN trong source text, phù hợp trình độ tiếng Anh bậc B2 theo Khung 6 bậc Việt Nam hoặc IELTS 6.5+. "
        "Mỗi item phải ở mức B2-C1, có giá trị thực sự để học và giúp hiểu passage/section tốt hơn. "
        "Ưu tiên từ/cụm từ học thuật hoặc bán học thuật, collocation, discourse word, động từ/danh từ/tính từ trừu tượng, hoặc expression mang tính học thuật. "
        "Loại bỏ tuyệt đối các mục quá dễ hoặc quá sơ cấp (A1-B1), ví dụ: people, good, bad, big, small, change, help, use, make, important, problem, question, answer. "
        "Không chọn proper nouns, số, nhãn đề, từ chức năng, hoặc từ quá hiển nhiên mà học sinh B1 đã nắm chắc. "
        "Nếu source không có đủ 5 từ đơn khó, hãy ưu tiên collocation/phrase 2-4 từ khó hơn thay vì chọn từ đơn dễ. "
        "Mỗi item phải có: keyword (giữ nguyên chính tả trong source), phonetic (IPA ngắn gọn nếu biết, không chắc thì để chuỗi rỗng), meaning_vi (rất ngắn), meaning_en (rất ngắn), cefr_level (chỉ được là B2 hoặc C1), difficulty_reason (rất ngắn, nêu vì sao item này là B2+). "
        "Không thêm ví dụ, không paraphrase keyword, không giải thích dài."
        f"{retry_note}\n\nSOURCE TEXT:\n{source_text}\n\nQUESTION CONTEXT:\n{question_text}"
    )






def generate_keywords_gemini(source_text: str, question_text: str, api_key: str, model: str = "gemini-2.5-flash") -> Dict[str, Any]:
    if not api_key:
        raise RuntimeError("Thiếu GEMINI_API_KEY.")
    import json
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 5,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string"},
                        "phonetic": {"type": "string"},
                        "meaning_vi": {"type": "string"},
                        "meaning_en": {"type": "string"},
                        "cefr_level": {"type": "string", "enum": ["B2", "C1"]},
                        "difficulty_reason": {"type": "string"}
                    },
                    "required": ["keyword", "phonetic", "meaning_vi", "meaning_en", "cefr_level", "difficulty_reason"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["items"],
        "additionalProperties": False
    }
    rejected_notes = ""
    last_reasons: List[str] = []
    for _attempt in range(3):
        prompt = _build_b2_keyword_prompt(source_text, question_text, rejected_notes)
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "temperature": 0.2,
            },
        }
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            data=json.dumps(body),
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        out_text = _extract_text_from_gemini_response(data)
        if not out_text:
            last_reasons = [f"Gemini không trả keyword hợp lệ: {data}"]
            rejected_notes = "; ".join(last_reasons[:6])
            continue
        parsed = json.loads(out_text)
        items, reasons = _validate_keyword_items_b2(parsed, source_text)
        if len(items) == 5 and not reasons:
            return {"items": items}
        last_reasons = reasons or [f"Chỉ giữ lại được {len(items)}/5 item hợp lệ."]
        rejected_notes = "; ".join(last_reasons[:6])
    raise RuntimeError(
        "Gemini chưa trả được đúng 5 từ vựng ở mức B2+/IELTS 6.5+ sau 3 lần thử. "
        + ("Lý do gần nhất: " + "; ".join(last_reasons[:8]) if last_reasons else "")
    )


def keywords_to_bar_html(keywords: Dict[str, Any]) -> str:
    parts = []
    for item in (keywords or {}).get("items", []):
        kw = html.escape(strip_invalid_xml_chars(str(item.get("keyword", "")).strip()))
        ph_raw = strip_invalid_xml_chars(str(item.get("phonetic", "")).strip())
        ph = html.escape(ph_raw)
        vi = html.escape(strip_invalid_xml_chars(str(item.get("meaning_vi", "")).strip()))
        en = html.escape(strip_invalid_xml_chars(str(item.get("meaning_en", "")).strip()))
        if not kw:
            continue
        title_attr = f' title="{en}"' if en else ""
        meaning_text = vi or en
        if ph and meaning_text:
            meta_html = f'<span class="cambridge-vocab-meaning"> {ph}: {meaning_text}</span>'
        elif ph:
            meta_html = f'<span class="cambridge-vocab-meaning"> {ph}</span>'
        elif meaning_text:
            meta_html = f'<span class="cambridge-vocab-meaning"> {meaning_text}</span>'
        else:
            meta_html = ""
        parts.append(f'<span class="cambridge-vocab-chip"{title_attr}><strong>{kw}</strong>{meta_html}</span>')
    return ''.join(parts)


def explanations_to_generalfeedback_html(explanations: Dict[str, Any]) -> str:
    rows = []
    for item in explanations.get("items", []):
        lbl = item.get("label", "")
        ans = item.get("answer", "")
        vi = item.get("explanation_vi", "")
        en = item.get("explanation_en", "")
        ev = item.get("evidence", "") or ""
        ans_html = _format_answer_for_feedback(ans)
        rows.append(
            f"<li><strong>Q{lbl}</strong> — <strong>{ans_html}</strong><br />"
            f"<strong>VI:</strong> {vi}<br />"
            f"<strong>EN:</strong> {en}" + (f"<br /><em>Evidence:</em> {ev}" if ev else "") + "</li>"
        )
    return "<ol>" + "".join(rows) + "</ol>" if rows else ""

# -----------------------------
# OCR scanning
# -----------------------------

HEADER_REGION = (0.0, 0.0, 1.0, 0.28)  # top 28% of page (captures headings well)
QUESTION_HEADER_REGION = (0.0, 0.0, 1.0, 0.45)  # slightly larger for instruction blocks

def scan_pdf_headers(
    pdf_path: Path,
    cache_dir: Path,
    lang: str = "eng",
    zoom: float = 1.4,
) -> List[PageScan]:
    """
    OCR only the header region of every page.
    Cache each header OCR in cache_dir/headers/page_###.txt
    """
    _FALLBACK_LOG.clear()
    doc = fitz.open(str(pdf_path))
    out: List[PageScan] = []
    _ensure_dir(cache_dir / "headers")

    for pno in range(len(doc)):
        cache_file = cache_dir / "headers" / f"page_{pno:04d}.txt"
        if cache_file.exists():
            header_text = cache_file.read_text(encoding="utf-8", errors="ignore")
        else:
            header_text = _ocr_page_region(doc, pno, HEADER_REGION, zoom=zoom, lang=lang, psm=6)
            cache_file.write_text(header_text, encoding="utf-8")

        test_num = _guess_test_num(header_text)

        # Detect reading passage N on header
        rpm = re.search(r"READING\s+PASSAGE\s+([1-3])", header_text, flags=re.IGNORECASE)
        has_reading_passage = int(rpm.group(1)) if rpm else None

        has_writing_task1 = bool(re.search(r"WRITING\s+TASK\s+1", header_text, flags=re.IGNORECASE))
        has_answer_keys = bool(re.search(r"answer\s+keys", header_text, flags=re.IGNORECASE))

        out.append(PageScan(
            pno=pno,
            header_text=header_text,
            test_num=test_num,
            has_reading_passage=has_reading_passage,
            has_writing_task1=has_writing_task1,
            has_answer_keys=has_answer_keys,
        ))

    doc.close()
    return out

def find_reading_range(scans: List[PageScan], test_num: int) -> Tuple[int, int]:
    """
    Find [start_reading, end_reading] page indexes (inclusive) for a given test number.
    start_reading: page where READING PASSAGE 1 appears for the test
    end_reading: page before WRITING TASK 1 for that test
    """
    start = None
    for s in scans:
        if s.test_num == test_num and s.has_reading_passage == 1:
            start = s.pno
            break
    if start is None:
        raise RuntimeError(f"Could not find READING PASSAGE 1 for Test {test_num}")

    end = None
    for s in scans:
        if s.pno > start and s.test_num == test_num and s.has_writing_task1:
            end = s.pno - 1
            break
    if end is None:
        # fallback: until next test start - 1
        next_test = None
        for s in scans:
            if s.pno > start and s.test_num and s.test_num != test_num:
                next_test = s.pno
                break
        end = (next_test - 1) if next_test else (scans[-1].pno)
    return start, end

def find_answer_key_page(scans: List[PageScan], test_num: int, pdf_path: Path, cache_dir: Path, lang: str="eng") -> int:
    """
    Find the page index containing the READING answer keys for a test.
    Strategy:
      - Among pages where header has "answer keys" and "Test N", OCR a bit more and look for "READING".
    """
    doc = fitz.open(str(pdf_path))
    candidates = [s.pno for s in scans if s.has_answer_keys and s.test_num == test_num]
    if not candidates:
        # Sometimes header region might miss. Fallback: scan near end.
        raise RuntimeError(f"Could not find answer key pages for Test {test_num} by header OCR")
    _ensure_dir(cache_dir / "keys")

    for pno in candidates:
        cache_file = cache_dir / "keys" / f"key_{pno:04d}.txt"
        if cache_file.exists():
            txt = cache_file.read_text(encoding="utf-8", errors="ignore")
        else:
            txt = _ocr_page_region(doc, pno, (0.0, 0.0, 1.0, 0.35), zoom=1.6, lang=lang, psm=6)
            cache_file.write_text(txt, encoding="utf-8")
        if re.search(r"\bREADING\b", txt, flags=re.IGNORECASE):
            doc.close()
            return pno

    # As a fallback, return last candidate
    doc.close()
    return candidates[-1]

# -----------------------------
# Answer key parsing
# -----------------------------

def parse_reading_answer_key(ocr_text: str) -> Tuple[Dict[int, List[str]], List[Tuple[Tuple[int,int], List[str]]]]:
    """
    Parse the READING answer key text into:
      - single answers: qnum -> [answers...]
      - paired answers (IN EITHER ORDER): [((q1,q2), [ans1,ans2]), ...]
    """
    singles: Dict[int, List[str]] = {}
    pairs: List[Tuple[Tuple[int,int], List[str]]] = []

    # Normalize weird dashes and ampersands spacing
    txt = ocr_text.replace("—", "-").replace("–", "-")
    lines = [l.strip() for l in txt.splitlines() if l.strip()]

    # Parse pairs like "20&21 IN EITHER ORDER B D"
    pair_re = re.compile(r"^(\d{1,2})\s*&\s*(\d{1,2})\s+IN\s+EITHER\s+ORDER\s+([A-Z])\s+([A-Z])$", flags=re.IGNORECASE)
    # Sometimes OCR merges numbers: "22823 IN EITHER ORDER" (22&23) => handle
    merged_pair_re = re.compile(r"^(\d{1,2})(?:&)?(\d{1,2})\s+IN\s+EITHER\s+ORDER\s+([A-Z])\s+([A-Z])$", flags=re.IGNORECASE)

    # Parse single like "10 intestines / gut" OR "14 D" OR "3 NOT GIVEN"
    single_re = re.compile(r"^(\d{1,2})\s+(.+)$")

    for line in lines:
        m = pair_re.match(line)
        if not m:
            m = merged_pair_re.match(line)
        if m:
            q1, q2 = int(m.group(1)), int(m.group(2))
            a1, a2 = m.group(3).upper(), m.group(4).upper()
            pairs.append(((q1,q2), [a1,a2]))
            continue

        m = single_re.match(line)
        if m:
            q = int(m.group(1))
            ans = m.group(2).strip()
            # Clean common OCR artifacts
            ans = ans.replace("  ", " ")
            # Handle slash-separated acceptable answers: "intestines / gut"
            
            parts = [p.strip() for p in re.split(r"\s*/\s*", ans) if p.strip()]
            # If no slash, keep original ans as a single part
            if len(parts) == 1:
                parts = [ans]
            # Clean OCR artifacts (esp. trailing dots) for each part
            parts = [_clean_key_answer(p) for p in parts if _clean_key_answer(p)]
            if not parts:
                parts = [_clean_key_answer(ans)] if _clean_key_answer(ans) else []
            singles[q] = parts
            continue

    return singles, pairs



def load_reading_answer_keys_for_test(
    pdf_path: Path,
    scans: List[PageScan],
    test_num: int,
    cache_dir: Path,
    lang: str = "eng",
) -> Tuple[Dict[int, List[str]], List[Tuple[Tuple[int,int], List[str]]]]:
    """
    OCR (cached) + parse the READING answer key for a given test.

    Returns:
        singles: Dict[int, List[str]]  (qnum -> acceptable answers)
        pairs:   List[((q1,q2), [ans1, ans2])] for lines like "20&21 IN EITHER ORDER B D"
    """
    doc = fitz.open(str(pdf_path))
    try:
        key_pno = find_answer_key_page(scans, test_num, pdf_path, cache_dir, lang=lang)
        _ensure_dir(cache_dir / "keys")
        key_cache = cache_dir / "keys" / f"reading_key_full_{test_num}.txt"
        if key_cache.exists():
            key_txt = key_cache.read_text(encoding="utf-8", errors="ignore")
        else:
            # OCR larger region of key page for full content
            key_txt = _ocr_page_region(doc, key_pno, (0.0, 0.0, 1.0, 0.95), zoom=1.8, lang=lang, psm=6)
            key_cache.write_text(key_txt, encoding="utf-8")
    finally:
        doc.close()

    return parse_reading_answer_key(key_txt)


# -----------------------------
# Question groups parsing (from question pages)
# -----------------------------

def parse_question_groups_from_pages(
    pdf_path: Path,
    question_pages: List[int],
    cache_dir: Path,
    lang: str = "eng",
) -> List[QuestionGroup]:
    """
    OCR instruction region from each question page, find one or more 'Questions ...' headers
    and determine group types.
    """
    doc = fitz.open(str(pdf_path))
    _ensure_dir(cache_dir / "qpages")
    all_groups: List[QuestionGroup] = []

    for pno in question_pages:
        cache_file = cache_dir / "qpages" / f"qpage_{pno:04d}.txt"
        if cache_file.exists():
            txt = cache_file.read_text(encoding="utf-8", errors="ignore")
        else:
            txt = _ocr_page_region(doc, pno, QUESTION_HEADER_REGION, zoom=1.8, lang=lang, psm=6)
            cache_file.write_text(txt, encoding="utf-8")

        headers = _find_question_headers(txt)
        if not headers:
            # Some pages might not have 'Questions' header (rare). Skip.
            continue

        # Split into blocks by headers positions in lines
        lines = [l.rstrip() for l in txt.splitlines()]
        # Map line indices where a header starts
        header_line_idxs = []
        for i, line in enumerate(lines):
            if re.match(r"^\s*Questions\b", line, flags=re.IGNORECASE):
                header_line_idxs.append(i)
        header_line_idxs.append(len(lines))

        for idx in range(len(header_line_idxs)-1):
            start_i = header_line_idxs[idx]
            end_i = header_line_idxs[idx+1]
            block = "\n".join(lines[start_i:end_i]).strip()
            # Parse header again from first line(s)
            kind = None
            qnums: List[int] = []
            # Range
            m = re.search(r"Questions\s+(\d+)\s*[\-–—]\s*(\d+)", block, flags=re.IGNORECASE)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                qnums = list(range(a, b+1))
                kind = "range"
            else:
                m = re.search(r"Questions\s+(\d+)\s+and\s+(\d+)", block, flags=re.IGNORECASE)
                if m:
                    q1, q2 = int(m.group(1)), int(m.group(2))
                    qnums = [q1, q2]
                    kind = "pair"

            if not kind:
                continue

            gtype, meta = _detect_group_type(block)
            all_groups.append(QuestionGroup(qnums=qnums, group_type=gtype, meta=meta, raw_block=block))

    doc.close()
    # Sort groups by first qnum
    all_groups.sort(key=lambda g: min(g.qnums))
    return all_groups

# -----------------------------
# Build passage bundles
# -----------------------------

def build_passages_for_test(
    pdf_path: Path,
    scans: List[PageScan],
    test_num: int,
    cache_dir: Path,
    lang: str="eng",
) -> List[PassageBundle]:
    """
    Identify passage pages + question pages for Reading Passage 1/2/3 for a given test.
    """
    doc = fitz.open(str(pdf_path))
    start_reading, end_reading = find_reading_range(scans, test_num)

    # Find passage start pages by header scan
    # IMPORTANT: In some Cambridge PDFs, Passage 3 pages do NOT include "Test N" in the header (only "Reading"),
    # so relying on s.test_num would miss the passage start. We are already inside the reading range for this test,
    # so use page range instead of test_num equality.
    passage_starts: Dict[int, int] = {}
    for s in scans:
        if s.pno < start_reading or s.pno > end_reading:
            continue
        if s.has_reading_passage:
            # keep the earliest page we see for each passage number
            passage_starts.setdefault(s.has_reading_passage, s.pno)

    missing = [n for n in [1,2,3] if n not in passage_starts]
    if missing:
        raise RuntimeError(f"Could not find starts for passages {missing} in Test {test_num}")

    # Determine question ranges per passage by OCR on passage start page line that includes "Questions x-y"
    _ensure_dir(cache_dir / "passage_start")
    qrange_by_passage: Dict[int, Tuple[int,int]] = {}
    for pn, pno in passage_starts.items():
        cache_file = cache_dir / "passage_start" / f"passage{pn}_p{pno:04d}.txt"
        if cache_file.exists():
            txt = cache_file.read_text(encoding="utf-8", errors="ignore")
        else:
            txt = _ocr_page_region(doc, pno, (0.0, 0.0, 1.0, 0.45), zoom=1.8, lang=lang, psm=6)
            cache_file.write_text(txt, encoding="utf-8")
        # Find "Questions 1-13" etc
        m = re.search(r"Questions\s+(\d+)\s*[\-–—]\s*(\d+)", txt, flags=re.IGNORECASE)
        if not m:
            # Fallback to IELTS standard
            if pn == 1:
                qrange_by_passage[pn] = (1,13)
            elif pn == 2:
                qrange_by_passage[pn] = (14,26)
            else:
                qrange_by_passage[pn] = (27,40)
        else:
            qrange_by_passage[pn] = (int(m.group(1)), int(m.group(2)))

    # Identify for each passage: find first question page after start where a line begins with "Questions"
    # We'll use OCR header for pages to detect.
    bundles: List[PassageBundle] = []
    for pn in [1,2,3]:
        p_start = passage_starts[pn]
        p_next = passage_starts[pn+1] if pn < 3 else (end_reading + 1)

        # Determine question start page
        q_start = None
        for pno in range(p_start+1, p_next):
            # Use cached header OCR from scans; check if it starts with "Questions"
            hdr = scans[pno].header_text
            # Some headers have "Reading" only; so do quick OCR of top left to detect "Questions" headings
            if re.search(r"^\s*Questions\b", hdr.strip(), flags=re.IGNORECASE):
                q_start = pno
                break
            # If header region didn't capture, do a small OCR check on top-left area (faster)
            # but only if we suspect it's a questions page by seeing the word "Questions" in header.
            if "Questions" in hdr or "questions" in hdr:
                # If "Questions" is present, treat as question page.
                q_start = pno
                break
            # else, still might be a questions page. To be safe, OCR a tiny region for the exact marker.
            tiny = _ocr_page_region(doc, pno, (0.0, 0.0, 0.55, 0.18), zoom=1.6, lang=lang, psm=6)
            if re.search(r"^\s*Questions\b", tiny.strip(), flags=re.IGNORECASE):
                q_start = pno
                break

        if q_start is None:
            # If not found, assume passage occupies everything (rare)
            q_start = p_next

        passage_pages = list(range(p_start, q_start))
        question_pages = list(range(q_start, p_next))

        groups = parse_question_groups_from_pages(pdf_path, question_pages, cache_dir, lang=lang)

        bundles.append(PassageBundle(
            test_num=test_num,
            passage_num=pn,
            qrange=qrange_by_passage[pn],
            passage_pages=passage_pages,
            question_pages=question_pages,
            groups=groups,
        ))

    doc.close()
    return bundles

# -----------------------------
# Moodle XML generation
# -----------------------------

def _canonical_correct_for_options(corr: str, opts: List[str]) -> str:
    opts_clean = [o.strip() for o in opts if str(o or "").strip()]
    opts_upper = [o.upper() for o in opts_clean]
    cand: List[str] = []
    if corr:
        cand.extend([corr, corr.upper(), _clean_key_answer(corr), _clean_key_answer(corr).upper()])
    if "NOT GIVEN" in opts_upper:
        letters_only = re.sub(r"[^A-Z]", "", (corr or "").upper())
        if letters_only == "NOTGIVEN":
            cand.append("NOT GIVEN")
    if opts_clean and all(re.match(r"^[A-Z]$", o.strip().upper()) for o in opts_clean):
        fixed = _maybe_fix_letter_token(corr)
        if fixed:
            cand.append(fixed)
    for v in cand:
        if not v:
            continue
        v_up = v.strip().upper()
        for i, ou in enumerate(opts_upper):
            if ou == v_up or ou.startswith(v_up + ".") or ou.startswith(v_up + ")") or ou.startswith(v_up + " "):
                return opts_clean[i]
    return (corr or "").strip()


def _choice_qtype(as_radio: bool = False, shuffle: bool = False, layout: str = "vertical") -> str:
    layout_norm = str(layout or "vertical").strip().lower()
    if not as_radio:
        return "MCS" if shuffle else "MC"
    if layout_norm == "horizontal":
        return "MCHS" if shuffle else "MCH"
    return "MCVS" if shuffle else "MCV"


def _multireponse_qtype(shuffle: bool = False, layout: str = "vertical") -> str:
    layout_norm = str(layout or "vertical").strip().lower()
    if layout_norm == "horizontal":
        return "MRHS" if shuffle else "MRH"
    return "MRS" if shuffle else "MR"


def _make_cloze_field_dropdown(correct: str, options: List[str], weight: int = 1, as_radio: bool=False, shuffle: bool=False, feedback_text: str = "", layout: str = "vertical") -> str:
    """Build a Cloze MULTICHOICE field."""
    correct_raw = (correct or "").strip()
    correct_canon = _canonical_correct_for_options(correct_raw, options)
    fb = _escape_cloze_feedback(feedback_text.strip()) if feedback_text else ""
    opt_tokens = []
    found = False
    for opt in options:
        opt_clean = opt.strip()
        prefix = "=" if (opt_clean.upper() == correct_canon.upper() and not found) else ""
        if prefix:
            found = True
        token = prefix + _escape_cloze_answer(opt_clean)
        if fb:
            token += "#" + fb
        opt_tokens.append(token)

    if not found and opt_tokens:
        try:
            _FALLBACK_LOG.append(f"[DROPDOWN-FALLBACK] correct='{correct_raw}' canon='{correct_canon}' options={options} -> using '{options[0] if options else ''}'")
        except Exception:
            pass
        opt_tokens[0] = "=" + opt_tokens[0].lstrip("=")

    qtype = _choice_qtype(as_radio=as_radio, shuffle=shuffle, layout=layout)
    return "{" + f"{weight}:{qtype}:" + "~".join(opt_tokens) + "}"


def _make_cloze_field_shortanswer(corrects: List[str], weight: int = 1, feedback_text: str = "") -> str:
    """Build a Cloze SHORTANSWER field with one or more correct answers and optional generic feedback."""
    fb = _escape_cloze_feedback(feedback_text.strip()) if feedback_text else ""
    tokens = []
    for ans in corrects:
        ans = _escape_cloze_answer(ans.strip())
        tok = f"={ans}"
        if fb:
            tok += "#" + fb
        tokens.append(tok)
    if fb:
        tokens.append(f"*#{fb}")
    return "{" + f"{weight}:SHORTANSWER:" + "~".join(tokens) + "}"


def _make_cloze_field_multireponse(correct_options: List[str], options: List[str], weight: int = 2, shuffle: bool=False, feedback_text: str = "", layout: str = "vertical", penalize_wrong: bool = False) -> str:
    """Build a Cloze MULTIRESPONSE field (checkboxes).

    IELTS-style choose-two questions should give partial credit for each correct pick
    without subtracting marks for an incorrect tick, so the default here is
    ``penalize_wrong=False``.
    """
    opts_clean = [o.strip() for o in options if str(o or "").strip()]
    if not opts_clean:
        opts_clean = [x.strip() for x in (correct_options or []) if str(x or "").strip()]
    canon_correct = {
        _canonical_correct_for_options(c, opts_clean).upper()
        for c in (correct_options or [])
        if str(c or "").strip()
    }
    fb = _escape_cloze_feedback(feedback_text.strip()) if feedback_text else ""
    n_correct = max(1, len(canon_correct))
    n_wrong = max(1, len([o for o in opts_clean if o.upper() not in canon_correct]))
    correct_pct = 100.0 / n_correct
    wrong_pct = (-100.0 / n_wrong) if penalize_wrong else 0.0

    def _pct_token(val: float) -> str:
        if abs(val - round(val)) < 1e-9:
            return str(int(round(val)))
        return (f"{val:.6f}").rstrip("0").rstrip(".")

    opt_tokens = []
    for opt in opts_clean:
        is_correct = opt.upper() in canon_correct
        escaped_opt = _escape_cloze_answer(opt)
        pct = correct_pct if is_correct else wrong_pct
        token = f"%{_pct_token(pct)}%" + escaped_opt
        if fb:
            token += "#" + fb
        opt_tokens.append(token)
    qtype = _multireponse_qtype(shuffle=shuffle, layout=layout)
    return "{" + f"{weight}:{qtype}:" + "~".join(opt_tokens) + "}"


def _make_cloze_field_choose_two_combo(correct_letters: List[str], letter_range: List[str], weight: int = 2, shuffle: bool=False, feedback_text: str = "") -> str:
    corr = "".join(sorted([_maybe_fix_letter_token(c) for c in correct_letters if _maybe_fix_letter_token(c)]))
    combos = _all_2_letter_combos(letter_range)
    if corr not in combos:
        combos = sorted(set(combos + [corr]))
    return _make_cloze_field_dropdown(correct=corr, options=combos, weight=weight, as_radio=False, shuffle=shuffle, feedback_text=feedback_text)


def _load_choice_meta(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _choice_display_options(group: QuestionGroup, fallback_letters: List[str], qnum: Optional[int] = None) -> List[str]:
    meta = group.meta or {}
    if qnum is not None:
        by_q = _load_choice_meta(meta.get("choice_text_by_q_json"))
        q_map = by_q.get(str(qnum)) if isinstance(by_q, dict) else None
        if isinstance(q_map, dict) and q_map:
            opts = []
            for letter in fallback_letters:
                txt = str(q_map.get(letter, "")).strip()
                opts.append(f"{letter}. {txt}" if txt else letter)
            return opts
    mapping = _load_choice_meta(meta.get("choice_text_json"))
    if isinstance(mapping, dict) and mapping:
        opts = []
        for letter in fallback_letters:
            txt = str(mapping.get(letter, "")).strip()
            opts.append(f"{letter}. {txt}" if txt else letter)
        return opts
    return list(fallback_letters)


def _qnums_to_fields(
    qrange: Tuple[int,int],
    groups: List[QuestionGroup],
    singles: Dict[int, List[str]],
    pairs: List[Tuple[Tuple[int,int], List[str]]],
    prefer_radio_small: bool = True,
    shuffle: bool = False,
    feedback_by_label: Optional[Dict[str, Dict[str, Any]]] = None,
    choice_layout: str = "vertical",
) -> List[Tuple[str, str, int]]:
    """Produce list of (label, cloze_field, weight) in display order."""
    q_start, q_end = qrange
    pair_map: Dict[Tuple[int,int], List[str]] = {}
    for (a,b), letters in pairs:
        pair_map[(min(a,b), max(a,b))] = letters

    groups_sorted = sorted(groups, key=lambda g: min(g.qnums))
    used: set[int] = set()
    outputs: List[Tuple[str, str, int]] = []

    for g in groups_sorted:
        if max(g.qnums) < q_start or min(g.qnums) > q_end:
            continue

        if g.group_type == "choose_two_letters" and len(g.qnums) == 2:
            a, b = sorted(g.qnums)
            if a < q_start or b > q_end:
                continue
            key = (a,b)
            correct_letters = pair_map.get(key)
            if not correct_letters:
                la = singles.get(a, [])
                lb = singles.get(b, [])
                correct_letters = []
                if la and re.match(r"^[A-Z]$", la[0].strip(), flags=re.IGNORECASE):
                    correct_letters.append(la[0].strip().upper())
                if lb and re.match(r"^[A-Z]$", lb[0].strip(), flags=re.IGNORECASE):
                    correct_letters.append(lb[0].strip().upper())
                if len(correct_letters) != 2:
                    correct_letters = ["A","B"]

            if "letters" in g.meta and re.match(r"^[A-Z]\-[A-Z]$", g.meta["letters"].strip().upper()):
                aL, bL = g.meta["letters"].strip().upper().split("-")
                letters = _letters_range(aL, bL)
            elif "letters_list" in g.meta:
                letters = [x.strip().upper() for x in str(g.meta.get("letters_list", "")).split(",") if x.strip()]
            else:
                letters = list("ABCDE")

            display_options = _choice_display_options(g, letters)
            field = _make_cloze_field_multireponse(
                correct_options=[_maybe_fix_letter_token(x) for x in correct_letters],
                options=display_options,
                weight=2,
                shuffle=shuffle,
                feedback_text=_feedback_text_from_item((feedback_by_label or {}).get(f"{a}-{b}")),
                layout=choice_layout,
                penalize_wrong=False,
            )
            outputs.append((f"{a}-{b}", field, 2))
            used.update([a,b])
            continue

        for q in g.qnums:
            if q < q_start or q > q_end or q in used:
                continue

            ans_norm = [a.strip() for a in singles.get(q, []) if a.strip()]

            if g.group_type in ("tfng", "yesno"):
                options = ["TRUE","FALSE","NOT GIVEN"] if g.group_type == "tfng" else ["YES","NO","NOT GIVEN"]
                correct = ans_norm[0].upper() if ans_norm else options[0]
                field = _make_cloze_field_dropdown(correct=correct, options=options, weight=1, as_radio=True, shuffle=False, feedback_text=_feedback_text_from_item((feedback_by_label or {}).get(str(q))), layout=choice_layout)
                outputs.append((str(q), field, 1))
                used.add(q)
                continue

            if g.group_type in ("mc_letters", "letter_dropdown"):
                letters: List[str] = []
                if "letters" in g.meta and re.match(r"^[A-Z]\-[A-Z]$", g.meta["letters"].strip().upper()):
                    aL, bL = g.meta["letters"].strip().upper().split("-")
                    letters = _letters_range(aL, bL)
                elif "letters_list" in g.meta:
                    letters = [x.strip().upper() for x in str(g.meta.get("letters_list", "")).split(",") if x.strip()]
                else:
                    letters = list("ABCD")
                correct = ans_norm[0].upper() if ans_norm else letters[0]
                if g.group_type == "mc_letters":
                    display_options = _choice_display_options(g, letters, qnum=q)
                    correct_display = _canonical_correct_for_options(correct, display_options) if display_options != letters else correct
                    field = _make_cloze_field_dropdown(correct=correct_display, options=display_options, weight=1, as_radio=True, shuffle=shuffle, feedback_text=_feedback_text_from_item((feedback_by_label or {}).get(str(q))), layout=choice_layout)
                else:
                    field = _make_cloze_field_dropdown(correct=correct, options=letters, weight=1, as_radio=False, shuffle=shuffle, feedback_text=_feedback_text_from_item((feedback_by_label or {}).get(str(q))))
                outputs.append((str(q), field, 1))
                used.add(q)
                continue

            if g.group_type == "choose_one_word":
                if not ans_norm:
                    ans_norm = ["*"]
                field = _make_cloze_field_shortanswer(corrects=ans_norm, weight=1, feedback_text=_feedback_text_from_item((feedback_by_label or {}).get(str(q))))
                outputs.append((str(q), field, 1))
                used.add(q)
                continue

            if ans_norm and re.match(r"^(TRUE|FALSE|NOT GIVEN|YES|NO)$", ans_norm[0].upper()):
                if ans_norm[0].upper() in ("TRUE","FALSE","NOT GIVEN"):
                    options = ["TRUE","FALSE","NOT GIVEN"]
                    correct = ans_norm[0].upper()
                    field = _make_cloze_field_dropdown(correct=correct, options=options, weight=1, as_radio=True, shuffle=False, feedback_text=_feedback_text_from_item((feedback_by_label or {}).get(str(q))), layout=choice_layout)
                else:
                    options = ["YES","NO","NOT GIVEN"]
                    correct = ans_norm[0].upper()
                    field = _make_cloze_field_dropdown(correct=correct, options=options, weight=1, as_radio=True, shuffle=False, feedback_text=_feedback_text_from_item((feedback_by_label or {}).get(str(q))), layout=choice_layout)
                outputs.append((str(q), field, 1))
                used.add(q)
            elif ans_norm and re.match(r"^[A-Z]$", ans_norm[0].upper()):
                field = _make_cloze_field_dropdown(correct=ans_norm[0].upper(), options=list("ABCD"), weight=1, as_radio=False, shuffle=shuffle)
                outputs.append((str(q), field, 1))
                used.add(q)
            else:
                if not ans_norm:
                    ans_norm = ["*"]
                field = _make_cloze_field_shortanswer(corrects=ans_norm, weight=1, feedback_text=_feedback_text_from_item((feedback_by_label or {}).get(str(q))))
                outputs.append((str(q), field, 1))
                used.add(q)

    for q in range(q_start, q_end+1):
        if q in used:
            continue
        ans_norm = [a.strip() for a in singles.get(q, []) if a.strip()]
        if ans_norm and re.match(r"^[A-Z]$", ans_norm[0].upper()):
            field = _make_cloze_field_dropdown(correct=ans_norm[0].upper(), options=list("ABCD"), weight=1, as_radio=False, shuffle=shuffle, feedback_text=_feedback_text_from_item((feedback_by_label or {}).get(str(q))))
        else:
            field = _make_cloze_field_shortanswer(corrects=ans_norm or ["*"], weight=1, feedback_text=_feedback_text_from_item((feedback_by_label or {}).get(str(q))))
        outputs.append((str(q), field, 1))
        used.add(q)

    def label_key(lbl: str) -> int:
        if "-" in lbl:
            return int(lbl.split("-")[0])
        return int(lbl)
    outputs.sort(key=lambda t: label_key(t[0]))
    return outputs

def _html_for_passage_question(
    passage_images: List[str],
    question_images: List[str],
    fields: List[Tuple[str,str,int]],
    title: str,
) -> str:
    # Build images HTML
    def imgs_html(img_names: List[str]) -> str:
        parts = []
        for name in img_names:
            parts.append(
                f'<div style="margin:0 0 10px 0;">'
                f'<img src="@@PLUGINFILE@@/{name}" style="width:100%; height:auto; border:1px solid #eee;" />'
                f'</div>'
            )
        return "\n".join(parts)

    # Build answer table
    rows = []
    for lbl, field, _w in fields:
        rows.append(f"<tr><td style='padding:4px 8px; white-space:nowrap; width:1%;'><strong>{lbl}</strong></td>"
                    f"<td style='padding:4px 8px;'>{field}</td></tr>")
    answers_table = "<table style='width:100%; border-collapse:collapse;'>" + "".join(rows) + "</table>"

    html = f"""
<div>
  <p><strong>{title}</strong></p>
  <table style="width:100%; border-collapse:collapse;">
    <tr>
      <td style="width:50%; vertical-align:top; padding:10px; border-right:1px solid #ddd;">
        <div style="max-height:75vh; overflow:auto;">
          {imgs_html(passage_images)}
        </div>
      </td>
      <td style="width:50%; vertical-align:top; padding:10px;">
        <div style="max-height:75vh; overflow:auto;">
          {imgs_html(question_images)}
          <hr style="margin:12px 0;" />
          <p><strong>Answers</strong></p>
          {answers_table}
          <p style="font-size:0.9em; color:#666; margin-top:10px;">
            Tip: The questions are shown above as images. Enter your answers in the table.
          </p>
        </div>
      </td>
    </tr>
  </table>
</div>
""".strip()
    return html

def build_moodle_xml_reading(
    pdf_path: Path,
    bundles: List[PassageBundle],
    scans: List[PageScan],
    out_dir: Path,
    cache_dir: Path,
    lang: str = "eng",
    image_zoom: float = 2.0,
    jpeg_quality: int = 80,
    category: str = "IELTS/Reading",
    shuffle_choices: bool = False,
    output_name: str = "moodle_reading.xml",
    keys_by_test: Optional[Dict[int, Tuple[Dict[int, List[str]], List[Tuple[Tuple[int,int], List[str]]]]]] = None,
    feedback_items_by_bundle: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
) -> Path:
    """
    Build a Moodle XML file containing Cloze questions for all passage bundles.
    """
    _ensure_dir(out_dir)
    _ensure_dir(cache_dir / "images")

    doc = fitz.open(str(pdf_path))

    # Group bundles by test, because we need answer key per test.
    bundles_by_test: Dict[int, List[PassageBundle]] = {}
    for b in bundles:
        bundles_by_test.setdefault(b.test_num, []).append(b)

    # Moodle XML root
    quiz = ET.Element("quiz")

    # Add category question
    cat_q = ET.SubElement(quiz, "question", {"type":"category"})
    cat_text = ET.SubElement(ET.SubElement(cat_q, "category"), "text")
    cat_text.text = f"$course$/{category}"

    # Generate questions
    for test_num, test_bundles in sorted(bundles_by_test.items(), key=lambda kv: kv[0]):
        # Load answer keys (either from overrides, or OCR+cache)
        if keys_by_test is not None and test_num in keys_by_test:
            singles, pairs = keys_by_test[test_num]
        else:
            key_pno = find_answer_key_page(scans, test_num, pdf_path, cache_dir, lang=lang)
            key_cache = cache_dir / "keys" / f"reading_key_full_{test_num}.txt"
            if key_cache.exists():
                key_txt = key_cache.read_text(encoding="utf-8", errors="ignore")
            else:
                # OCR larger region of key page for full content
                key_txt = _ocr_page_region(doc, key_pno, (0.0, 0.0, 1.0, 0.95), zoom=1.8, lang=lang, psm=6)
                key_cache.write_text(key_txt, encoding="utf-8")
            singles, pairs = parse_reading_answer_key(key_txt)

        for b in sorted(test_bundles, key=lambda x: x.passage_num):
            # Render & embed images
            passage_img_names: List[str] = []
            question_img_names: List[str] = []
            embedded_files: List[Tuple[str, bytes]] = []

            def add_page_image(pno: int, prefix: str) -> str:
                name = f"{prefix}_t{test_num}_p{pno:03d}.jpg"
                img_path = cache_dir / "images" / name
                if img_path.exists():
                    img_bytes = img_path.read_bytes()
                else:
                    img = _render_page(doc, pno, zoom=image_zoom)
                    img_bytes = _img_to_jpeg_bytes(img, quality=jpeg_quality)
                    img_path.write_bytes(img_bytes)
                embedded_files.append((name, img_bytes))
                return name

            for pno in b.passage_pages:
                passage_img_names.append(add_page_image(pno, f"passage{b.passage_num}"))
            for pno in b.question_pages:
                question_img_names.append(add_page_image(pno, f"q{b.passage_num}"))

            # Build cloze fields
            bid = bundle_id(b)
            feedback_by_label = (feedback_items_by_bundle or {}).get(bid, {})
            fields = _qnums_to_fields(
                qrange=b.qrange,
                groups=b.groups,
                singles=singles,
                pairs=pairs,
                prefer_radio_small=True,
                shuffle=shuffle_choices,
                feedback_by_label=feedback_by_label,
            )

            # Compute total weight as default mark
            total_mark = sum(w for _lbl, _f, w in fields)

            title = f"Test {test_num} - Reading Passage {b.passage_num} (Q{b.qrange[0]}-{b.qrange[1]})"
            html = _html_for_passage_question(
                passage_images=passage_img_names,
                question_images=question_img_names,
                fields=fields,
                title=title,
            )

            q = ET.SubElement(quiz, "question", {"type":"cloze"})
            name = ET.SubElement(ET.SubElement(q, "name"), "text")
            name.text = title

            qtext = ET.SubElement(q, "questiontext", {"format":"html"})
            text_el = ET.SubElement(qtext, "text")
            text_el.text = html

            # Attach files
            for fname, fbytes in embedded_files:
                file_el = ET.SubElement(qtext, "file", {"name": fname, "path": "/", "encoding":"base64"})
                file_el.text = base64.b64encode(fbytes).decode("ascii")

            defaultgrade = ET.SubElement(q, "defaultgrade")
            defaultgrade.text = str(total_mark)

            penalty = ET.SubElement(q, "penalty")
            penalty.text = "0.3333333"

            hidden = ET.SubElement(q, "hidden")
            hidden.text = "0"

            gf = ET.SubElement(q, "generalfeedback", {"format":"html"})
            if feedback_by_label:
                gf_payload = {"items": list(feedback_by_label.values())}
                ET.SubElement(gf, "text").text = explanations_to_generalfeedback_html(gf_payload)
            else:
                ET.SubElement(gf, "text").text = ""

    doc.close()

    # Write XML
    xml_path = out_dir / output_name
    tree = ET.ElementTree(quiz)
    ET.indent(tree, space="  ", level=0)  # Python 3.9+
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)
    # Write fallback warnings if any (helps you find OCR mismatches).
    if _FALLBACK_LOG:
        warn_path = out_dir / "import_warnings.txt"
        warn_path.write_text("\n".join(_FALLBACK_LOG), encoding="utf-8")
    return xml_path



# -----------------------------
# HTML export (optional)
# -----------------------------

def _html_for_passage_question_inline(
    passage_images: List[Tuple[str, bytes]],
    question_images: List[Tuple[str, bytes]],
    fields: List[Tuple[str, str, int]],
    title: str,
) -> str:
    """
    Same layout as _html_for_passage_question, but embeds images as data: URIs (base64).
    Useful for a *single self-contained HTML file*.
    Note: Some Moodle configurations may strip data: URIs; if so, prefer Moodle XML import.
    """
    def imgs_html(entries: List[Tuple[str, bytes]]) -> str:
        parts = []
        for name, b in entries:
            src = "data:image/jpeg;base64," + base64.b64encode(b).decode("ascii")
            parts.append(
                f'<div style="margin:0 0 10px 0;">'
                f'<img src="{src}" alt="{name}" style="width:100%; height:auto; border:1px solid #eee;" />'
                f'</div>'
            )
        return "\n".join(parts)

    # Build answer table
    rows = []
    for lbl, field, _w in fields:
        rows.append(
            f"<tr>"
            f"<td style='padding:4px 8px; white-space:nowrap; width:1%;'><strong>{lbl}</strong></td>"
            f"<td style='padding:4px 8px;'>{field}</td>"
            f"</tr>"
        )
    answers_table = "<table style='width:100%; border-collapse:collapse;'>" + "".join(rows) + "</table>"

    html = f"""
<div>
  <p><strong>{title}</strong></p>
  <table style="width:100%; border-collapse:collapse;">
    <tr>
      <td style="width:50%; vertical-align:top; padding:10px; border-right:1px solid #ddd;">
        <div style="max-height:75vh; overflow:auto;">
          {imgs_html(passage_images)}
        </div>
      </td>
      <td style="width:50%; vertical-align:top; padding:10px;">
        <div style="max-height:75vh; overflow:auto;">
          {imgs_html(question_images)}
          <hr style="margin:12px 0;" />
          <p><strong>Answers</strong></p>
          {answers_table}
          <p style="font-size:0.9em; color:#666; margin-top:10px;">
            Tip: The questions are shown above as images. Enter your answers in the table.
          </p>
        </div>
      </td>
    </tr>
  </table>
</div>
""".strip()
    return html


def build_html_reading(
    pdf_path: Path,
    bundles: List[PassageBundle],
    scans: List[PageScan],
    out_dir: Path,
    cache_dir: Path,
    lang: str = "eng",
    image_zoom: float = 2.0,
    jpeg_quality: int = 80,
    shuffle_choices: bool = False,
    html_mode: str = "moodle",   # "moodle" (@@PLUGINFILE@@) or "standalone" (data URI)
    output_name: str = "reading_snippets.html",
    export_assets: bool = True,
    keys_by_test: Optional[Dict[int, Tuple[Dict[int, List[str]], List[Tuple[Tuple[int,int], List[str]]]]]] = None,
) -> Tuple[Path, Optional[Path]]:
    """
    Export HTML snippets for manual paste or inspection.

    Returns:
        (html_path, assets_dir_or_None)

    Modes:
      - html_mode="moodle": HTML uses @@PLUGINFILE@@/filename for images.
        If export_assets=True, images are written to <out>/html_assets/ so you can upload them to Moodle.
      - html_mode="standalone": embeds images as data:image/jpeg;base64,... in a single HTML file.
    """
    _ensure_dir(out_dir)
    _ensure_dir(cache_dir / "images")

    assets_dir: Optional[Path] = None
    if html_mode == "moodle" and export_assets:
        assets_dir = out_dir / "html_assets"
        _ensure_dir(assets_dir)

    doc = fitz.open(str(pdf_path))

    bundles_by_test: Dict[int, List[PassageBundle]] = {}
    for b in bundles:
        bundles_by_test.setdefault(b.test_num, []).append(b)

    snippets: List[str] = []
    snippets.append("<!doctype html><html><head><meta charset='utf-8'><title>Reading snippets</title></head><body>")
    snippets.append("<h2>Reading snippets</h2>")
    snippets.append("<p>Generated by cambridge_pdf2moodle.py</p>")
    snippets.append("<hr>")

    for test_num, test_bundles in sorted(bundles_by_test.items(), key=lambda kv: kv[0]):
        # Load answer keys (either from overrides, or OCR+cache)
        if keys_by_test is not None and test_num in keys_by_test:
            singles, pairs = keys_by_test[test_num]
        else:
            key_pno = find_answer_key_page(scans, test_num, pdf_path, cache_dir, lang=lang)
            key_cache = cache_dir / "keys" / f"reading_key_full_{test_num}.txt"
            if key_cache.exists():
                key_txt = key_cache.read_text(encoding="utf-8", errors="ignore")
            else:
                key_txt = _ocr_page_region(doc, key_pno, (0.0, 0.0, 1.0, 0.95), zoom=1.8, lang=lang, psm=6)
                key_cache.write_text(key_txt, encoding="utf-8")
            singles, pairs = parse_reading_answer_key(key_txt)

        snippets.append(f"<h3>Test {test_num}</h3>")

        for b in sorted(test_bundles, key=lambda x: x.passage_num):
            # Render & cache images
            passage_inline: List[Tuple[str, bytes]] = []
            question_inline: List[Tuple[str, bytes]] = []
            passage_names: List[str] = []
            question_names: List[str] = []

            def add_page_image(pno: int, prefix: str) -> Tuple[str, bytes]:
                name = f"{prefix}_t{test_num}_p{pno:03d}.jpg"
                img_path = cache_dir / "images" / name
                if img_path.exists():
                    img_bytes = img_path.read_bytes()
                else:
                    img = _render_page(doc, pno, zoom=image_zoom)
                    img_bytes = _img_to_jpeg_bytes(img, quality=jpeg_quality)
                    img_path.write_bytes(img_bytes)
                # Optional assets export for Moodle snippet mode
                if assets_dir is not None:
                    (assets_dir / name).write_bytes(img_bytes)
                return name, img_bytes

            for pno in b.passage_pages:
                name, img_bytes = add_page_image(pno, f"passage{b.passage_num}")
                passage_inline.append((name, img_bytes))
                passage_names.append(name)
            for pno in b.question_pages:
                name, img_bytes = add_page_image(pno, f"q{b.passage_num}")
                question_inline.append((name, img_bytes))
                question_names.append(name)

            bid = bundle_id(b)
            feedback_by_label = (feedback_items_by_bundle or {}).get(bid, {})
            fields = _qnums_to_fields(
                qrange=b.qrange,
                groups=b.groups,
                singles=singles,
                pairs=pairs,
                prefer_radio_small=True,
                shuffle=shuffle_choices,
                feedback_by_label=feedback_by_label,
            )

            title = f"Test {test_num} - Reading Passage {b.passage_num} (Q{b.qrange[0]}-{b.qrange[1]})"

            if html_mode == "standalone":
                html = _html_for_passage_question_inline(
                    passage_images=passage_inline,
                    question_images=question_inline,
                    fields=fields,
                    title=title,
                )
            else:
                html = _html_for_passage_question(
                    passage_images=passage_names,
                    question_images=question_names,
                    fields=fields,
                    title=title,
                )

            snippets.append(html)
            snippets.append("<hr>")

    snippets.append("</body></html>")
    doc.close()

    html_path = out_dir / output_name
    html_path.write_text("\n".join(snippets), encoding="utf-8")
    return html_path, assets_dir


# -----------------------------
# CLI
# -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Convert Cambridge IELTS-style scanned PDF to Moodle XML (Reading).")
    ap.add_argument("--pdf", required=True, type=str, help="Path to input PDF (image-only or normal).")
    ap.add_argument("--out", required=True, type=str, help="Output folder.")
    ap.add_argument("--tests", nargs="+", type=int, default=[1], help="Test numbers to export, e.g. 1 2 3 4")
    ap.add_argument("--mode", choices=["reading"], default="reading", help="Currently only reading is implemented.")
    ap.add_argument("--cache", type=str, default="", help="Cache folder (default: <out>/.cache)")
    ap.add_argument("--lang", type=str, default="eng", help="Tesseract language (default: eng).")
    ap.add_argument("--category", type=str, default="IELTS/Reading", help="Moodle question category path.")
    ap.add_argument("--image-zoom", type=float, default=2.0, help="Render zoom for images (higher = clearer but larger).")
    ap.add_argument("--jpeg-quality", type=int, default=80, help="JPEG quality 1-95.")
    ap.add_argument("--shuffle-choices", action="store_true", help="Use shuffled Cloze subquestion types where applicable.")
    ap.add_argument("--output-mode", choices=["all","per-test"], default="all", help="Output 1 file for all tests or 1 file per test.")
    ap.add_argument("--xml-name", type=str, default="moodle_reading.xml", help="XML output filename (when output-mode=all).")
    ap.add_argument("--export-html", action="store_true", help="Also export HTML snippets.")
    ap.add_argument("--html-mode", choices=["moodle","standalone"], default="moodle", help="HTML mode: moodle uses @@PLUGINFILE@@, standalone embeds base64 images.")
    ap.add_argument("--html-name", type=str, default="reading_snippets.html", help="HTML output filename (when output-mode=all).")
    ap.add_argument("--export-html-assets", action="store_true", help="When html-mode=moodle, also write images into <out>/html_assets for manual upload.")
    args = ap.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    cache_dir = Path(args.cache).expanduser().resolve() if args.cache else (out_dir / ".cache")

    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    _ensure_dir(out_dir)
    _ensure_dir(cache_dir)

    # If Windows user needs to point to tesseract:
    tcmd = os.environ.get("TESSERACT_CMD")
    if tcmd:
        pytesseract.pytesseract.tesseract_cmd = tcmd

    print("1) Scanning PDF headers (cached)...")
    scans = scan_pdf_headers(pdf_path, cache_dir, lang=args.lang)

    bundles: List[PassageBundle] = []
    for t in args.tests:
        print(f"2) Building reading passage bundles for Test {t} ...")
        bundles.extend(build_passages_for_test(pdf_path, scans, t, cache_dir, lang=args.lang))

    print("3) Generating outputs (Reading)...")
    if args.output_mode == "all":
        xml_path = build_moodle_xml_reading(
            pdf_path=pdf_path,
            bundles=bundles,
            scans=scans,
            out_dir=out_dir,
            cache_dir=cache_dir,
            lang=args.lang,
            image_zoom=args.image_zoom,
            jpeg_quality=args.jpeg_quality,
            category=args.category,
            shuffle_choices=args.shuffle_choices,
            output_name=args.xml_name,
        )
        print(f"Done. XML Output: {xml_path}")

        if args.export_html:
            html_path, assets_dir = build_html_reading(
                pdf_path=pdf_path,
                bundles=bundles,
                scans=scans,
                out_dir=out_dir,
                cache_dir=cache_dir,
                lang=args.lang,
                image_zoom=args.image_zoom,
                jpeg_quality=args.jpeg_quality,
                shuffle_choices=args.shuffle_choices,
                html_mode=args.html_mode,
                output_name=args.html_name,
                export_assets=args.export_html_assets,
            )
            print(f"HTML Output: {html_path}")
            if assets_dir:
                print(f"HTML assets (images): {assets_dir}")
    else:
        # One file per test
        bundles_by_test: Dict[int, List[PassageBundle]] = {}
        for b in bundles:
            bundles_by_test.setdefault(b.test_num, []).append(b)

        for t in sorted(bundles_by_test.keys()):
            xml_name = f"moodle_reading_test{t}.xml"
            xml_path = build_moodle_xml_reading(
                pdf_path=pdf_path,
                bundles=bundles_by_test[t],
                scans=scans,
                out_dir=out_dir,
                cache_dir=cache_dir,
                lang=args.lang,
                image_zoom=args.image_zoom,
                jpeg_quality=args.jpeg_quality,
                category=args.category,
                shuffle_choices=args.shuffle_choices,
                output_name=xml_name,
            )
            print(f"Done. XML Output (Test {t}): {xml_path}")

            if args.export_html:
                html_name = f"reading_snippets_test{t}.html"
                html_path, assets_dir = build_html_reading(
                    pdf_path=pdf_path,
                    bundles=bundles_by_test[t],
                    scans=scans,
                    out_dir=out_dir,
                    cache_dir=cache_dir,
                    lang=args.lang,
                    image_zoom=args.image_zoom,
                    jpeg_quality=args.jpeg_quality,
                    shuffle_choices=args.shuffle_choices,
                    html_mode=args.html_mode,
                    output_name=html_name,
                    export_assets=args.export_html_assets,
                )
                print(f"HTML Output (Test {t}): {html_path}")
                if assets_dir:
                    print(f"HTML assets (images): {assets_dir}")

    print("Import in Moodle: Course → Question bank → Import → Moodle XML → upload the XML file(s).")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
