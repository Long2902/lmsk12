#!/usr/bin/env python3
from __future__ import annotations

import base64
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

import cambridge_pdf2moodle as base

try:
    import markdown as md_lib
except Exception:
    md_lib = None

# re-export frequently used names
fitz = base.fitz
pytesseract = base.pytesseract
PageScan = base.PageScan
PassageBundle = base.PassageBundle
QuestionGroup = base.QuestionGroup
scan_pdf_headers = base.scan_pdf_headers
build_passages_for_test = base.build_passages_for_test
load_reading_answer_keys_for_test = base.load_reading_answer_keys_for_test
parse_question_groups_from_pages = base.parse_question_groups_from_pages
find_answer_key_page = base.find_answer_key_page
parse_reading_answer_key = base.parse_reading_answer_key
bundle_id = base.bundle_id
_ensure_dir = base._ensure_dir
_render_page = base._render_page
_img_to_jpeg_bytes = base._img_to_jpeg_bytes
_ocr_page_region = base._ocr_page_region
_qnums_to_fields = base._qnums_to_fields
_letters_range = base._letters_range
_maybe_fix_letter_token = base._maybe_fix_letter_token
_clean_key_answer = base._clean_key_answer
_escape_cloze_answer = base._escape_cloze_answer
extract_text_default = base._extract_text_from_pages
build_answer_context_base = base.build_answer_context
generate_explanations_gemini = base.generate_explanations_gemini
generate_keywords_gemini = base.generate_keywords_gemini
keywords_to_bar_html = base.keywords_to_bar_html
explanations_to_generalfeedback_html = base.explanations_to_generalfeedback_html

QUESTION_TYPE_OPTIONS = [
    "unknown",
    "tfng",
    "yesno",
    "choose_one_word",
    "letter_dropdown",
    "mc_letters",
    "choose_two_letters",
]


def group_key(group: QuestionGroup) -> str:
    if not group.qnums:
        return ""
    if group.group_type == "choose_two_letters" and len(group.qnums) == 2:
        a, b = sorted(group.qnums)
        return f"{a}-{b}"
    return f"{min(group.qnums)}-{max(group.qnums)}"


def default_meta_for_type(group_type: str) -> Dict[str, str]:
    if group_type == "mc_letters":
        return {"letters": "A-D"}
    if group_type in ("letter_dropdown", "choose_two_letters"):
        return {"letters": "A-G"}
    return {}


def parse_letters_spec(spec: str) -> Dict[str, str]:
    spec = (spec or "").strip().upper()
    if not spec:
        return {}
    m = re.match(r"^([A-Z])\s*[-–—]\s*([A-Z])$", spec)
    if m:
        return {"letters": f"{m.group(1)}-{m.group(2)}"}
    vals = [x.strip().upper() for x in re.split(r"[,;/\s]+", spec) if x.strip()]
    if vals:
        return {"letters_list": ",".join(vals)}
    return {}


def letters_for_group(group: QuestionGroup) -> List[str]:
    meta = group.meta or {}
    if "letters" in meta:
        val = (meta.get("letters") or "").strip().upper()
        if len(val) == 3 and val[1] == "-":
            return _letters_range(val[0], val[2])
    if "letters_list" in meta:
        vals = [x.strip().upper() for x in (meta.get("letters_list") or "").split(",") if x.strip()]
        if vals:
            return vals
    if group.group_type == "mc_letters":
        return list("ABCD")
    if group.group_type in ("letter_dropdown", "choose_two_letters"):
        return list("ABCDEFG")
    return []


def apply_group_overrides_to_groups(groups: List[QuestionGroup], overrides: Optional[Dict[str, Dict[str, Any]]]) -> List[QuestionGroup]:
    if not overrides:
        return list(groups)
    out: List[QuestionGroup] = []
    for g in groups:
        gk = group_key(g)
        ov = overrides.get(gk) if gk else None
        if not ov:
            out.append(g)
            continue
        gtype = ov.get("group_type") or g.group_type
        meta = dict(g.meta or {})
        meta.update(default_meta_for_type(gtype))
        meta.update(ov.get("meta") or {})
        out.append(QuestionGroup(qnums=list(g.qnums), group_type=gtype, meta=meta, raw_block=g.raw_block))
    return out


def infer_groups_from_question_source(raw_question_text: str, qrange: Optional[Tuple[int, int]] = None) -> List[QuestionGroup]:
    source = _clean_markdown_source(raw_question_text)
    if not source:
        return []
    groups: List[QuestionGroup] = []
    seen: set[str] = set()
    for sec in _split_question_sections(source):
        if not sec:
            continue
        block = "\n\n".join(sec).strip()
        hdr = _extract_question_header_range(sec[0])
        if not hdr:
            continue
        a, b = hdr
        if re.search(r"(?im)^\s*#*\s*Questions?\s+\d+\s+and\s+\d+\b", _strip_blockquote_prefix(sec[0])):
            qnums = [a, b]
        else:
            qnums = list(range(a, b + 1))
        if qrange and (max(qnums) < qrange[0] or min(qnums) > qrange[1]):
            continue
        gtype, meta = _detect_group_type_from_markdown_block(block)
        g = QuestionGroup(qnums=qnums, group_type=gtype, meta=meta, raw_block=_strip_blockquote_prefix(block))
        gk = group_key(g)
        if gk and gk not in seen:
            groups.append(g)
            seen.add(gk)
    groups.sort(key=lambda g: min(g.qnums) if g.qnums else 10**9)
    return groups


def merge_groups_from_raw_source(bundle: PassageBundle, raw_question_text: str) -> List[QuestionGroup]:
    merged: Dict[str, QuestionGroup] = {}
    for g in bundle.groups:
        gk = group_key(g)
        if gk:
            merged[gk] = g
    for g in infer_groups_from_question_source(raw_question_text, bundle.qrange):
        gk = group_key(g)
        if gk and gk not in merged:
            merged[gk] = g
    groups = sorted(merged.values(), key=lambda g: min(g.qnums) if g.qnums else 10**9)
    groups = _synthesize_missing_groups(bundle, groups, raw_question_text)
    return _attach_choice_texts_to_groups(groups, raw_question_text)


def effective_groups_from_source(bundle: PassageBundle, raw_question_text: str, overrides: Optional[Dict[str, Dict[str, Any]]]) -> List[QuestionGroup]:
    groups = apply_group_overrides_to_groups(merge_groups_from_raw_source(bundle, raw_question_text), overrides)
    return _attach_choice_texts_to_groups(groups, raw_question_text)


