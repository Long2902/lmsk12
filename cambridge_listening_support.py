from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz

import cambridge_pdf2moodle_plus as c19

SECTION_EXPECTED_RANGES = {1: (1, 10), 2: (11, 20), 3: (21, 30), 4: (31, 40)}


def _page_top_text(doc: fitz.Document, pno: int, cache_dir: Path, prefix: str, lang: str = "eng") -> str:
    c19._ensure_dir(cache_dir / prefix)
    cache_file = cache_dir / prefix / f"page_{pno:04d}.txt"
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8", errors="ignore")
    txt = c19._ocr_page_region(doc, pno, (0.0, 0.0, 1.0, 0.36), zoom=1.8, lang=lang, psm=6)
    cache_file.write_text(txt, encoding="utf-8")
    return txt


def _page_native_text(doc: fitz.Document, pno: int, cache_dir: Path, prefix: str) -> str:
    c19._ensure_dir(cache_dir / prefix)
    cache_file = cache_dir / prefix / f"page_{pno:04d}.txt"
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8", errors="ignore")
    txt = doc[pno].get_text("text") or ""
    cache_file.write_text(txt, encoding="utf-8")
    return txt


def _extract_test_window(scans: List[c19.PageScan], test_num: int) -> Tuple[int, int]:
    pages = [s.pno for s in scans if s.test_num == test_num]
    if not pages:
        raise RuntimeError(f"Không tìm thấy trang nào thuộc Test {test_num}.")
    return min(pages), max(pages)


def _detect_reading_start(scans: List[c19.PageScan], test_num: int) -> Optional[int]:
    for s in scans:
        if s.test_num == test_num and re.search(r"READING\s+PASSAGE\s+1", s.header_text or "", flags=re.I):
            return s.pno
    return None


def _listening_window(scans: List[c19.PageScan], test_num: int) -> Tuple[int, int]:
    start, end = _extract_test_window(scans, test_num)
    reading_start = _detect_reading_start(scans, test_num)
    if reading_start is not None:
        end = min(end, reading_start - 1)
    return start, max(start, end)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()




def _trim_blob_to_current_test(blob: str, test_num: int) -> str:
    txt = (blob or "").replace("\r\n", "\n").replace("\r", "\n")
    next_test = re.search(rf"\bTest\s*{test_num + 1}\b", txt, flags=re.I)
    if next_test:
        txt = txt[:next_test.start()]
    return txt.strip()


def _extract_specific_section_blob(blob: str, sec: int, test_num: int) -> str:
    txt = _trim_blob_to_current_test(blob, test_num)
    start_m = re.search(rf"^\s*(?:#+\s*)?(?:SECTION|PART)\s+{sec}\b", txt, flags=re.I | re.M)
    if not start_m:
        return txt.strip()
    remainder = txt[start_m.start():]
    offset = start_m.end() - start_m.start()
    next_m = re.search(r"^\s*(?:#+\s*)?(?:SECTION|PART)\s+[1-4]\b", remainder[offset + 1:], flags=re.I | re.M)
    if next_m:
        remainder = remainder[:offset + 1 + next_m.start()]
    return remainder.strip()


def _page_question_hint_range(pdf_path: Path, pno: int, cache_dir: Path, lang: str = "eng") -> Optional[Tuple[int, int]]:
    try:
        groups = c19.parse_question_groups_from_pages(pdf_path, [pno], cache_dir / "listening_page_hints", lang=lang)
        qnums = sorted({q for g in groups for q in g.qnums})
        if qnums:
            return min(qnums), max(qnums)
    except Exception:
        return None
    return None


def _score_section_candidate(text: str, sec: int, qhint: Optional[Tuple[int, int]]) -> int:
    score = 0
    merged = text or ""
    a, b = SECTION_EXPECTED_RANGES[sec]
    if re.search(rf"\bSECTION\s+{sec}\b", merged, flags=re.I):
        score += 120
    if re.search(rf"Questions?\s*{a}\s*[\-–—]\s*{b}\b", merged, flags=re.I):
        score += 70
    if re.search(rf"\b{a}\s*[\-–—]\s*{b}\b", merged, flags=re.I):
        score += 20
    if qhint:
        qmin, qmax = qhint
        if a <= qmin <= b:
            score += 50
        if qmin == a:
            score += 35
        if qmax <= b:
            score += 10
    return score


def _detect_section_starts(pdf_path: Path, scans: List[c19.PageScan], test_num: int, cache_dir: Path, lang: str = "eng") -> Dict[int, int]:
    doc = fitz.open(str(pdf_path))
    try:
        start, end = _listening_window(scans, test_num)
        candidates: Dict[int, List[Tuple[int, int]]] = {1: [], 2: [], 3: [], 4: []}
        for pno in range(start, end + 1):
            header = scans[pno].header_text or ""
            top = _page_top_text(doc, pno, cache_dir, "listening_section_headers", lang=lang)
            native = _page_native_text(doc, pno, cache_dir, "listening_native_headers")[:2500]
            merged = "\n".join([x for x in [header, top, native] if x])
            qhint = _page_question_hint_range(pdf_path, pno, cache_dir, lang=lang)
            for sec in (1, 2, 3, 4):
                score = _score_section_candidate(merged, sec, qhint)
                if score > 0:
                    candidates[sec].append((pno, score))

        chosen: Dict[int, int] = {}
        prev = start - 1
        for sec in (1, 2, 3, 4):
            viable = [(p, s) for p, s in candidates[sec] if p > prev]
            if viable:
                viable.sort(key=lambda x: (-x[1], x[0]))
                best_score = viable[0][1]
                near_best = [p for p, s in viable if s >= best_score - 20]
                chosen_p = min(near_best)
                chosen[sec] = chosen_p
                prev = chosen_p

        # Fallback infer from question number hints even when explicit SECTION markers are weak.
        all_hints: List[Tuple[int, int, int]] = []
        for pno in range(start, end + 1):
            qhint = _page_question_hint_range(pdf_path, pno, cache_dir, lang=lang)
            if not qhint:
                continue
            qmin, qmax = qhint
            all_hints.append((pno, qmin, qmax))
        for sec in (1, 2, 3, 4):
            if sec in chosen:
                continue
            a, b = SECTION_EXPECTED_RANGES[sec]
            lower_bound = chosen.get(sec - 1, start - 1)
            upper_bound = chosen.get(sec + 1, end + 1)
            hint_pages = [p for p, qmin, qmax in all_hints if p > lower_bound and p < upper_bound and a <= qmin <= b]
            if hint_pages:
                chosen[sec] = min(hint_pages)

        # Last-resort monotonic fill using listening window order.
        prev = start - 1
        for sec in (1, 2, 3, 4):
            if sec not in chosen:
                chosen[sec] = max(prev + 1, start)
            prev = chosen[sec]

        ordered = {sec: chosen[sec] for sec in (1, 2, 3, 4)}
        prev = start - 1
        for sec in (1, 2, 3, 4):
            if ordered[sec] <= prev:
                ordered[sec] = prev + 1
            prev = ordered[sec]
        return ordered
    finally:
        doc.close()


def build_listening_bundles_for_test(pdf_path: Path, scans: List[c19.PageScan], test_num: int, cache_dir: Path, lang: str = "eng") -> List[c19.PassageBundle]:
    starts = _detect_section_starts(pdf_path, scans, test_num, cache_dir, lang=lang)
    _, end_window = _listening_window(scans, test_num)
    bundles: List[c19.PassageBundle] = []
    for sec in (1, 2, 3, 4):
        start = starts[sec]
        later_starts = [starts[x] for x in (1, 2, 3, 4) if x > sec and x in starts]
        end = min(later_starts) - 1 if later_starts else end_window
        end = max(start, end)
        section_pages = list(range(start, end + 1))
        groups = c19.parse_question_groups_from_pages(pdf_path, section_pages, cache_dir / "listening_bundle_groups", lang=lang)
        if groups:
            qnums = [q for g in groups for q in g.qnums]
            qrange = (min(qnums), max(qnums))
        else:
            qrange = SECTION_EXPECTED_RANGES[sec]
        bundles.append(c19.PassageBundle(test_num=test_num, passage_num=sec, qrange=qrange, passage_pages=[], question_pages=section_pages, groups=groups))
    return bundles


def _answer_key_candidate_pages(scans: List[c19.PageScan], test_num: int) -> List[int]:
    return [s.pno for s in scans if s.has_answer_keys and s.test_num == test_num]