def _parse_choice_meta(group: QuestionGroup) -> Dict[str, str]:
    raw = (group.meta or {}).get("choice_text_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for k, v in (parsed or {}).items():
        key = str(k or "").strip().upper()
        val = re.sub(r"\s+", " ", str(v or "")).strip()
        if key and val:
            out[key] = val
    return out


def choice_texts_for_group(group: QuestionGroup) -> Dict[str, str]:
    return _parse_choice_meta(group)


def _parse_choice_meta_by_q(group: QuestionGroup) -> Dict[str, Dict[str, str]]:
    raw = (group.meta or {}).get("choice_text_by_q_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for q, mapping in (parsed or {}).items():
        if not isinstance(mapping, dict):
            continue
        inner: Dict[str, str] = {}
        for k, v in mapping.items():
            key = str(k or "").strip().upper()
            val = re.sub(r"\s+", " ", str(v or "")).strip()
            if key and val:
                inner[key] = val
        if inner:
            out[str(q)] = inner
    return out


def choice_texts_for_question(group: QuestionGroup, qnum: int) -> Dict[str, str]:
    return _parse_choice_meta_by_q(group).get(str(qnum), {})


def _normalize_choice_candidate_line(line: str) -> str:
    work = (line or "").strip()
    if not work:
        return ""
    work = re.sub(r"^[-*+•]\s+", "", work)
    work = re.sub(r"^\[\s*\]\s*", "", work)
    work = re.sub(r"[*_`]+", "", work)
    work = re.sub(r"\s+", " ", work).strip()
    return work


def _match_choice_line(line: str, allowed: set[str]) -> Optional[Tuple[str, str]]:
    work = _normalize_choice_candidate_line(line)
    if not work:
        return None
    table_cells = [c.strip() for c in work.strip("|").split("|") if c.strip()]
    if len(table_cells) >= 2 and len(table_cells[0]) == 1 and table_cells[0].upper() in allowed:
        return table_cells[0].upper(), re.sub(r"\s+", " ", table_cells[1]).strip()
    patterns = [
        r"^\(?([A-Z])\)?(?:\s*[\.)]|\s*[-–—:])\s+(.*\S)$",
        r"^\(?([A-Z])\)?\s+(.*\S)$",
    ]
    for pat in patterns:
        m = re.match(pat, work)
        if m and m.group(1).upper() in allowed:
            return m.group(1).upper(), re.sub(r"\s+", " ", m.group(2)).strip()
    return None


def _extract_choice_map_from_blocks(section_blocks: List[str], letters: List[str]) -> Dict[str, str]:
    allowed = {str(x or "").strip().upper() for x in letters if str(x or "").strip()}
    if not allowed:
        return {}
    lines: List[str] = []
    for blk in section_blocks or []:
        raw = _strip_blockquote_prefix(blk or "")
        lines.extend(raw.replace("\r", "").split("\n"))
    mapping: Dict[str, str] = {}
    current: Optional[str] = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            current = None
            continue
        matched = _match_choice_line(line, allowed)
        if matched:
            current, value = matched
            mapping[current] = value
            continue
        if current:
            if re.match(r"^(Question\s+\d+|Questions\s+\d+|Choose|Complete|Write\s+your\s+answers?|In\s+boxes|Reading Passage)\b", line, flags=re.I):
                current = None
                continue
            if re.match(r"^\d+[\.)]?\s+", line):
                current = None
                continue
            mapping[current] = (mapping.get(current, "") + " " + re.sub(r"\s+", " ", _normalize_choice_candidate_line(line))).strip()
    return {k: v for k, v in mapping.items() if v}


def _extract_choice_map_from_block(block: str, letters: List[str]) -> Dict[str, str]:
    return _extract_choice_map_from_blocks([block], letters)


def _strip_choice_lines_from_block(block: str, letters: List[str]) -> str:
    allowed = {str(x or "").strip().upper() for x in letters if str(x or "").strip()}
    if not allowed:
        return block
    out: List[str] = []
    skipping = False
    for raw_line in _strip_blockquote_prefix(block or "").replace("\r", "").split("\n"):
        line = raw_line.strip()
        if _match_choice_line(line or "", allowed):
            skipping = True
            continue
        if skipping:
            if not line:
                skipping = False
                continue
            if re.match(r"^(Question\s+\d+|Questions\s+\d+|Choose|Complete|Write\s+your\s+answers?|In\s+boxes|Reading Passage)\b", line, flags=re.I):
                skipping = False
                out.append(raw_line)
                continue
            if re.match(r"^\d+[\.)]?\s+", line):
                skipping = False
                out.append(raw_line)
                continue
            continue
        out.append(raw_line)
    return "\n".join(out).strip()


def _replace_choice_lines_with_placeholder(block: str, letters: List[str], label: str) -> str:
    allowed = {str(x or "").strip().upper() for x in letters if str(x or "").strip()}
    if not allowed:
        return block
    out: List[str] = []
    skipping = False
    inserted = False
    for raw_line in _strip_blockquote_prefix(block or "").replace("\r", "").split("\n"):
        line = raw_line.strip()
        if _match_choice_line(line or "", allowed):
            if not inserted:
                out.append(f"[[{label}]]")
                inserted = True
            skipping = True
            continue
        if skipping:
            if not line:
                skipping = False
                continue
            if re.match(r"^(Question\s+\d+|Questions\s+\d+|Choose|Complete|Write\s+your\s+answers?|In\s+boxes|Reading Passage)\b", line, flags=re.I):
                skipping = False
                out.append(raw_line)
                continue
            if re.match(r"^\d+[\.)]?\s+", line):
                skipping = False
                out.append(raw_line)
                continue
            continue
        out.append(raw_line)
    rendered = "\n".join(out).strip()
    if not inserted:
        rendered = (rendered + "\n\n" if rendered else "") + f"[[{label}]]"
    return rendered


def _attach_choice_texts_to_groups(groups: List[QuestionGroup], raw_question_text: str) -> List[QuestionGroup]:
    source = _clean_markdown_source(raw_question_text)
    if not source:
        return list(groups)
    sections = _split_question_sections(source)
    out: List[QuestionGroup] = []
    for g in groups:
        if g.group_type in ("mc_letters", "letter_dropdown"):
            letters = letters_for_group(g)
            by_q = _parse_choice_meta_by_q(g)
            sec = next((sec for sec in sections if _section_covers_group(sec, g)), None)
            qnums = sorted(g.qnums)
            q_block_idx = _find_q_block_indices(sec or [], qnums) if sec else {}
            first_item_idx = min(q_block_idx.values()) if q_block_idx else 0
            combined_block = (sec or [""])[first_item_idx] if sec and first_item_idx < len(sec) else source
            use_combined = len(q_block_idx) <= 1 and len(qnums) > 1 and bool(combined_block)
            for idx, q in enumerate(qnums):
                if str(q) in by_q and by_q[str(q)]:
                    continue
                snippet = ""
                if use_combined:
                    next_q = qnums[idx + 1] if idx + 1 < len(qnums) else None
                    snippet = _extract_body_from_combined_items_block(combined_block, q, next_q)
                elif sec and q in q_block_idx:
                    q_blocks = _blocks_for_q_in_section(sec, q_block_idx, qnums, q)
                    if q_blocks:
                        snippet = "\n\n".join(q_blocks)
                if not snippet:
                    next_q = qnums[idx + 1] if idx + 1 < len(qnums) else None
                    snippet = _extract_snippet_for_q(source, q, next_q)
                    if snippet and not re.match(rf"^\s*{q}\b", snippet):
                        snippet = f"{q}. {snippet}"
                choice_map = _extract_choice_map_from_blocks([snippet], letters) if snippet else {}
                if choice_map:
                    by_q[str(q)] = choice_map
            if by_q:
                meta = dict(g.meta or {})
                meta["choice_text_by_q_json"] = json.dumps(by_q, ensure_ascii=False)
                promoted_type = "mc_letters" if g.group_type == "letter_dropdown" else g.group_type
                g = QuestionGroup(qnums=list(g.qnums), group_type=promoted_type, meta=meta, raw_block=g.raw_block)
            out.append(g)
            continue
        if g.group_type == "choose_two_letters":
            letters = letters_for_group(g)
            choice_map = choice_texts_for_group(g)
            sec = next((sec for sec in sections if _section_covers_group(sec, g)), None)
            if not choice_map and sec and letters:
                choice_map = _extract_choice_map_from_blocks(sec, letters)
            if letters and (not choice_map or len(choice_map) < min(2, len(letters))):
                fallback_map = _extract_choice_map_from_blocks([source], letters)
                if fallback_map:
                    merged_map = dict(fallback_map)
                    merged_map.update(choice_map or {})
                    choice_map = merged_map
            if choice_map:
                meta = dict(g.meta or {})
                meta["choice_text_json"] = json.dumps(choice_map, ensure_ascii=False)
                g = QuestionGroup(qnums=list(g.qnums), group_type=g.group_type, meta=meta, raw_block=g.raw_block)
            out.append(g)
            continue
        out.append(g)
    return out


def _contiguous_runs(nums: List[int]) -> List[List[int]]:
    if not nums:
        return []
    ordered = sorted(set(int(x) for x in nums))
    runs: List[List[int]] = [[ordered[0]]]
    for n in ordered[1:]:
        if n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    return runs


def _guess_group_type_from_global_source(source: str, qnums: List[int]) -> Tuple[str, Dict[str, str]]:
    txt = _strip_blockquote_prefix(source or "")
    gtype, meta = _detect_group_type_from_markdown_block(txt)
    if gtype != "unknown":
        return gtype, meta
    up = txt.upper()
    m = re.search(r"WRITE\s+THE\s+CORRECT\s+LETTER[^\n]*\b([A-Z])\s*[-–—]\s*([A-Z])\b", txt, flags=re.I)
    if m:
        return "letter_dropdown", {"letters": f"{m.group(1).upper()}-{m.group(2).upper()}"}
    if "CHOOSE TWO LETTERS" in up or "CHOOSE TWO" in up:
        return "choose_two_letters", {"letters": "A-G"}
    if "TRUE" in up and "FALSE" in up and "NOT GIVEN" in up:
        return "tfng", {}
    if "YES" in up and "NO" in up and "NOT GIVEN" in up:
        return "yesno", {}
    if re.search(r"\b(ONE WORD ONLY|NO MORE THAN ONE WORD|NO MORE THAN TWO WORDS|ONE WORD AND/?OR A NUMBER|TWO WORDS AND/?OR A NUMBER|NO MORE THAN THREE WORDS|COMPLETE THE NOTES|COMPLETE THE TABLE|COMPLETE THE FORM|COMPLETE THE SUMMARY|COMPLETE THE SENTENCES?)\b", up):
        return "choose_one_word", {}
    if re.search(r"\b(LABEL THE MAP|LABEL THE PLAN|LABEL THE DIAGRAM|MAP BELOW|PLAN BELOW|DIAGRAM BELOW)\b", up):
        m = re.search(r"\b([A-Z])\s*[-–—]\s*([A-Z])\b", txt)
        meta = {"letters": f"{m.group(1).upper()}-{m.group(2).upper()}"} if m else {"letters": "A-H"}
        return "letter_dropdown", meta
    return "choose_one_word", {}


def _synthesize_missing_groups(bundle: PassageBundle, existing: List[QuestionGroup], source: str) -> List[QuestionGroup]:
    if not bundle.qrange:
        return list(existing)
    q_start, q_end = bundle.qrange
    covered = {q for g in existing for q in g.qnums}
    missing = [q for q in range(q_start, q_end + 1) if q not in covered]
    if not missing:
        return list(existing)
    out = list(existing)
    gtype, meta = _guess_group_type_from_global_source(source, missing)
    for run in _contiguous_runs(missing):
        rk = f"{run[0]}-{run[-1]}"
        out.append(QuestionGroup(qnums=list(run), group_type=gtype, meta=dict(meta), raw_block=f"[auto] Questions {rk}"))
    return sorted(out, key=lambda g: min(g.qnums) if g.qnums else 10**9)


def apply_group_overrides(bundle: PassageBundle, overrides: Optional[Dict[str, Dict[str, Any]]]) -> List[QuestionGroup]:
    return apply_group_overrides_to_groups(list(bundle.groups), overrides)


def _detect_group_type_from_markdown_block(block: str) -> Tuple[str, Dict[str, str]]:
    clean = _strip_blockquote_prefix(block or "")
    clean = clean.replace("*", "")
    clean = re.sub(r"^\s*#{1,6}\s*", "", clean, flags=re.MULTILINE)
    return base._detect_group_type(clean)


# -----------------------------
# Text extraction
# -----------------------------

def _extract_pages_native_text(pdf_path: Path, pages: List[int]) -> str:
    doc = fitz.open(str(pdf_path))
    try:
        out = []
        for p in pages:
            out.append((doc[p].get_text("text") or "").strip())
        return "\n\n".join([x for x in out if x]).strip()
    finally:
        doc.close()


def _extract_pages_ocr_text(pdf_path: Path, pages: List[int], lang: str = "eng") -> str:
    doc = fitz.open(str(pdf_path))
    try:
        out = []
        for p in pages:
            out.append(_ocr_page_region(doc, p, (0.0, 0.0, 1.0, 0.98), zoom=1.8, lang=lang, psm=6))
        return "\n\n".join([x for x in out if x]).strip()
    finally:
        doc.close()


def _subset_pdf(pdf_path: Path, pages: List[int], out_path: Path) -> Path:
    doc_in = fitz.open(str(pdf_path))
    doc_out = fitz.open()
    try:
        for p in pages:
            doc_out.insert_pdf(doc_in, from_page=p, to_page=p)
        doc_out.save(str(out_path))
    finally:
        doc_out.close()
        doc_in.close()
    return out_path


def _coerce_llamaparse_markdown(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n\n".join([_coerce_llamaparse_markdown(v) for v in value if _coerce_llamaparse_markdown(v)]).strip()
    if isinstance(value, dict):
        for key in ("markdown", "md", "text", "content", "value"):
            if key in value:
                out = _coerce_llamaparse_markdown(value.get(key))
                if out:
                    return out
        for key in ("pages", "items", "chunks", "results", "documents", "nodes"):
            if key in value:
                out = _coerce_llamaparse_markdown(value.get(key))
                if out:
                    return out
        for key in ("result", "result_data", "data", "job_result"):
            if key in value:
                out = _coerce_llamaparse_markdown(value.get(key))
                if out:
                    return out
        nested = []
        seen = set()
        for child in value.values():
            if isinstance(child, (str, list, dict)):
                out = _coerce_llamaparse_markdown(child)
                if out and out not in seen:
                    nested.append(out)
                    seen.add(out)
        return "\n\n".join(nested).strip()
    return str(value).strip()


def _extract_pages_llamaparse_markdown(
    pdf_path: Path,
    pages: List[int],
    cache_dir: Path,
    api_key: str,
    tier: str = "agentic",
    version: str = "latest",
    timeout_seconds: int = 300,
) -> str:
    if not api_key:
        raise RuntimeError("Thiếu LLAMA_CLOUD_API_KEY.")
    _ensure_dir(cache_dir / "llamaparse")
    subset_name = f"subset_{min(pages)}_{max(pages)}_{len(pages)}.pdf"
    subset_pdf = cache_dir / "llamaparse" / subset_name
    cache_md = cache_dir / "llamaparse" / f"{subset_name}.md"
    if cache_md.exists():
        return cache_md.read_text(encoding="utf-8", errors="ignore")
    if not subset_pdf.exists():
        _subset_pdf(pdf_path, pages, subset_pdf)

    headers = {"Authorization": f"Bearer {api_key}"}
    config = {
        "tier": tier,
        "version": version,
        "output_options": {
            "markdown": {
                "annotate_links": False,
                "tables": {"compact_markdown_tables": False, "merge_continued_tables": True},
            },
            "images_to_save": [],
        },
    }
    with open(subset_pdf, "rb") as fh:
        resp = requests.post(
            "https://api.cloud.llamaindex.ai/api/v2/parse/upload",
            headers=headers,
            files={"file": (subset_pdf.name, fh, "application/pdf")},
            data={"configuration": json.dumps(config)},
            timeout=60,
        )
    resp.raise_for_status()
    payload = resp.json()
    job_id = payload.get("job", {}).get("id") or payload.get("id") or payload.get("parse_job", {}).get("id") or payload.get("job_id")
    if not job_id:
        raise RuntimeError(f"Không lấy được job_id từ LlamaParse: {payload}")

    deadline = time.time() + timeout_seconds
    last_payload = None
    while time.time() < deadline:
        res = requests.get(
            f"https://api.cloud.llamaindex.ai/api/v2/parse/{job_id}?expand=markdown",
            headers=headers,
            timeout=60,
        )
        res.raise_for_status()
        last_payload = res.json()
        job = last_payload.get("job") or {}
        status = (job.get("status") or "").upper()
        if status == "COMPLETED":
            md = _coerce_llamaparse_markdown(last_payload)
            if not md:
                raise RuntimeError(f"LlamaParse hoàn tất nhưng không rút ra được markdown text từ response: {last_payload}")
            cache_md.write_text(md, encoding="utf-8")
            return md
        if status in {"FAILED", "CANCELLED"}:
            raise RuntimeError(job.get("error_message") or f"LlamaParse job {job_id} thất bại")
        time.sleep(2.0)
    raise RuntimeError(f"LlamaParse timeout. Response cuối: {last_payload}")


def extract_pages_text(
    pdf_path: Path,
    pages: List[int],
    cache_dir: Path,
    provider: str,
    cache_name: str,
    lang: str = "eng",
    llama_api_key: str = "",
    llama_tier: str = "agentic",
) -> str:
    _ensure_dir(cache_dir / "text")
    cache_ext = "md" if provider == "llamaparse_markdown" else "txt"
    cache_file = cache_dir / "text" / f"{cache_name}_{provider}.{cache_ext}"
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8", errors="ignore")
    if provider == "none":
        return ""
    if provider == "native_pdf_text":
        txt = _extract_pages_native_text(pdf_path, pages)
    elif provider == "ocr_text":
        txt = _extract_pages_ocr_text(pdf_path, pages, lang=lang)
    elif provider == "llamaparse_markdown":
        txt = _extract_pages_llamaparse_markdown(pdf_path, pages, cache_dir, api_key=llama_api_key, tier=llama_tier)
    else:
        raise ValueError(f"Unknown text provider: {provider}")
    cache_file.write_text(txt, encoding="utf-8")
    return txt


# -----------------------------
# Markdown / question markup generation
# -----------------------------

def _clean_markdown_source(raw: str) -> str:
    raw = (raw or "").replace("\r", "")
    raw = raw.replace("\xa0", " ")
    raw = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _split_markdown_blocks(raw: str) -> List[str]:
    raw = _clean_markdown_source(raw)
    if not raw:
        return []
    return [b.strip() for b in re.split(r"\n\s*\n", raw) if b.strip()]


def _strip_blockquote_prefix(block: str) -> str:
    return "\n".join(re.sub(r"^\s*>\s?", "", ln) for ln in (block or "").splitlines())


def _quote_block(block: str) -> str:
    clean = _strip_blockquote_prefix((block or "").strip())
    if not clean:
        return ""
    lines = []
    for raw_ln in clean.splitlines() or [clean]:
        ln = re.sub(r"^\s*#{1,6}\s*", "", raw_ln).rstrip()
        lines.append(("> " + ln) if ln.strip() else ">")
    return "\n".join(lines)


def _extract_question_header_range(block: str) -> Optional[Tuple[int, int]]:
    block = _strip_blockquote_prefix(block or "")
    m = re.search(r"(?im)^\s*#*\s*Questions?\s+(\d+)\s*(?:[-–—]|and)\s*(\d+)\b", block)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return (min(a, b), max(a, b))


def _block_contains_q(block: str, q: int) -> bool:
    text = block or ""
    patterns = [
        rf"\*\*{q}\*\*",
        rf"(?m)^\s*[-*•·]?\s*\(?{q}\)?[.)]?\s+",
        rf"(?<!\d){q}(?!\d)\s*(?:_{{2,}}|\.{{2,}}|\[\s*\]|□+)",
        rf"(?<!\d){q}(?!\d)[ \t]{{2,}}[A-Za-z]",
    ]
    return any(re.search(p, text) for p in patterns)


def _extract_statement_body(block: str, qnum: int) -> str:
    text = _strip_blockquote_prefix(block or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(rf"^[-*•·]?\s*\(?{qnum}\)?[.)]?\s*", "", text).strip()
    return text


def _extract_body_from_combined_items_block(block: str, qnum: int, next_q: Optional[int]) -> str:
    text = _strip_blockquote_prefix(block or "")
    text = text.replace("\r", "")
    start_pat = rf"(?:\*\*{qnum}\*\*|(?<!\d){qnum}(?!\d))"
    if next_q is not None:
        end_pat = rf"(?=(?:\*\*{next_q}\*\*|(?<!\d){next_q}(?!\d))|$)"
    else:
        end_pat = r"$"
    m = re.search(start_pat + r"\s*(.*?)" + end_pat, text, flags=re.DOTALL)
    if not m:
        return ""
    body = re.sub(r"\s+", " ", m.group(1)).strip()
    return body


def _format_question_item(body: str, label: str, qnum: int, group_type: str) -> str:
    body = re.sub(r"\s+", " ", (body or "")).strip()
    placeholder = f"[[{label}]]"
    if group_type in ("tfng", "yesno"):
        return f"**{qnum}.** {body} {placeholder}".strip() if body else f"**{qnum}.** {placeholder}"
    if group_type in ("mc_letters", "letter_dropdown"):
        return f"**{qnum}.** {placeholder} {body}".strip() if body else f"**{qnum}.** {placeholder}"
    return f"**{qnum}.** {body} {placeholder}".strip() if body else f"**{qnum}.** {placeholder}"


def _is_markdown_table_block(block: str) -> bool:
    lines = [ln.rstrip() for ln in _strip_blockquote_prefix(block or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    for i in range(len(lines) - 1):
        a = lines[i].strip()
        b = lines[i + 1].strip()
        if "|" not in a or "|" not in b:
            continue
        if re.match(r"^\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?$", b):
            return True
    return False


def _render_mixed_blocks(blocks: List[str]) -> str:
    out: List[str] = []
    for blk in blocks:
        clean = _strip_blockquote_prefix((blk or "").strip())
        if not clean:
            continue
        if _is_markdown_table_block(clean):
            out.append(clean)
        else:
            out.append(_quote_block(clean))
    return "\n\n".join(out)


def _transform_table_block_for_q(block: str, qnum: int, label: str, group_type: str) -> str:
    placeholder = f"[[{label}]]"
    text = _strip_blockquote_prefix(block or "")
    if placeholder in text:
        return text
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not re.search(rf"(?<!\d){qnum}(?!\d)", line):
            continue
        if re.match(r"^\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?$", line.strip()):
            continue
        new_line = line
        patterns = [
            (rf"(\*\*{qnum}\*\*)\s*(?:_{{2,}}|\.{{2,}}|□+)?", rf"\1 {placeholder} "),
            (rf"(\|\s*)(\*\*{qnum}\*\*)(\s*\|)", rf"\1\2 {placeholder}\3"),
            (rf"(\|\s*)({qnum})(\s*\|)", rf"\1**\2** {placeholder}\3"),
            (rf"(?<!\d)({qnum})(?!\d)\s*(?:_{{2,}}|\.{{2,}}|□+)", rf"**\1** {placeholder} "),
            (rf"(?<!\d)({qnum})(?!\d)", rf"**\1** {placeholder}"),
        ]
        replaced = False
        for pat, repl in patterns:
            newer, n = re.subn(pat, repl, new_line, count=1)
            if n:
                new_line = newer
                replaced = True
                break
        if not replaced and group_type in ("tfng", "yesno"):
            new_line = new_line.rstrip() + f" {placeholder}"
        lines[i] = re.sub(r"[ \t]{2,}", " ", new_line)
        break
    return "\n".join(lines)


def _transform_inline_block_for_q(block: str, qnum: int, label: str, group_type: str = "choose_one_word") -> str:
    if _is_markdown_table_block(block):
        return _transform_table_block_for_q(block, qnum, label, group_type)
    placeholder = f"[[{label}]]"
    if placeholder in block:
        return block
    text = block
    patterns = [
        (rf"(\*\*{qnum}\*\*)\s*(?:_{{2,}}|\.{{2,}}|□+)?", rf"\1 {placeholder} "),
        (rf"(^|\n)(\s*[-*•·]?\s*)({qnum})(\s*(?:_{{2,}}|\.{{2,}}|□+)?)", rf"\1\2**{qnum}** {placeholder} "),
        (rf"(?<!\d)({qnum})(?!\d)\s*(?:_{{2,}}|\.{{2,}}|□+)", rf"**{qnum}** {placeholder} "),
        (rf"(?<!\d)({qnum})(?!\d)", rf"**{qnum}** {placeholder}"),
    ]
    for pat, repl in patterns:
        new_text, n = re.subn(pat, repl, text, count=1, flags=re.MULTILINE)
        if n:
            return re.sub(r"[ \t]{2,}", " ", new_text)
    return text


def _split_question_sections(raw: str) -> List[List[str]]:
    blocks = _split_markdown_blocks(raw)
    if not blocks:
        return []
    sections: List[List[str]] = []
    current: List[str] = []
    for blk in blocks:
        if _extract_question_header_range(blk) and current:
            sections.append(current)
            current = [blk]
        else:
            current.append(blk)
    if current:
        sections.append(current)
    return sections


def _section_covers_group(section_blocks: List[str], group: QuestionGroup) -> bool:
    if not section_blocks:
        return False
    hdr = _extract_question_header_range(section_blocks[0])
    gmin, gmax = min(group.qnums), max(group.qnums)
    if hdr and gmin >= hdr[0] and gmax <= hdr[1]:
        return True
    return _block_contains_q("\n\n".join(section_blocks), gmin)


def _find_q_block_indices(section_blocks: List[str], qnums: List[int]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    cursor = 0
    for q in sorted(qnums):
        for idx in range(cursor, len(section_blocks)):
            if _block_contains_q(section_blocks[idx], q):
                out[q] = idx
                cursor = idx + 1
                break
    return out


def _blocks_for_q_in_section(section_blocks: List[str], q_block_idx: Dict[int, int], qnums: List[int], q: int) -> List[str]:
    if not section_blocks or q not in q_block_idx:
        return []
    start = q_block_idx[q]
    later = [q_block_idx[qq] for qq in sorted(qnums) if qq > q and qq in q_block_idx and q_block_idx[qq] > start]
    end = min(later) if later else len(section_blocks)
    if end <= start:
        end = min(start + 1, len(section_blocks))
    return [b for b in section_blocks[start:end] if (b or "").strip()]


def _render_list_section(section_blocks: List[str], group: QuestionGroup) -> str:
    qnums = sorted(group.qnums)
    q_block_idx = _find_q_block_indices(section_blocks, qnums)
    first_item_idx = min(q_block_idx.values()) if q_block_idx else len(section_blocks)
    intro_blocks = section_blocks[:first_item_idx]
    parts: List[str] = []
    if intro_blocks:
        intro_render = intro_blocks
        if group.group_type == "mc_letters":
            letters = letters_for_group(group)
            intro_render = [_strip_choice_lines_from_block(b, letters) for b in intro_blocks]
        parts.append("\n\n".join(_quote_block(b) for b in intro_render if b.strip()))

    combined_block = section_blocks[first_item_idx] if first_item_idx < len(section_blocks) else ""
    use_combined = len(q_block_idx) <= 1 and len(qnums) > 1 and bool(combined_block)

    if group.group_type == "mc_letters":
        letters = letters_for_group(group)
        section_source = "\n\n".join(section_blocks)
        for idx, q in enumerate(qnums):
            snippet = ""
            if use_combined:
                next_q = qnums[idx + 1] if idx + 1 < len(qnums) else None
                body = _extract_body_from_combined_items_block(combined_block, q, next_q)
                snippet = (f"{q}. {body}" if body else "")
            elif q in q_block_idx:
                q_blocks = _blocks_for_q_in_section(section_blocks, q_block_idx, qnums, q)
                if q_blocks:
                    snippet = "\n\n".join(q_blocks)
            if not snippet:
                next_q = qnums[idx + 1] if idx + 1 < len(qnums) else None
                snippet = _extract_snippet_for_q(section_source, q, next_q)
                if snippet and not re.match(rf"^\s*{q}\b", snippet):
                    snippet = f"{q}. {snippet}"
            if snippet:
                parts.append(_quote_block(_replace_choice_lines_with_placeholder(snippet, letters, str(q))))
            else:
                parts.append(f"**{q}.** [[{q}]]")
        return "\n\n".join([p for p in parts if p.strip()])

    for idx, q in enumerate(qnums):
        body = ""
        if use_combined:
            next_q = qnums[idx + 1] if idx + 1 < len(qnums) else None
            body = _extract_body_from_combined_items_block(combined_block, q, next_q)
        elif q in q_block_idx:
            body = _extract_statement_body(section_blocks[q_block_idx[q]], q)
        parts.append(_format_question_item(body, str(q), q, group.group_type))
    return "\n\n".join([p for p in parts if p.strip()])

def _section_has_markdown_table(section_blocks: List[str]) -> bool:
    return any(_is_markdown_table_block(b) for b in section_blocks if b.strip())


def _render_table_section(section_blocks: List[str], group: QuestionGroup) -> str:
    q_sorted = sorted(group.qnums)
    out_blocks: List[str] = []
    for blk in section_blocks:
        new_blk = blk
        if _is_markdown_table_block(blk):
            for q in q_sorted:
                if _block_contains_q(new_blk, q):
                    new_blk = _transform_table_block_for_q(new_blk, q, str(q), group.group_type or "choose_one_word")
        else:
            for q in q_sorted:
                if not _extract_question_header_range(new_blk) and _block_contains_q(new_blk, q):
                    new_blk = _transform_inline_block_for_q(new_blk, q, str(q), group.group_type or "choose_one_word")
        out_blocks.append(new_blk)
    body = _render_mixed_blocks([b for b in out_blocks if b.strip()])
    if group.group_type == "choose_two_letters" and len(group.qnums) == 2:
        label = f"{min(group.qnums)}-{max(group.qnums)}"
        tail = f"**Questions {label}.** [[{label}]]"
        return (body + "\n\n" + tail).strip() if body else tail
    return body


def _render_inline_section(section_blocks: List[str], qnums: List[int], group_type: str = "choose_one_word") -> str:
    q_sorted = sorted(qnums)
    out_blocks: List[str] = []
    for blk in section_blocks:
        new_blk = blk
        for q in q_sorted:
            if not _extract_question_header_range(new_blk) and _block_contains_q(new_blk, q):
                new_blk = _transform_inline_block_for_q(new_blk, q, str(q), group_type)
        out_blocks.append(new_blk)
    return _render_mixed_blocks([b for b in out_blocks if b.strip()])


def _render_choose_two_section(section_blocks: List[str], group: QuestionGroup) -> str:
    label = f"{min(group.qnums)}-{max(group.qnums)}"
    letters = letters_for_group(group)
    blocks: List[str] = []
    inserted = False
    for blk in [b for b in section_blocks if b.strip()]:
        if letters and not inserted and _extract_choice_map_from_blocks([blk], letters):
            blocks.append(_replace_choice_lines_with_placeholder(blk, letters, label))
            inserted = True
        else:
            blocks.append(_strip_choice_lines_from_block(blk, letters) if letters else blk)
    body = _render_mixed_blocks([b for b in blocks if b.strip()])
    if inserted:
        return body
    answer_line = f"[[{label}]]"
    return (body + "\n\n" + answer_line).strip() if body else answer_line

def _render_group_from_section(section_blocks: List[str], group: QuestionGroup) -> str:
    gtype = group.group_type or "unknown"
    if _section_has_markdown_table(section_blocks):
        return _render_table_section(section_blocks, group)
    if gtype in ("tfng", "yesno", "mc_letters", "letter_dropdown"):
        return _render_list_section(section_blocks, group)
    if gtype == "choose_two_letters" and len(group.qnums) == 2:
        return _render_choose_two_section(section_blocks, group)
    return _render_inline_section(section_blocks, group.qnums, gtype)


def _should_skip_question_line(line: str) -> bool:
    line = (line or "").strip()
    if not line:
        return True
    if re.match(r"^Questions\s+\d+\s*(?:[-–—]|and)\s*\d+\b", line, flags=re.IGNORECASE):
        return True
    if re.match(r"^(Complete|Choose|Write your answers|Write your answer|In boxes|Do the following statements|Which TWO|Which of the following|Reading Passage)\b", line, flags=re.IGNORECASE):
        return True
    return False


def _line_starts_question(line: str, q: int) -> bool:
    line = (line or "").strip()
    if _should_skip_question_line(line):
        return False
    patterns = [
        rf"^[-*•·]?\s*{q}[\.)]?\s+",
        rf"^[-*•·]?\s*\(?{q}\)?\s+",
        rf"^[-*•·]?\s*{q}(?=\s)",
        rf"^[-*•·]?\s*.*?\b{q}\b\s*(?:_{{2,}}|\.+|\[\s*\]|□+)",
    ]
    return any(re.search(p, line) for p in patterns)


def _start_index_for_q_in_line(line: str, q: int) -> int:
    line = line or ""
    patterns = [
        rf"^[-*•·]?\s*{q}[\.)]?\s+",
        rf"^[-*•·]?\s*\(?{q}\)?\s+",
        rf"\b{q}\b\s*(?:_{{2,}}|\.+|\[\s*\]|□+)",
    ]
    best = -1
    for pat in patterns:
        m = re.search(pat, line)
        if m:
            idx = m.start()
            best = idx if best == -1 else min(best, idx)
    return best


def _sanitize_snippet(text: str, q: int, next_q: Optional[int]) -> str:
    text = _normalize_camfmt_blocks((text or "").strip())
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if next_q is not None:
        split = re.split(rf"(?<!\d){next_q}(?!\d)", text, maxsplit=1)
        if split:
            text = split[0].strip()
    return text


def _extract_snippet_for_q(raw: str, q: int, next_q: Optional[int]) -> str:
    lines = [x.rstrip() for x in _clean_markdown_source(raw).splitlines()]
    for line in lines:
        if _should_skip_question_line(line):
            continue
        if _line_starts_question(line, q):
            idx = _start_index_for_q_in_line(line, q)
            if idx > 0:
                line = line[idx:]
            return _sanitize_snippet(line.strip(), q, next_q)
    for line in lines:
        if _should_skip_question_line(line):
            continue
        m = re.search(rf"(?<!\d){q}(?!\d)", line)
        if m:
            return _sanitize_snippet(line[m.start():].strip(), q, next_q)
    return ""


def _render_group_fallback(group: QuestionGroup, source: str) -> str:
    if group.group_type == "mc_letters":
        letters = letters_for_group(group)
        blocks = []
        q_sorted = sorted(group.qnums)
        for idx, q in enumerate(q_sorted):
            next_q = q_sorted[idx + 1] if idx + 1 < len(q_sorted) else None
            snippet = _extract_snippet_for_q(source, q, next_q)
            if snippet and not re.match(rf"^\s*{q}\b", snippet):
                snippet = f"{q}. {snippet}"
            blocks.append(_quote_block(_replace_choice_lines_with_placeholder(snippet or f"{q}", letters, str(q))))
        return "\n\n".join([x for x in blocks if x.strip()])
    if group.group_type in ("tfng", "yesno", "letter_dropdown"):
        lines = []
        for q in sorted(group.qnums):
            snippet = _extract_snippet_for_q(source, q, q + 1)
            body = _extract_statement_body(snippet, q)
            lines.append(_format_question_item(body, str(q), q, group.group_type))
        return "\n".join([x for x in lines if x.strip()])
    if group.group_type == "choose_two_letters" and len(group.qnums) == 2:
        label = f"{min(group.qnums)}-{max(group.qnums)}"
        letters = letters_for_group(group)
        rendered = _replace_choice_lines_with_placeholder(source, letters, label)
        return _quote_block(rendered) if rendered else f"[[{label}]]"
    lines = [f"> Questions {min(group.qnums)}-{max(group.qnums)}"]
    for q in sorted(group.qnums):
        lines.append(f"> **{q}** [[{q}]]")
    return "\n".join(lines)

def build_auto_question_markdown(bundle: PassageBundle, effective_groups: List[QuestionGroup], raw_question_text: str) -> str:
    source = _clean_markdown_source(raw_question_text)
    if not source:
        return ""
    groups_sorted = sorted(effective_groups, key=lambda g: min(g.qnums) if g.qnums else 10**9)
    sections = _split_question_sections(source)
    rendered: List[str] = []
    section_cursor = 0
    used_sections = set()
    for group in groups_sorted:
        matched_idx: Optional[int] = None
        for idx in range(section_cursor, len(sections)):
            if _section_covers_group(sections[idx], group):
                matched_idx = idx
                break
        if matched_idx is not None:
            rendered.append(_render_group_from_section(sections[matched_idx], group))
            used_sections.add(matched_idx)
            section_cursor = matched_idx + 1
        else:
            rendered.append(_render_group_fallback(group, source))
    for idx, sec in enumerate(sections):
        if idx in used_sections:
            continue
        sec_text = "\n\n".join(_quote_block(b) for b in sec if b.strip())
        if sec_text.strip():
            rendered.append(sec_text)
    return "\n\n".join([r for r in rendered if r and r.strip()]).strip()


def prepare_bundle_text_artifacts(
    pdf_path: Path,
    bundle: PassageBundle,
    cache_dir: Path,
    question_provider: str = "native_pdf_text",
    passage_provider: str = "native_pdf_text",
    lang: str = "eng",
    group_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    llama_api_key: str = "",
    llama_tier: str = "agentic",
) -> Dict[str, str]:
    bid = bundle_id(bundle)
    raw_q = extract_pages_text(pdf_path, bundle.question_pages, cache_dir, question_provider, f"question_{bid}", lang, llama_api_key, llama_tier) if bundle.question_pages else ""
    raw_p = extract_pages_text(pdf_path, bundle.passage_pages, cache_dir, passage_provider, f"passage_{bid}", lang, llama_api_key, llama_tier) if bundle.passage_pages else ""
    eff_groups = effective_groups_from_source(bundle, raw_q, group_overrides)
    return {
        "question_source": raw_q,
        "question_markup": build_auto_question_markdown(bundle, eff_groups, raw_q) if raw_q else "",
        "passage_text": _clean_markdown_source(raw_p),
        "question_provider": question_provider,
        "passage_provider": passage_provider,
    }


# -----------------------------
# Rendering helpers
# -----------------------------

def _normalize_camfmt_blocks(text: str) -> str:
    src = text or ""
    def repl(m: re.Match[str]) -> str:
        tag = m.group(0)
        if re.search(r"\bmarkdown\s*=\s*['\"]1['\"]", tag, flags=re.IGNORECASE):
            return tag
        return tag[:-1] + ' markdown="1">'
    return re.sub(r"<div\s+[^>]*data-camfmt=['\"]1['\"][^>]*>", repl, src, flags=re.IGNORECASE)


def _markdown_to_html(text: str) -> str:
    text = _normalize_camfmt_blocks((text or "").strip())
    if not text:
        return ""
    if md_lib is not None:
        return md_lib.markdown(text, extensions=["tables", "extra", "sane_lists", "nl2br", "md_in_html"])
    paras = [html.escape(x) for x in text.split("\n\n") if x.strip()]
    paras = [p.replace("\n", "<br />") for p in paras]
    return "".join(f"<p>{p}</p>" for p in paras)


def _style_markdown_tables_in_html(rendered: str) -> str:
    if not rendered:
        return rendered
    replacements = [
        (r"<table>", '<table style="width:100%; border-collapse:collapse; margin:10px 0; background:#fff; text-align:justify; text-justify:inter-word;">'),
        (r"<th>", '<th style="border:1px solid #d7dee8; padding:8px 10px; background:#edf3fb; text-align:left; vertical-align:top;">'),
        (r"<td>", '<td style="border:1px solid #d7dee8; padding:8px 10px; vertical-align:top;">'),
    ]
    for pat, repl in replacements:
        rendered = re.sub(pat, repl, rendered)
    return rendered


def _style_passage_paragraph_labels_in_html(rendered: str, label_style: str = "plain") -> str:
    if not rendered:
        return rendered
    mode = str(label_style or "plain").strip().lower()
    if mode not in {"plain", "badge"}:
        mode = "plain"

    def repl(m: re.Match[str]) -> str:
        label = m.group(1)
        body = (m.group(2) or '').strip()
        plain = re.sub(r'<[^>]+>', ' ', body)
        plain = re.sub(r'\s+', ' ', plain).strip()
        if len(plain) < 70:
            return m.group(0)
        return (
            f'<div class="cambridge-passage-para cambridge-passage-para-{mode}">'
            f'<div class="cambridge-passage-para-label cambridge-passage-para-label-{mode}">{html.escape(label)}</div>'
            f'<div class="cambridge-passage-para-body"><p>{body}</p></div>'
            '</div>'
        )

    patterns = [
        re.compile(r'<p>\s*(?:<strong>\s*)?([A-H])(?:\s*</strong>)?\s*<br\s*/?>\s*(.*?)</p>', flags=re.IGNORECASE | re.DOTALL),
        re.compile(r'<p>\s*(?:<strong>\s*)?([A-H])(?:\s*</strong>)?\s+((?:(?!</p>).){70,})</p>', flags=re.IGNORECASE | re.DOTALL),
    ]
    out = rendered
    for pat in patterns:
        out = pat.sub(repl, out)
    return out

def _course_scoped_layout_css() -> str:
    return """<style>
.cambridge-ielts-reading-layout {
  text-align: justify;
  text-justify: inter-word;
}
.cambridge-ielts-reading-layout,
.cambridge-ielts-reading-layout * {
  box-sizing: border-box;
}
.cambridge-ielts-reading-layout .cambridge-title {
  margin: 0 0 10px 0;
}
.cambridge-ielts-reading-layout .cambridge-split {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}
.cambridge-ielts-reading-layout .cambridge-col {
  vertical-align: top;
  padding: 12px;
}
.cambridge-ielts-reading-layout .cambridge-col-left {
  width: 50%;
  border-right: 1px solid #ddd;
  background: #fff;
}
.cambridge-ielts-reading-layout .cambridge-col-right {
  width: 50%;
  background: #f5f8fc;
}
.cambridge-ielts-reading-layout .cambridge-scrollpane {
  max-height: 75vh;
  overflow: auto;
  padding: 10px 20px 12px 10px;
  scrollbar-gutter: stable both-edges;
}
.cambridge-ielts-reading-layout .cambridge-leftpane {
  font-size: 0.95em;
  line-height: 1.58;
}
.cambridge-ielts-reading-layout .cambridge-rightpane {
  font-size: 0.95em;
  line-height: 1.66;
}
.cambridge-ielts-reading-layout .cambridge-leftpane p,
.cambridge-ielts-reading-layout .cambridge-rightpane p,
.cambridge-ielts-reading-layout .cambridge-leftpane li,
.cambridge-ielts-reading-layout .cambridge-rightpane li,
.cambridge-ielts-reading-layout .cambridge-leftpane blockquote,
.cambridge-ielts-reading-layout .cambridge-rightpane blockquote {
  margin-right: 6px;
}
.cambridge-ielts-reading-layout .cambridge-leftpane ul,
.cambridge-ielts-reading-layout .cambridge-leftpane ol,
.cambridge-ielts-reading-layout .cambridge-rightpane ul,
.cambridge-ielts-reading-layout .cambridge-rightpane ol {
  padding-left: 1.2em;
}
.cambridge-ielts-reading-layout .cambridge-rightpane table {
  margin-right: 6px;
}
.cambridge-ielts-reading-layout .cambridge-passage-para {
  display: grid;
  align-items: start;
  margin: 0 0 1em 0;
}
.cambridge-ielts-reading-layout .cambridge-passage-para.cambridge-passage-para-plain {
  grid-template-columns: 0.9em minmax(0, 1fr);
  gap: 0.22em;
}
.cambridge-ielts-reading-layout .cambridge-passage-para.cambridge-passage-para-badge {
  grid-template-columns: 1.9em minmax(0, 1fr);
  gap: 0.85em;
}
.cambridge-ielts-reading-layout .cambridge-passage-para-label {
  font-weight: 700;
  line-height: 1.15;
}
.cambridge-ielts-reading-layout .cambridge-passage-para-label.cambridge-passage-para-label-plain {
  display: block;
  padding-top: 0.03em;
  color: #334155;
}
.cambridge-ielts-reading-layout .cambridge-passage-para-label.cambridge-passage-para-label-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 1.9em;
  padding: 0.1em 0.5em;
  border-radius: 999px;
  background: #eef3f8;
  border: 1px solid #d7dee8;
  color: #66788a;
}
.cambridge-ielts-reading-layout .cambridge-passage-para-body > p {
  margin: 0;
}
.cambridge-ielts-reading-layout .cambridge-attempt-only { display:block; }
.cambridge-ielts-reading-layout .cambridge-review-only { display:block; }
body#page-mod-quiz-attempt .cambridge-ielts-reading-layout.cambridge-skill-listening .cambridge-col-left,
body.path-mod-quiz-attempt .cambridge-ielts-reading-layout.cambridge-skill-listening .cambridge-col-left {
  display:none !important;
}
body#page-mod-quiz-attempt .cambridge-ielts-reading-layout.cambridge-skill-listening .cambridge-review-only,
body.path-mod-quiz-attempt .cambridge-ielts-reading-layout.cambridge-skill-listening .cambridge-review-only {
  display:none !important;
}
body#page-mod-quiz-attempt .cambridge-ielts-reading-layout.cambridge-skill-listening .cambridge-col-right,
body.path-mod-quiz-attempt .cambridge-ielts-reading-layout.cambridge-skill-listening .cambridge-col-right {
  width:100% !important;
  display:table-cell !important;
}
body#page-mod-quiz-review .cambridge-ielts-reading-layout.cambridge-skill-listening .cambridge-review-only,
body.path-mod-quiz-review .cambridge-ielts-reading-layout.cambridge-skill-listening .cambridge-review-only,
body#page-mod-quiz-summary .cambridge-ielts-reading-layout.cambridge-skill-listening .cambridge-review-only {
  display:block !important;
}
body#page-mod-quiz-review .cambridge-ielts-reading-layout.cambridge-skill-listening .cambridge-attempt-only,
body.path-mod-quiz-review .cambridge-ielts-reading-layout.cambridge-skill-listening .cambridge-attempt-only,
body#page-mod-quiz-summary .cambridge-ielts-reading-layout.cambridge-skill-listening .cambridge-attempt-only {
  display:none !important;
}
.cambridge-ielts-reading-layout .cambridge-audio-wrap:empty {
  display:none !important;
}
body#page-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-progress-control,
body#page-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-progress-holder,
body#page-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-slider,
body#page-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-mouse-display,
body#page-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-time-tooltip,
body#page-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-play-progress,
body#page-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-load-progress,
body.path-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-progress-control,
body.path-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-progress-holder,
body.path-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-slider,
body.path-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-mouse-display,
body.path-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-time-tooltip,
body.path-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-play-progress,
body.path-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-load-progress {
  display:none !important;
}
body#page-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-progress-control.vjs-control,
body.path-mod-quiz-attempt .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-progress-control.vjs-control {
  width:0 !important;
  min-width:0 !important;
  max-width:0 !important;
  padding:0 !important;
  margin:0 !important;
  flex:0 0 0 !important;
}
body#page-mod-quiz-review .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-progress-control,
body.path-mod-quiz-review .cambridge-ielts-reading-layout .cambridge-audio-top .video-js .vjs-progress-control {
  display:flex !important;
  width:auto !important;
  min-width:0 !important;
  max-width:none !important;
  flex:1 1 auto !important;
}
.cambridge-ielts-reading-layout .cambridge-audio-wrap {
  margin: 0 0 12px 0;
  padding: 10px 12px;
  border: 1px solid #d7e4ff;
  border-radius: 14px;
  background: linear-gradient(180deg, #f8fbff 0%, #eef4ff 100%);
  box-shadow: 0 2px 10px rgba(47, 103, 216, 0.08);
}
.cambridge-ielts-reading-layout audio.cambridge-audio-player {
  width: 100% !important;
  max-width: 100%;
  border-radius: 14px;
  background: #eef4ff;
  color-scheme: light;
  accent-color: #2f67d8;
}
.cambridge-ielts-reading-layout audio.cambridge-audio-player::-webkit-media-controls-enclosure,
.cambridge-ielts-reading-layout audio.cambridge-audio-player::-webkit-media-controls-panel {
  background: #eef4ff !important;
  border-radius: 12px;
}
.cambridge-ielts-reading-layout .cambridge-feedback-common {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid #d7dee8;
}
.cambridge-ielts-reading-layout .cambridge-answer-slot {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  vertical-align: middle;
}
.cambridge-ielts-reading-layout .cambridge-inline-answer-badge {
  display: none;
  margin-left: 6px;
  padding: 2px 8px;
  border-radius: 4px;
  background: #f7eab5;
  color: #1f2328;
  font-size: 0.92em;
  line-height: 1.35;
  white-space: normal;
}
.cambridge-ielts-reading-layout .cambridge-audio-top {
  margin: 0 0 12px 0;
}
.cambridge-ielts-reading-layout .cambridge-vocab-bar {
  margin: 0 0 12px 0;
  padding: 10px 12px;
  border: 1px solid #d7dee8;
  border-radius: 8px;
  background: #fffdf4;
}
.cambridge-ielts-reading-layout .cambridge-vocab-bar-title {
  margin: 0 0 8px 0;
  font-weight: 700;
  color: #6b5a0c;
}
.cambridge-ielts-reading-layout .cambridge-vocab-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin: 4px 8px 4px 0;
  padding: 4px 10px;
  border-radius: 999px;
  background: #f7eab5;
  color: #1f2328;
  font-size: 0.93em;
}
.cambridge-ielts-reading-layout .cambridge-vocab-meaning {
  opacity: .9;
}
.que.correct .cambridge-ielts-reading-layout .cambridge-inline-answer-badge,
.que.incorrect .cambridge-ielts-reading-layout .cambridge-inline-answer-badge,
.que.partiallycorrect .cambridge-ielts-reading-layout .cambridge-inline-answer-badge,
.que.gaveup .cambridge-ielts-reading-layout .cambridge-inline-answer-badge,
.que.gradedright .cambridge-ielts-reading-layout .cambridge-inline-answer-badge,
.que.gradedwrong .cambridge-ielts-reading-layout .cambridge-inline-answer-badge,
.que.notanswered .cambridge-ielts-reading-layout .cambridge-inline-answer-badge {
  display: inline-flex;
}
.cambridge-ielts-reading-layout .cambridge-answer-slot:has(.feedback),
.cambridge-ielts-reading-layout .cambridge-answer-slot:has(.icon),
.cambridge-ielts-reading-layout .cambridge-answer-slot:has(.specificfeedback),
.cambridge-ielts-reading-layout .cambridge-answer-slot:has(input[readonly]),
.cambridge-ielts-reading-layout .cambridge-answer-slot:has(select[disabled]) {
  align-items: center;
}
.cambridge-ielts-reading-layout .cambridge-answer-slot:has(.feedback) .cambridge-inline-answer-badge,
.cambridge-ielts-reading-layout .cambridge-answer-slot:has(.icon) .cambridge-inline-answer-badge,
.cambridge-ielts-reading-layout .cambridge-answer-slot:has(.specificfeedback) .cambridge-inline-answer-badge,
.cambridge-ielts-reading-layout .cambridge-answer-slot:has(input[readonly]) .cambridge-inline-answer-badge,
.cambridge-ielts-reading-layout .cambridge-answer-slot:has(select[disabled]) .cambridge-inline-answer-badge {
  display: inline-flex;
}
</style>"""


def _write_course_scoped_layout_patch(out_dir: Path) -> None:
    css_block = _course_scoped_layout_css()
    css_only = re.sub(r"(?is)^<style>|</style>$", "", css_block).strip() + "\n"
    (out_dir / "moodle_course_only_layout_patch.css").write_text(css_only, encoding="utf-8")
    html_snippet = (
        "<!-- Dán khối này vào một Text/HTML block hoặc phần Additional HTML chỉ của course này nếu theme không giữ <style> bên trong question text. -->\n"
        + css_block
    )
    (out_dir / "moodle_course_only_layout_patch.html").write_text(html_snippet, encoding="utf-8")
    notes = (
        "Patch CSS này được scope theo body:has(.cambridge-ielts-reading-layout), vì vậy chỉ tác động ở những trang quiz có câu hỏi Cambridge do app này sinh ra.\n\n"
        "Mục tiêu:\n"
        "- giảm cỡ chữ nhẹ và thêm khoảng đệm trong vùng cuộn để chữ không sát scrollbar\n"
        "- cố gắng đẩy right sidebar / quiz navigation xuống dưới nội dung chính, theo chiều ngang\n\n"
        "Nếu theme Moodle của bạn có DOM khác, có thể cần chỉnh thêm selector trong file CSS này.\n"
    )
    (out_dir / "moodle_course_only_layout_patch_README.txt").write_text(notes, encoding="utf-8")



def _extract_display_answer_from_cloze(field: str) -> str:
    """Best-effort parser to show the correct answer next to reviewed subquestions."""
    text = (field or "").strip()
    if not text.startswith("{") or not text.endswith("}"):
        return ""
    inner = text[1:-1]
    parts = inner.split(":", 2)
    if len(parts) != 3:
        return ""
    _weight, qtype, body = parts
    qtype = (qtype or "").upper()

    def split_tokens(raw: str) -> List[str]:
        toks: List[str] = []
        buf: List[str] = []
        esc = False
        for ch in raw:
            if esc:
                buf.append(ch)
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == "~":
                toks.append("".join(buf))
                buf = []
                continue
            buf.append(ch)
        toks.append("".join(buf))
        return toks

    def unescape_token(tok: str) -> str:
        tok = re.split(r"(?<!\\)#", tok, 1)[0]
        tok = tok.replace("\\\\", "\\")
        tok = tok.replace("\\~", "~").replace("\\}", "}").replace("\\#", "#").replace("\\/", "/")
        tok = tok.replace("&quot;", '"')
        return tok.strip()

    tokens = split_tokens(body)
    corrects: List[str] = []
    for tok in tokens:
        tok = tok.strip()
        if not tok.startswith("="):
            continue
        val = unescape_token(tok[1:])
        if val == "*":
            continue
        if val:
            corrects.append(val)
    if not corrects:
        return ""
    if qtype.startswith("SHORTANSWER"):
        return " / ".join(corrects)
    return corrects[0]


def _wrap_field_for_markup(lbl: str, field: str) -> str:
    display_answer = _extract_display_answer_from_cloze(field)
    is_radio = bool(re.match(r"^\{\d+:MC(?:V|H)S?:", (field or "").strip(), flags=re.IGNORECASE))
    if not display_answer or is_radio:
        return field
    ans_html = base._format_answer_for_feedback(display_answer)
    return (
        f'<span class="cambridge-answer-slot" data-label="{html.escape(lbl)}">'
        f'{field}'
        f'<span class="cambridge-inline-answer-badge" aria-label="Correct answer" title="Correct answer">{ans_html}</span>'
        f'</span>'
    )


def _question_markup_needs_visual_aid(question_markup: str) -> bool:
    txt = _strip_blockquote_prefix(question_markup or "")
    return bool(re.search(r"\b(label the map|label the plan|label the diagram|map below|plan below|diagram below|picture below|map of|plan of|diagram of)\b", txt, flags=re.I))


def question_text_needs_visual_aid(question_markup: str = "", question_source: str = "", groups: Optional[List[QuestionGroup]] = None) -> bool:
    if _question_markup_needs_visual_aid(question_markup) or _question_markup_needs_visual_aid(question_source):
        return True
    src = question_source or ""
    if groups and any(g.group_type in ("letter_dropdown", "mc_letters") for g in groups):
        if re.search(r"\b(write the correct letter|choose the correct letter)\b", src, flags=re.I) and re.search(r"\b[A-Z]\s*[-–—]\s*[A-Z]\b", src):
            return True
    return False


def render_question_markup_with_fields(
    question_markup: str,
    fields: List[Tuple[str, str, int]],
    question_visual_images: Optional[List[Tuple[str, bytes]]] = None,
    question_visual_position: str = "top",
    question_visual_after_label: str = "",
    question_visual_after_keyword: str = "",
    pluginfile: bool = False,
) -> str:
    visual_html = _imgs_html(question_visual_images or [], pluginfile, compact=True) if (question_visual_images or []) else ""
    markup = question_markup or ""
    if visual_html:
        pos = (question_visual_position or "top").strip().lower()
        after_label = str(question_visual_after_label or "").strip()
        after_keyword = str(question_visual_after_keyword or "").strip().lower()
        lines = markup.replace("\r\n", "\n").replace("\r", "\n").split("\n")

        def _normalize_line_for_match(s: str) -> str:
            s = _strip_blockquote_prefix(s or "")
            s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)
            s = re.sub(r"\*(.*?)\*", r"\1", s)
            s = re.sub(r"`([^`]*)`", r"\1", s)
            s = re.sub(r"!\[[^\]]*\]\([^\)]*\)", " ", s)
            s = re.sub(r"\[[^\]]*\]\([^\)]*\)", " ", s)
            s = re.sub(r"\{[^{}]*\}", " ", s)
            s = re.sub(r"\[\[[^\]]+\]\]", " ", s)
            s = re.sub(r"^[\-\*\+]\s+", "", s.strip())
            return re.sub(r"\s+", " ", s).strip().lower()

        injected = False
        if pos == "after_label" and after_label:
            token = f"[[{after_label}]]"
            label_pat = re.compile(rf"(^|\b){re.escape(after_label)}(\b|$)")
            for i, line in enumerate(lines):
                norm = _normalize_line_for_match(line)
                if token in line or label_pat.search(norm):
                    lines.insert(i + 1, visual_html)
                    injected = True
                    break
        elif pos == "after_keyword" and after_keyword:
            kw = re.sub(r"\s+", " ", after_keyword).strip().lower()
            for i, line in enumerate(lines):
                norm = _normalize_line_for_match(line)
                if kw and kw in norm:
                    lines.insert(i + 1, visual_html)
                    injected = True
                    break
        elif pos == "bottom":
            markup = markup + "\n\n" + visual_html
            injected = True

        if not injected and pos != "bottom":
            markup = visual_html + "\n\n" + markup
        elif injected and pos != "bottom":
            markup = "\n".join(lines)
    rendered = _markdown_to_html(markup)
    field_map = {lbl: _wrap_field_for_markup(lbl, field) for lbl, field, _w in fields}
    for lbl, field in field_map.items():
        rendered = rendered.replace(html.escape(f"[[{lbl}]]"), field)
        rendered = rendered.replace(f"[[{lbl}]]", field)
    return _style_markdown_tables_in_html(rendered)



def _mime_for_name(name: str) -> str:
    ext = Path(name or "").suffix.lower()
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    if ext in {".mp3"}:
        return "audio/mpeg"
    if ext in {".wav"}:
        return "audio/wav"
    if ext in {".m4a"}:
        return "audio/mp4"
    if ext in {".ogg"}:
        return "audio/ogg"
    return "image/jpeg"


def _imgs_html(entries: List[Tuple[str, bytes]], pluginfile: bool = False, compact: bool = False) -> str:
    parts = []
    for name, b in entries:
        mime = _mime_for_name(name)
        src = f"@@PLUGINFILE@@/{name}" if pluginfile else (f"data:{mime};base64," + base64.b64encode(b).decode("ascii"))
        style = "width:100%; height:auto; border:1px solid #eee; border-radius:6px;" if compact else "width:100%; height:auto; border:1px solid #eee;"
        wrap = "margin:0 0 12px 0;" if compact else "margin:0 0 10px 0;"
        parts.append(f'<div style="{wrap}"><img src="{src}" alt="{html.escape(name)}" style="{style}" /></div>')
    return "\n".join(parts)


def _bundle_audio_entries(text_data: Dict[str, Any]) -> List[Tuple[str, bytes]]:
    path = str((text_data or {}).get("audio_override_path", "")).strip()
    if path and Path(path).exists():
        p = Path(path)
        return [(p.name, p.read_bytes())]
    return []


def _audio_players_html(entries: List[Tuple[str, bytes]], pluginfile: bool = False, audio_title: str = "", audio_lockid: str = "") -> str:
    if not entries:
        return ""
    parts: List[str] = ['<div class="cambridge-audio-block">']
    if str(audio_title or "").strip():
        parts.append(f'<div class="cambridge-audio-block-title"><strong>{html.escape(str(audio_title))}</strong></div>')
    for name, b in entries:
        mime = _mime_for_name(name)
        src = f"@@PLUGINFILE@@/{name}" if pluginfile else (f"data:{mime};base64," + base64.b64encode(b).decode("ascii"))
        lock_attr = f' data-lockid="{html.escape(str(audio_lockid))}"' if str(audio_lockid or "").strip() else ""
        parts.append(
            f'<div class="cambridge-audio-wrap"><audio preload="none" controls="controls" data-cambridge-audio="1" class="cambridge-audio-player"{lock_attr} style="width:100%; max-width:100%;">'
            f'<source src="{src}" type="{mime}">{html.escape(src)}</audio></div>'
        )
    parts.append('</div>')
    return "".join(parts)





def _default_audio_lockid(bundle: PassageBundle) -> str:
    return f"test{bundle.test_num}_part{bundle.passage_num}"


def _bank_question_name(bundle: PassageBundle, skill: str = "reading") -> str:
    s = (skill or "reading").strip().lower()
    if s == "listening":
        return f"Test {bundle.test_num} - Listening Section {bundle.passage_num} (Q{bundle.qrange[0]}-{bundle.qrange[1]})"
    return f"Test {bundle.test_num} - Reading Passage {bundle.passage_num} (Q{bundle.qrange[0]}-{bundle.qrange[1]})"

def _bundle_question_visual_images(pdf_path: Path, cache_dir: Path, doc: fitz.Document, bundle: PassageBundle, text_data: Dict[str, Any], image_zoom: float, jpeg_quality: int) -> List[Tuple[str, bytes]]:
    if bool((text_data or {}).get("question_image_disabled", False)):
        return []
    override_path = str((text_data or {}).get("question_image_override_path", "")).strip()
    if override_path and Path(override_path).exists():
        p = Path(override_path)
        return [(p.name, p.read_bytes())]
    spec = str((text_data or {}).get("question_image_page_override", "")).strip()
    pages: List[int] = []
    needs_visual = question_text_needs_visual_aid((text_data or {}).get("question_markup", ""), (text_data or {}).get("question_source", ""), bundle.groups)
    if spec:
        for chunk in re.split(r"[,;\s]+", spec):
            if not chunk:
                continue
            m = re.match(r"^(\d+)\s*[-–—]\s*(\d+)$", chunk)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                lo, hi = (a, b) if a <= b else (b, a)
                pages.extend(range(lo, hi + 1))
            elif chunk.isdigit():
                pages.append(int(chunk))
    elif needs_visual and bundle.question_pages:
        pages = [bundle.question_pages[0] + 1]
    out: List[Tuple[str, bytes]] = []
    for page_1based in sorted(dict.fromkeys([p for p in pages if p >= 1]))[:2]:
        pno = page_1based - 1
        if pno < 0 or pno >= len(doc):
            continue
        name = f"qvis_t{bundle.test_num}_s{bundle.passage_num}_p{pno:03d}.jpg"
        img_path = cache_dir / "images" / name
        if img_path.exists():
            img_bytes = img_path.read_bytes()
        else:
            img = _render_page(doc, pno, zoom=image_zoom)
            img_bytes = _img_to_jpeg_bytes(img, quality=jpeg_quality)
            img_path.write_bytes(img_bytes)
        out.append((name, img_bytes))
    return out
def build_preview_html(
    title: str,
    passage_images: List[Tuple[str, bytes]],
    question_images: List[Tuple[str, bytes]],
    fields: List[Tuple[str, str, int]],
    passage_text: str = "",
    question_markup: str = "",
    explanation_html: str = "",
    pluginfile: bool = False,
    question_visual_images: Optional[List[Tuple[str, bytes]]] = None,
    question_visual_position: str = "top",
    question_visual_after_label: str = "",
    question_visual_after_keyword: str = "",
    skill: str = "reading",
    review_left_markdown: str = "",
    audio_entries: Optional[List[Tuple[str, bytes]]] = None,
    audio_title: str = "",
    audio_lockid: str = "",
    audio_show_in_review: bool = True,
    study_keywords: Optional[Dict[str, Any]] = None,
    passage_label_style: str = "plain",
) -> str:
    default_left_html = _style_passage_paragraph_labels_in_html(_style_markdown_tables_in_html(_markdown_to_html(passage_text)), label_style=passage_label_style) if passage_text.strip() else _imgs_html(passage_images, pluginfile)
    review_left_html = _style_passage_paragraph_labels_in_html(_style_markdown_tables_in_html(_markdown_to_html(review_left_markdown)), label_style=passage_label_style) if (review_left_markdown or "").strip() else default_left_html
    if question_markup.strip():
        right_body = render_question_markup_with_fields(
            question_markup,
            fields,
            question_visual_images=question_visual_images or [],
            question_visual_position=str((question_visual_position or "top")),
            question_visual_after_label=str((question_visual_after_label or "")),
            question_visual_after_keyword=str((question_visual_after_keyword or "")),
            pluginfile=pluginfile,
        )
    else:
        rows = []
        for lbl, field, _w in fields:
            rows.append(f"<tr><td style='padding:4px 8px; width:1%; white-space:nowrap;'><strong>{lbl}</strong></td><td style='padding:4px 8px;'>{field}</td></tr>")
        answers = "<table style='width:100%; border-collapse:collapse;'>" + "".join(rows) + "</table>"
        right_body = _imgs_html(question_images, pluginfile) + "<hr /><p><strong>Answers</strong></p>" + answers
    feedback = f"<div class='cambridge-feedback-common'><strong>Answer explanations / Giải thích đáp án</strong>{explanation_html}</div>" if explanation_html else ""
    audio_html = _audio_players_html(audio_entries or [], pluginfile=pluginfile, audio_title=audio_title, audio_lockid=audio_lockid)
    attempt_left_html = default_left_html if (skill or "reading") != "listening" else ""
    layout_cls = "cambridge-ielts-reading-layout cambridge-skill-" + html.escape(skill or "reading")
    vocab_html = ""
    if study_keywords and (study_keywords.get("items") or []):
        vocab_html = '<div class="cambridge-vocab-bar"><div class="cambridge-vocab-bar-title">Vocabulary B2+ / Từ vựng B2+ cần học</div>' + keywords_to_bar_html(study_keywords) + '</div>'
    top_audio_html = ""
    if (skill or "reading") == "listening" and audio_html:
        top_audio_html = f'<div class="cambridge-audio-top">{audio_html}</div>'
    left_combined_html = review_left_html if (skill or "reading") != "listening" else (
        f'<div class="cambridge-attempt-only">{attempt_left_html}</div>'
        f'<div class="cambridge-review-only">{review_left_html}</div>'
    )
    return f"""
{_course_scoped_layout_css()}
<div class="{layout_cls}">
  <p class="cambridge-title"><strong>{html.escape(title)}</strong></p>
  {vocab_html}
  {top_audio_html}
  <table class="cambridge-split">
    <tr>
      <td class="cambridge-col cambridge-col-left">
        <div class="cambridge-scrollpane cambridge-leftpane">{left_combined_html}</div>
      </td>
      <td class="cambridge-col cambridge-col-right">
        <div class="cambridge-scrollpane cambridge-rightpane">{right_body}{feedback}</div>
      </td>
    </tr>
  </table>
</div>
""".strip()


def build_answer_context(bundle: PassageBundle, groups: List[QuestionGroup], singles: Dict[int, List[str]], pairs: List[Tuple[Tuple[int, int], List[str]]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    pair_map = {(min(a, b), max(a, b)): vals for (a, b), vals in pairs}
    for g in sorted(groups, key=lambda x: min(x.qnums) if x.qnums else 10**9):
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




def _append_unique_named_files(target: List[Tuple[str, bytes]], extra: List[Tuple[str, bytes]]) -> None:
    seen = {name for name, _ in target}
    for name, b in extra or []:
        if name in seen:
            continue
        target.append((name, b))
        seen.add(name)

# -----------------------------
# Export wrappers
# -----------------------------

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
    keys_by_test: Optional[Dict[int, Tuple[Dict[int, List[str]], List[Tuple[Tuple[int, int], List[str]]]]]] = None,
    group_overrides_by_bundle: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    text_data_by_bundle: Optional[Dict[str, Dict[str, str]]] = None,
    feedback_items_by_bundle: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    transcript_export_mode: str = "review_only",
    include_keywords: bool = True,
) -> Path:
    _ensure_dir(out_dir)
    _ensure_dir(cache_dir / "images")
    doc = fitz.open(str(pdf_path))
    bundles_by_test: Dict[int, List[PassageBundle]] = {}
    for b in bundles:
        bundles_by_test.setdefault(b.test_num, []).append(b)

    quiz = ET.Element("quiz")
    cat_q = ET.SubElement(quiz, "question", {"type": "category"})
    cat_text = ET.SubElement(ET.SubElement(cat_q, "category"), "text")
    cat_text.text = f"$course$/{category}"

    for test_num, test_bundles in sorted(bundles_by_test.items(), key=lambda kv: kv[0]):
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

        for b in sorted(test_bundles, key=lambda x: x.passage_num):
            bid = bundle_id(b)
            embedded_files: List[Tuple[str, bytes]] = []
            passage_inline: List[Tuple[str, bytes]] = []
            question_inline: List[Tuple[str, bytes]] = []

            def add_page_image(pno: int, prefix: str) -> Tuple[str, bytes]:
                name = f"{prefix}_t{test_num}_p{pno:03d}.jpg"
                img_path = cache_dir / "images" / name
                if img_path.exists():
                    img_bytes = img_path.read_bytes()
                else:
                    img = _render_page(doc, pno, zoom=image_zoom)
                    img_bytes = _img_to_jpeg_bytes(img, quality=jpeg_quality)
                    img_path.write_bytes(img_bytes)
                embedded_files.append((name, img_bytes))
                return name, img_bytes

            for pno in b.passage_pages:
                passage_inline.append(add_page_image(pno, f"passage{b.passage_num}"))
            for pno in b.question_pages:
                question_inline.append(add_page_image(pno, f"q{b.passage_num}"))

            effective_groups = effective_groups_from_source(b, ((text_data_by_bundle or {}).get(bid) or {}).get("question_source", ""), (group_overrides_by_bundle or {}).get(bid))
            feedback_by_label = (feedback_items_by_bundle or {}).get(bid, {})
            fields = _qnums_to_fields(
                qrange=b.qrange,
                groups=effective_groups,
                singles=singles,
                pairs=pairs,
                prefer_radio_small=True,
                shuffle=shuffle_choices,
                feedback_by_label=feedback_by_label,
                choice_layout=str(((text_data_by_bundle or {}).get(bid) or {}).get("choice_layout", "vertical") or "vertical"),
            )
            total_mark = sum(w for _lbl, _f, w in fields)
            text_data = (text_data_by_bundle or {}).get(bid) or {}
            title = text_data.get("display_title") or (f"Listening - Part {b.passage_num}" if str(text_data.get("skill", "reading") or "reading") == "listening" else f"Reading - Passage {b.passage_num}")
            question_visual_inline = _bundle_question_visual_images(pdf_path, cache_dir, doc, b, text_data, image_zoom, jpeg_quality)
            audio_inline = _bundle_audio_entries(text_data)
            _append_unique_named_files(embedded_files, question_visual_inline)
            _append_unique_named_files(embedded_files, audio_inline)
            review_left_markdown = text_data.get("audioscript_clean", "") or text_data.get("audioscript_raw", "") if text_data.get("skill") == "listening" else text_data.get("passage_text", "")
            html_str = build_preview_html(
                title=title,
                passage_images=passage_inline,
                question_images=question_inline,
                fields=fields,
                passage_text=text_data.get("passage_text", ""),
                question_markup=text_data.get("question_markup", ""),
                explanation_html="",
                pluginfile=True,
                question_visual_images=question_visual_inline,
                question_visual_position=str(text_data.get("question_image_position", "top") or "top"),
                question_visual_after_label=str(text_data.get("question_image_after_label", "") or ""),
                question_visual_after_keyword=str(text_data.get("question_image_after_keyword", "") or ""),
                skill=str(text_data.get("skill", "reading") or "reading"),
                review_left_markdown=review_left_markdown,
                audio_entries=audio_inline,
                audio_title=str(text_data.get("audio_title", "") or ""),
                audio_lockid=("" if include_keywords else str(text_data.get("audio_lockid", _default_audio_lockid(b)) or _default_audio_lockid(b))),
                audio_show_in_review=bool(text_data.get("audio_show_in_review", True)),
                study_keywords=(text_data.get("study_keywords") if include_keywords else None),
                passage_label_style=str(text_data.get("passage_label_style", "plain") or "plain"),
            )

            q = ET.SubElement(quiz, "question", {"type": "cloze"})
            ET.SubElement(ET.SubElement(q, "name"), "text").text = base.strip_invalid_xml_chars(_bank_question_name(b, str(text_data.get("skill", "reading") or "reading")))
            qtext = ET.SubElement(q, "questiontext", {"format": "html"})
            ET.SubElement(qtext, "text").text = base.strip_invalid_xml_chars(html_str)
            for fname, fbytes in embedded_files:
                file_el = ET.SubElement(qtext, "file", {"name": fname, "path": "/", "encoding": "base64"})
                file_el.text = base64.b64encode(fbytes).decode("ascii")
            ET.SubElement(q, "defaultgrade").text = str(total_mark)
            ET.SubElement(q, "penalty").text = "0.3333333"
            ET.SubElement(q, "hidden").text = "0"
            gf = ET.SubElement(q, "generalfeedback", {"format": "html"})
            gf_parts: List[str] = []
            if feedback_by_label:
                gf_parts.append(explanations_to_generalfeedback_html({"items": list(feedback_by_label.values())}))
            if transcript_export_mode == "generalfeedback" and text_data.get("skill") == "listening" and str(text_data.get("audioscript_clean", "")).strip():
                transcript_source = text_data.get("audioscript_clean", "") or text_data.get("audioscript_normalized", "")
                transcript_html = _style_markdown_tables_in_html(_markdown_to_html(transcript_source))
                gf_parts.append("<hr /><details><summary><strong>Audioscript / Transcript</strong></summary>" + transcript_html + "</details>")
            ET.SubElement(gf, "text").text = base.strip_invalid_xml_chars("".join(gf_parts))

    doc.close()
    xml_path = out_dir / output_name
    tree = ET.ElementTree(quiz)
    ET.indent(tree, space="  ", level=0)
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)
    return xml_path


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
    html_mode: str = "standalone",
    output_name: str = "reading_snippets.html",
    export_assets: bool = True,
    keys_by_test: Optional[Dict[int, Tuple[Dict[int, List[str]], List[Tuple[Tuple[int, int], List[str]]]]]] = None,
    group_overrides_by_bundle: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    text_data_by_bundle: Optional[Dict[str, Dict[str, str]]] = None,
    feedback_items_by_bundle: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    transcript_export_mode: str = "review_only",
    include_keywords: bool = True,
) -> Tuple[Path, Optional[Path]]:
    _ensure_dir(out_dir)
    _ensure_dir(cache_dir / "images")
    doc = fitz.open(str(pdf_path))
    assets_dir: Optional[Path] = None
    if export_assets and html_mode == "moodle":
        assets_dir = out_dir / "html_assets"
        _ensure_dir(assets_dir)

    bundles_by_test: Dict[int, List[PassageBundle]] = {}
    for b in bundles:
        bundles_by_test.setdefault(b.test_num, []).append(b)

    parts = ["<!doctype html><html><head><meta charset='utf-8'><title>Reading snippets</title></head><body>", "<h2>Reading snippets</h2><hr />"]
    for test_num, test_bundles in sorted(bundles_by_test.items(), key=lambda kv: kv[0]):
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
        parts.append(f"<h3>Test {test_num}</h3>")

        for b in sorted(test_bundles, key=lambda x: x.passage_num):
            bid = bundle_id(b)
            passage_inline: List[Tuple[str, bytes]] = []
            question_inline: List[Tuple[str, bytes]] = []

            def add_page_image(pno: int, prefix: str) -> Tuple[str, bytes]:
                name = f"{prefix}_t{test_num}_p{pno:03d}.jpg"
                img_path = cache_dir / "images" / name
                if img_path.exists():
                    img_bytes = img_path.read_bytes()
                else:
                    img = _render_page(doc, pno, zoom=image_zoom)
                    img_bytes = _img_to_jpeg_bytes(img, quality=jpeg_quality)
                    img_path.write_bytes(img_bytes)
                if assets_dir is not None:
                    (assets_dir / name).write_bytes(img_bytes)
                return name, img_bytes

            for pno in b.passage_pages:
                passage_inline.append(add_page_image(pno, f"passage{b.passage_num}"))
            for pno in b.question_pages:
                question_inline.append(add_page_image(pno, f"q{b.passage_num}"))

            effective_groups = effective_groups_from_source(b, ((text_data_by_bundle or {}).get(bid) or {}).get("question_source", ""), (group_overrides_by_bundle or {}).get(bid))
            feedback_by_label = (feedback_items_by_bundle or {}).get(bid, {})
            fields = _qnums_to_fields(
                qrange=b.qrange,
                groups=effective_groups,
                singles=singles,
                pairs=pairs,
                prefer_radio_small=True,
                shuffle=shuffle_choices,
                feedback_by_label=feedback_by_label,
                choice_layout=str(((text_data_by_bundle or {}).get(bid) or {}).get("choice_layout", "vertical") or "vertical"),
            )
            text_data = (text_data_by_bundle or {}).get(bid) or {}
            title = text_data.get("display_title") or (f"Listening - Part {b.passage_num}" if str(text_data.get("skill", "reading") or "reading") == "listening" else f"Reading - Passage {b.passage_num}")
            explanation_parts: List[str] = []
            if feedback_by_label:
                explanation_parts.append(explanations_to_generalfeedback_html({"items": list(feedback_by_label.values())}))
            if transcript_export_mode == "generalfeedback" and text_data.get("skill") == "listening" and str(text_data.get("audioscript_clean", "")).strip():
                transcript_source = text_data.get("audioscript_clean", "") or text_data.get("audioscript_normalized", "")
                transcript_html = _style_markdown_tables_in_html(_markdown_to_html(transcript_source))
                explanation_parts.append("<hr /><details><summary><strong>Audioscript / Transcript</strong></summary>" + transcript_html + "</details>")
            question_visual_inline = _bundle_question_visual_images(pdf_path, cache_dir, doc, b, text_data, image_zoom, jpeg_quality)
            audio_inline = _bundle_audio_entries(text_data)
            if assets_dir is not None:
                for name, img_bytes in question_visual_inline:
                    (assets_dir / name).write_bytes(img_bytes)
                for name, audio_bytes in audio_inline:
                    (assets_dir / name).write_bytes(audio_bytes)
            review_left_markdown = text_data.get("audioscript_clean", "") or text_data.get("audioscript_raw", "") if text_data.get("skill") == "listening" else text_data.get("passage_text", "")
            parts.append(build_preview_html(
                title=title,
                passage_images=passage_inline,
                question_images=question_inline,
                fields=fields,
                passage_text=text_data.get("passage_text", ""),
                question_markup=text_data.get("question_markup", ""),
                explanation_html="".join(explanation_parts),
                pluginfile=(html_mode == "moodle"),
                question_visual_images=question_visual_inline,
                question_visual_position=str(text_data.get("question_image_position", "top") or "top"),
                question_visual_after_label=str(text_data.get("question_image_after_label", "") or ""),
                question_visual_after_keyword=str(text_data.get("question_image_after_keyword", "") or ""),
                skill=str(text_data.get("skill", "reading") or "reading"),
                review_left_markdown=review_left_markdown,
                audio_entries=audio_inline,
                audio_title=str(text_data.get("audio_title", "") or ""),
                audio_lockid=("" if include_keywords else str(text_data.get("audio_lockid", _default_audio_lockid(b)) or _default_audio_lockid(b))),
                audio_show_in_review=bool(text_data.get("audio_show_in_review", True)),
                study_keywords=(text_data.get("study_keywords") if include_keywords else None),
                passage_label_style=str(text_data.get("passage_label_style", "plain") or "plain"),
            ))
            parts.append("<hr />")

    parts.append("</body></html>")
    doc.close()
    html_path = out_dir / output_name
    html_path.write_text("\n".join(parts), encoding="utf-8")
    return html_path, assets_dir