def _extract_skill_block_from_answer_text(text: str, skill: str) -> str:
    txt = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not txt.strip():
        return ""
    skill_pat = r"LISTENING" if skill == "listening" else r"READING"
    other_pat = r"READING|WRITING|SPEAKING" if skill == "listening" else r"WRITING|LISTENING|SPEAKING"
    m = re.search(rf"\b{skill_pat}\b", txt, flags=re.I)
    if m:
        txt = txt[m.start():]
    m2 = re.search(rf"\n\s*(?:{other_pat})\b", txt, flags=re.I)
    if m2:
        txt = txt[:m2.start()]
    return txt.strip()


def load_answer_keys_for_test(
    pdf_path: Path,
    scans: List[c19.PageScan],
    test_num: int,
    cache_dir: Path,
    skill: str = "reading",
    provider: str = "llamaparse_markdown",
    lang: str = "eng",
    llama_api_key: str = "",
    llama_tier: str = "agentic",
) -> Tuple[Dict[int, List[str]], List[Tuple[Tuple[int, int], List[str]]]]:
    candidates = _answer_key_candidate_pages(scans, test_num)
    if not candidates:
        if skill == "reading":
            return c19.load_reading_answer_keys_for_test(pdf_path, scans, test_num, cache_dir, lang=lang)
        raise RuntimeError(f"Không tìm thấy Answer Keys cho Test {test_num}.")
    provider = provider or "ocr_text"
    if provider == "none":
        provider = "ocr_text"
    key_name = f"{skill}_key_t{test_num}"
    if provider == "ocr_text" and skill == "reading":
        return c19.load_reading_answer_keys_for_test(pdf_path, scans, test_num, cache_dir, lang=lang)
    raw = c19.extract_pages_text(pdf_path, candidates, cache_dir, provider, key_name, lang, llama_api_key, llama_tier)
    block = _extract_skill_block_from_answer_text(raw, skill)
    singles, pairs = c19.parse_reading_answer_key(block or raw)
    if not singles and not pairs and provider != "ocr_text":
        raw2 = c19.extract_pages_text(pdf_path, candidates, cache_dir, "ocr_text", key_name + "_ocr_fallback", lang, llama_api_key, llama_tier)
        block2 = _extract_skill_block_from_answer_text(raw2, skill)
        singles, pairs = c19.parse_reading_answer_key(block2 or raw2)
    return singles, pairs



def _parse_page_spec(spec: str) -> List[int]:
    pages: List[int] = []
    for chunk in re.split(r"[,;\s]+", (spec or "").strip()):
        if not chunk:
            continue
        m = re.match(r"^(\d+)\s*[-–—]\s*(\d+)$", chunk)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            lo, hi = (a, b) if a <= b else (b, a)
            pages.extend(range(lo, hi + 1))
        elif chunk.isdigit():
            pages.append(int(chunk))
    return sorted(set(p for p in pages if p >= 1))



def _format_page_range(pages_1based: List[int]) -> str:
    pages = sorted(dict.fromkeys(int(p) for p in (pages_1based or []) if int(p) >= 1))
    if not pages:
        return ""
    ranges: List[str] = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = p
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(ranges)


def _normalize_manual_section_pages(manual_pages: Optional[Any], scans_len: int) -> Dict[int, List[int]]:
    out: Dict[int, List[int]] = {}
    if not isinstance(manual_pages, dict):
        return out
    for raw_sec, raw_pages in manual_pages.items():
        try:
            sec = int(raw_sec)
        except Exception:
            continue
        if sec not in (1, 2, 3, 4):
            continue
        pages: List[int] = []
        if isinstance(raw_pages, str):
            pages = _parse_page_spec(raw_pages)
        elif isinstance(raw_pages, (list, tuple, set)):
            for p in raw_pages:
                try:
                    ip = int(p)
                except Exception:
                    continue
                if 1 <= ip <= scans_len:
                    pages.append(ip)
        out[sec] = sorted(dict.fromkeys([p for p in pages if 1 <= int(p) <= scans_len]))
    return out

def _find_audioscript_pages(pdf_path: Path, scans: List[c19.PageScan], test_num: int, cache_dir: Path, lang: str = "eng", manual_pages: Optional[List[int]] = None) -> List[int]:
    if manual_pages:
        valid = sorted(set(int(p) for p in manual_pages if 1 <= int(p) <= len(scans)))
        if valid:
            return [p - 1 for p in valid]
    doc = fitz.open(str(pdf_path))
    try:
        start_window, end_window = _extract_test_window(scans, test_num)
        start = None
        stop_markers: List[int] = []
        for pno in range(start_window, len(scans)):
            hdr = scans[pno].header_text or ""
            top = _page_top_text(doc, pno, cache_dir, "audioscript_headers", lang=lang)
            native = _page_native_text(doc, pno, cache_dir, "audioscript_native_headers")[:2200]
            merged = "\n".join([hdr, top, native])
            if start is None and re.search(r"AUDIO\s*SCRIPTS?|AUDIOSCRIPTS?", merged, flags=re.I):
                start = pno
                continue
            if start is None and pno > end_window and re.search(r"^\s*(?:#+\s*)?(?:SECTION|PART)\s*1\b", merged, flags=re.I | re.M):
                start = pno
                continue
            if start is not None and pno > start and re.search(r"ANSWER\s*KEYS?|READING\s+PASSAGE\s+1|TEST\s+\d+", merged, flags=re.I):
                stop_markers.append(pno)
                break
        if start is None:
            key_pages = _answer_key_candidate_pages(scans, test_num)
            start = (max(key_pages) + 1) if key_pages else (end_window + 1)
        next_test = [s.pno for s in scans if s.pno > start and s.test_num and s.test_num != test_num]
        candidates = stop_markers + next_test
        end = (min(candidates) - 1) if candidates else (len(scans) - 1)
        end = max(start, end)
        return list(range(start, end + 1))
    finally:
        doc.close()


def _clean_audioscript_part_conservative(part: str, sec: int, test_num: int) -> str:
    txt = (part or "").replace("\r\n", "\n").replace("\r", "\n")
    txt = re.sub(r"^\s*AUDIOSCRIPTS?\s*$", "", txt, flags=re.I | re.M)
    txt = re.sub(rf"^\s*Test\s*{test_num}\s*$", "", txt, flags=re.I | re.M)
    txt = re.sub(r"^\s*page\s+\d+\s*$", "", txt, flags=re.I | re.M)
    txt = re.sub(r"^\s*CAMBRIDGE.*$", "", txt, flags=re.I | re.M)
    txt = re.sub(rf"^\s*(?:#+\s*)?(?:SECTION|PART)\s+{sec}\b[:\- ]*", "", txt, flags=re.I | re.M)
    lines: List[str] = []
    for line in txt.split("\n"):
        ss = line.strip()
        if not ss:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if re.fullmatch(r"\d+", ss):
            continue
        if re.match(rf"^(?:SECTION|PART)\s+{sec}\b", ss, flags=re.I):
            continue
        if re.match(r"^(LISTENING|READING|WRITING|SPEAKING)\b", ss, flags=re.I):
            continue
        lines.append(ss)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    body = "\n".join(lines).strip()
    return f"## Section {sec} audioscript\n\n" + body if body else f"## Section {sec} audioscript"


def _normalize_audioscript_part(clean_part: str) -> str:
    txt = (clean_part or "").replace("\r\n", "\n").replace("\r", "\n")
    txt = re.sub(r"^##\s*Section\s+\d+\s+audioscript\s*", "", txt, flags=re.I).strip()
    lines = [ln.strip() for ln in txt.split("\n")]
    compact: List[str] = []
    for line in lines:
        if not line:
            if compact and compact[-1] != "":
                compact.append("")
            continue
        speaker_like = bool(re.match(r"^[A-Z][A-Za-z .'-]{0,40}:", line))
        if compact:
            prev = compact[-1]
            prev_speaker = bool(re.match(r"^[A-Z][A-Za-z .'-]{0,40}:", prev))
            joinable = prev and line and not speaker_like and not prev_speaker and prev[-1:] not in ".?!:]" and not re.match(r"^[\-*•·]\s+", line)
            if joinable:
                compact[-1] = prev.rstrip() + " " + line.lstrip()
                continue
        compact.append(line)
    dedup: List[str] = []
    for line in compact:
        if dedup and line == dedup[-1]:
            continue
        dedup.append(line)
    return "\n".join(dedup).strip()


def extract_listening_audioscripts_for_test(
    pdf_path: Path,
    scans: List[c19.PageScan],
    test_num: int,
    cache_dir: Path,
    provider: str = "llamaparse_markdown",
    lang: str = "eng",
    llama_api_key: str = "",
    llama_tier: str = "agentic",
    manual_pages: Optional[Any] = None,
) -> Dict[int, Dict[str, str]]:
    provider = provider or "native_pdf_text"
    if provider == "none":
        provider = "native_pdf_text"

    def pack(sec: int, part: str, status: str, page_range: str = "") -> Dict[str, str]:
        conservative = _clean_audioscript_part_conservative(part, sec, test_num)
        normalized = _normalize_audioscript_part(conservative)
        return {
            "audioscript_raw": (part or "").strip(),
            "audioscript_clean": conservative,
            "audioscript_normalized": normalized,
            "status": status,
            "page_range": page_range,
        }

    def split_parts(blob: str, status: str) -> Dict[int, Dict[str, str]]:
        parts = re.split(r"(?=^\s*(?:#+\s*)?(?:SECTION|PART)\s+[1-4]\b)", blob, flags=re.I | re.M)
        found: Dict[int, Dict[str, str]] = {}
        for part in parts:
            m = re.search(r"^\s*(?:#+\s*)?(?:SECTION|PART)\s+([1-4])\b", part, flags=re.I | re.M)
            if not m:
                continue
            sec = int(m.group(1))
            found[sec] = pack(sec, part, status)
        return found

    manual_section_pages = _normalize_manual_section_pages(manual_pages, len(scans))
    if manual_section_pages:
        out: Dict[int, Dict[str, str]] = {}
        for sec in (1, 2, 3, 4):
            sec_pages = manual_section_pages.get(sec, [])
            if not sec_pages:
                out[sec] = {"audioscript_raw": "", "audioscript_clean": f"## Section {sec} audioscript", "audioscript_normalized": "", "status": "missing", "page_range": ""}
                continue
            zero_pages = [p - 1 for p in sec_pages if 1 <= p <= len(scans)]
            raw_sec = c19.extract_pages_text(pdf_path, zero_pages, cache_dir, provider, f"audioscript_test{test_num}_s{sec}", lang, llama_api_key, llama_tier)
            out[sec] = pack(sec, (raw_sec or "").replace("\\r\\n", "\\n").replace("\\r", "\\n"), "manual_section", _format_page_range(sec_pages))
        return out

    pages = _find_audioscript_pages(pdf_path, scans, test_num, cache_dir, lang=lang, manual_pages=manual_pages if isinstance(manual_pages, list) else None)
    if not pages:
        return {sec: {"audioscript_raw": "", "audioscript_clean": f"## Section {sec} audioscript", "audioscript_normalized": "", "status": "missing", "page_range": ""} for sec in (1, 2, 3, 4)}

    raw = c19.extract_pages_text(pdf_path, pages, cache_dir, provider, f"audioscript_test{test_num}", lang, llama_api_key, llama_tier)
    txt = (raw or "").replace("\\r\\n", "\\n").replace("\\r", "\\n")
    out: Dict[int, Dict[str, str]] = split_parts(txt, "direct")

    if provider != "ocr_text":
        missing = [sec for sec in (1, 2, 3, 4) if sec not in out]
        if missing:
            raw2 = c19.extract_pages_text(pdf_path, pages, cache_dir, "ocr_text", f"audioscript_test{test_num}_ocr_fallback", lang, llama_api_key, llama_tier)
            txt2 = (raw2 or "").replace("\\r\\n", "\\n").replace("\\r", "\\n")
            out2 = split_parts(txt2, "ocr_fallback")
            for sec, payload in out2.items():
                out.setdefault(sec, payload)

    missing = [sec for sec in (1, 2, 3, 4) if sec not in out]
    if missing and pages:
        doc = fitz.open(str(pdf_path))
        try:
            page_blobs: List[Tuple[int, str]] = []
            for pno in pages:
                native = (doc[pno].get_text("text") or "").strip()
                top = _page_top_text(doc, pno, cache_dir, "audioscript_page_fallback", lang=lang)
                merged = "\n".join([x for x in [native, top] if x]).strip()
                if merged:
                    page_blobs.append((pno, merged))
            starts: Dict[int, int] = {}
            for pno, blob in page_blobs:
                m2 = re.search(r"^\s*(?:#+\s*)?(?:SECTION|PART)\s+([1-4])\b", blob, flags=re.I | re.M)
                if m2:
                    starts.setdefault(int(m2.group(1)), pno)
            if starts:
                ordered = sorted(starts.items(), key=lambda kv: kv[1])
                for idx, (sec, start_pno) in enumerate(ordered):
                    if sec in out and out[sec].get("audioscript_raw", "").strip():
                        continue
                    end_pno = ordered[idx + 1][1] - 1 if idx + 1 < len(ordered) else pages[-1]
                    chunk = "\n\n".join(blob for pno, blob in page_blobs if start_pno <= pno <= end_pno).strip()
                    if chunk:
                        out[sec] = pack(sec, chunk, "page_fallback", f"{start_pno+1}-{end_pno+1}")
        finally:
            doc.close()

    if len([sec for sec in out if out[sec].get("audioscript_raw", "").strip()]) < 4 and pages:
        doc = fitz.open(str(pdf_path))
        try:
            page_blobs: List[Tuple[int, str]] = []
            for pno in pages:
                blob = (doc[pno].get_text("text") or "").strip()
                if blob:
                    page_blobs.append((pno, blob))
            if page_blobs:
                for idx, sec in enumerate((1, 2, 3, 4)):
                    if out.get(sec, {}).get("audioscript_raw", "").strip():
                        continue
                    sidx = int(round(idx * len(page_blobs) / 4.0))
                    eidx = max(sidx, int(round((idx + 1) * len(page_blobs) / 4.0)) - 1)
                    eidx = min(eidx, len(page_blobs) - 1)
                    if sidx < len(page_blobs):
                        chunk = "\n\n".join(blob for _, blob in page_blobs[sidx:eidx+1]).strip()
                        if chunk:
                            out[sec] = pack(sec, chunk, "range_inferred", f"{page_blobs[sidx][0]+1}-{page_blobs[eidx][0]+1}")
        finally:
            doc.close()

    for sec in (1, 2, 3, 4):
        out.setdefault(sec, {"audioscript_raw": "", "audioscript_clean": f"## Section {sec} audioscript", "audioscript_normalized": "", "status": "missing", "page_range": ""})
        if isinstance(manual_pages, list) and manual_pages and not out[sec].get("page_range"):
            out[sec]["page_range"] = _format_page_range(manual_pages)
    return out

def prepare_listening_bundle_text_artifacts(
    pdf_path: Path,
    bundle: c19.PassageBundle,
    cache_dir: Path,
    transcript_by_section: Dict[int, Dict[str, str]],
    question_provider: str = "native_pdf_text",
    lang: str = "eng",
    group_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    llama_api_key: str = "",
    llama_tier: str = "agentic",
) -> Dict[str, str]:
    bid = c19.bundle_id(bundle)
    raw_q = c19.extract_pages_text(pdf_path, bundle.question_pages, cache_dir, question_provider, f"question_{bid}", lang, llama_api_key, llama_tier) if bundle.question_pages else ""
    eff_groups = c19.effective_groups_from_source(bundle, raw_q, group_overrides)
    transcript = transcript_by_section.get(bundle.passage_num, {}) or {}
    section_title = f"## Listening Section {bundle.passage_num}\n\nQuestions {bundle.qrange[0]}-{bundle.qrange[1]}"
    return {
        "skill": "listening",
        "question_source": raw_q,
        "question_markup": c19.build_auto_question_markdown(bundle, eff_groups, raw_q) if raw_q else "",
        "passage_text": section_title + "\n\n> Audioscript sẽ được dùng trong REVIEW của app. Khi export, transcript nên tách ra file companion riêng để General feedback chỉ giữ explanation.",
        "audioscript_raw": transcript.get("audioscript_raw", ""),
        "audioscript_clean": transcript.get("audioscript_clean", ""),
        "audioscript_normalized": transcript.get("audioscript_normalized", transcript.get("audioscript_clean", "")),
        "audioscript_status": transcript.get("status", "missing"),
        "audioscript_page_range": transcript.get("page_range", ""),
        "display_title": f"Listening - Part {bundle.passage_num}",
        "audio_title": "Audio",
    }
