import base64
import copy
import dataclasses
import html
import io
import json
import os
import pickle
import re
import zipfile
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image
import streamlit as st
import streamlit.components.v1 as components

import cambridge_pdf2moodle_plus as c19
import cambridge_listening_support as c19l

st.set_page_config(page_title="Cambridge PDF → Moodle v7_full_fix6_hotfix8 UI", layout="wide")
st.title("Cambridge PDF → Moodle v7_full_fix6_hotfix8 UI")
st.caption(
    "Bản v7_full_fix6_hotfix8: tăng độ chắc cho Listening ở phần detect Section 1-4 và audioscript clean, "
    "đồng thời thêm manifest portable + import ngược XML/HTML/manifest để mở lại preview cũ mà không cần gọi API."
)


def _apply_tesseract_cmd_if_set() -> None:
    tcmd = os.environ.get("TESSERACT_CMD")
    if tcmd:
        c19.pytesseract.pytesseract.tesseract_cmd = tcmd


def _zip_dir(dir_path: Path, zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in dir_path.rglob("*"):
            if p.is_file():
                z.write(p, arcname=str(p.relative_to(dir_path.parent)))
    return zip_path

SNAPSHOT_STATE_KEYS = [
    "scans",
    "bundles",
    "keys_by_test",
    "keys_original",
    "prepared_pdf",
    "prepared_cache_dir",
    "prepared_tests",
    "prepared_lang",
    "group_overrides",
    "text_data_by_bundle",
    "format_layers_by_bundle",
    "feedback_items_by_bundle",
    "prepared_question_provider",
    "prepared_passage_provider",
    "prepared_skill",
    "prepared_answer_provider",
    "prepared_transcript_provider",
    "prepared_transcript_page_mode",
    "prepared_transcript_page_ranges",
    "transcript_diagnostics_by_test",
]

TEXT_EDITOR_FIELDS = ["passage_text", "question_markup"]
MANIFEST_MARKER = "CAMPLUS_MANIFEST_BASE64:"
MANIFEST_VERSION = "v7_full_fix6_hotfix8"


def _editor_widget_key(target: str, bid: str) -> str:
    return f"{target}_{bid}"


def _parse_page_range_spec(spec: str) -> List[int]:
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



def _collect_transcript_page_ranges(mode: str, tests: List[int]) -> Dict[int, Any]:
    if mode == "manual_global":
        pages = _parse_page_range_spec(str(st.session_state.get("transcript_pages_global", "")))
        return {int(t): list(pages) for t in tests if pages}
    if mode == "manual_per_test":
        out: Dict[int, Any] = {}
        for t in tests:
            pages = _parse_page_range_spec(str(st.session_state.get(f"transcript_pages_t{t}", "")))
            if pages:
                out[int(t)] = pages
        return out
    if mode == "manual_per_section":
        out: Dict[int, Any] = {}
        for t in tests:
            sec_map = {
                1: _parse_page_range_spec(str(st.session_state.get(f"transcript_pages_t{t}_s1", ""))),
                2: _parse_page_range_spec(str(st.session_state.get(f"transcript_pages_t{t}_s2", ""))),
                3: _parse_page_range_spec(str(st.session_state.get(f"transcript_pages_t{t}_s3", ""))),
                4: _parse_page_range_spec(str(st.session_state.get(f"transcript_pages_t{t}_s4", ""))),
            }
            if any(sec_map.values()):
                out[int(t)] = sec_map
        return out

    return {}


def _bundle_asset_dir(bid: str) -> Optional[Path]:
    cache_dir = st.session_state.get("prepared_cache_dir")
    if not cache_dir:
        return None
    p = Path(str(cache_dir)) / "bundle_assets" / bid
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save_uploaded_bundle_image(bid: str, uploaded_file) -> Optional[Path]:
    if uploaded_file is None:
        return None
    asset_dir = _bundle_asset_dir(bid)
    if asset_dir is None:
        return None
    suffix = Path(uploaded_file.name).suffix.lower() or ".png"
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        suffix = ".png"
    out = asset_dir / f"question_visual_override{suffix}"
    out.write_bytes(uploaded_file.getvalue())
    td = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
    cur = copy.deepcopy(td.get(bid, {}))
    cur["question_image_override_path"] = str(out)
    cur["question_image_override_name"] = uploaded_file.name
    td[bid] = cur
    st.session_state["text_data_by_bundle"] = td
    return out


def _clear_uploaded_bundle_image(bid: str) -> None:
    td = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
    cur = copy.deepcopy(td.get(bid, {}))
    path = str(cur.get("question_image_override_path", ""))
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
    cur.pop("question_image_override_path", None)
    cur.pop("question_image_override_name", None)
    td[bid] = cur
    st.session_state["text_data_by_bundle"] = td


def _save_uploaded_bundle_audio(bid: str, uploaded_file) -> Optional[Path]:
    if uploaded_file is None:
        return None
    asset_dir = _bundle_asset_dir(bid)
    if asset_dir is None:
        return None
    suffix = Path(uploaded_file.name).suffix.lower() or ".mp3"
    if suffix not in {".mp3", ".wav", ".m4a", ".ogg"}:
        suffix = ".mp3"
    out = asset_dir / f"section_audio{suffix}"
    out.write_bytes(uploaded_file.getvalue())
    td = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
    cur = copy.deepcopy(td.get(bid, {}))
    cur["audio_override_path"] = str(out)
    cur["audio_override_name"] = uploaded_file.name
    td[bid] = cur
    st.session_state["text_data_by_bundle"] = td
    return out


def _clear_uploaded_bundle_audio(bid: str) -> None:
    td = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
    cur = copy.deepcopy(td.get(bid, {}))
    path = str(cur.get("audio_override_path", ""))
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
    cur.pop("audio_override_path", None)
    cur.pop("audio_override_name", None)
    td[bid] = cur
    st.session_state["text_data_by_bundle"] = td


def _render_question_visual_page(bundle: c19.PassageBundle, page_1based: int, preview_zoom: float = 1.3, jpeg_quality: int = 82) -> Optional[Tuple[str, bytes]]:
    pdf_path_local = st.session_state.get("prepared_pdf")
    cache_dir = st.session_state.get("prepared_cache_dir")
    if not pdf_path_local or not cache_dir or not Path(str(pdf_path_local)).exists():
        return None
    doc = c19.fitz.open(str(pdf_path_local))
    try:
        pno = int(page_1based) - 1
        if pno < 0 or pno >= len(doc):
            return None
        c19._ensure_dir(Path(str(cache_dir)) / "images")
        bid = c19.bundle_id(bundle)
        name = f"qvis_{bid}_p{pno:03d}.jpg"
        img_path = Path(str(cache_dir)) / "images" / name
        if img_path.exists():
            img_bytes = img_path.read_bytes()
        else:
            img = c19._render_page(doc, pno, zoom=float(preview_zoom))
            img_bytes = c19._img_to_jpeg_bytes(img, quality=int(jpeg_quality))
            img_path.write_bytes(img_bytes)
        return name, img_bytes
    finally:
        doc.close()


def _resolve_question_visual_source(bundle: c19.PassageBundle, pages_spec: str = "", uploaded_file=None, prefer_existing_override: bool = True, preview_zoom: float = 1.4, jpeg_quality: int = 88) -> Optional[Tuple[str, bytes]]:
    bid = c19.bundle_id(bundle)
    if uploaded_file is not None:
        try:
            data = uploaded_file.getvalue()
            if data:
                suffix = Path(uploaded_file.name).suffix.lower() or ".png"
                return (f"upload_{bid}{suffix}", data)
        except Exception:
            pass
    td = _get_bundle_text_data(bid)
    if prefer_existing_override:
        override_path = str(td.get("question_image_override_path", "")).strip()
        if override_path and Path(override_path).exists():
            p = Path(override_path)
            return (p.name, p.read_bytes())
    spec = (pages_spec or str(td.get("question_image_page_override", "")).strip()).strip()
    pages = _parse_page_range_spec(spec)
    if not pages and bundle.question_pages:
        pages = [bundle.question_pages[0] + 1]
    if not pages:
        return None
    return _render_question_visual_page(bundle, pages[0], preview_zoom=preview_zoom, jpeg_quality=jpeg_quality)


def _save_cropped_bundle_image(bid: str, source_name: str, source_bytes: bytes, crop_box: Tuple[int, int, int, int]) -> Optional[Path]:
    asset_dir = _bundle_asset_dir(bid)
    if asset_dir is None:
        return None
    img = Image.open(io.BytesIO(source_bytes)).convert("RGBA")
    x, y, w, h = crop_box
    x = max(0, min(int(x), max(0, img.width - 1)))
    y = max(0, min(int(y), max(0, img.height - 1)))
    w = max(1, min(int(w), max(1, img.width - x)))
    h = max(1, min(int(h), max(1, img.height - y)))
    cropped = img.crop((x, y, x + w, y + h))
    out = asset_dir / "question_visual_override_crop.png"
    cropped.save(out, format="PNG")
    td = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
    cur = copy.deepcopy(td.get(bid, {}))
    cur["question_image_override_path"] = str(out)
    cur["question_image_override_name"] = f"cropped:{source_name}"
    td[bid] = cur
    st.session_state["text_data_by_bundle"] = td
    return out


def _question_visual_override_entries(bundle: c19.PassageBundle, preview_zoom: float = 1.3, jpeg_quality: int = 82) -> List[Tuple[str, bytes]]:
    bid = c19.bundle_id(bundle)
    text_data = _get_resolved_bundle_text_data(bid)
    if bool(text_data.get("question_image_disabled", False)):
        return []
    override_path = str(text_data.get("question_image_override_path", "")).strip()
    if override_path and Path(override_path).exists():
        p = Path(override_path)
        return [(p.name, p.read_bytes())]
    pdf_path_local = st.session_state.get("prepared_pdf")
    cache_dir = st.session_state.get("prepared_cache_dir")
    if not pdf_path_local or not cache_dir or not Path(str(pdf_path_local)).exists():
        return []
    pages_spec = str(text_data.get("question_image_page_override", "")).strip()
    needs_visual = c19.question_text_needs_visual_aid(text_data.get("question_markup", ""), text_data.get("question_source", ""), _effective_groups(bundle))
    if pages_spec:
        chosen_pages = _parse_page_range_spec(pages_spec)
    elif needs_visual and bundle.question_pages:
        chosen_pages = [bundle.question_pages[0] + 1]
    else:
        chosen_pages = []
    doc = c19.fitz.open(str(pdf_path_local))
    try:
        c19._ensure_dir(Path(str(cache_dir)) / "images")
        entries: List[Tuple[str, bytes]] = []
        for page_1based in chosen_pages[:2]:
            pno = int(page_1based) - 1
            if pno < 0 or pno >= len(doc):
                continue
            name = f"qvis_{bid}_p{pno:03d}.jpg"
            img_path = Path(str(cache_dir)) / "images" / name
            if img_path.exists():
                img_bytes = img_path.read_bytes()
            else:
                img = c19._render_page(doc, pno, zoom=float(preview_zoom))
                img_bytes = c19._img_to_jpeg_bytes(img, quality=int(jpeg_quality))
                img_path.write_bytes(img_bytes)
            entries.append((name, img_bytes))
        return entries
    finally:
        doc.close()


def _bundle_audio_preview_bytes(bid: str) -> Optional[Tuple[bytes, str]]:
    text_data = _get_bundle_text_data(bid)
    path = str(text_data.get("audio_override_path", "")).strip()
    if path and Path(path).exists():
        p = Path(path)
        return p.read_bytes(), p.suffix.lower() or ".mp3"
    return None


def _manual_transcript_pages_for_test(test_num: int) -> Any:
    if st.session_state.get("prepared_transcript_page_mode") == "auto":
        return []
    return (st.session_state.get("prepared_transcript_page_ranges") or {}).get(int(test_num), [])


def _mark_editor_refresh(bid: str) -> None:
    pending = set(st.session_state.get("_editor_refresh_bids", []))
    pending.add(str(bid))
    st.session_state["_editor_refresh_bids"] = sorted(pending)


def _mark_all_editor_refresh() -> None:
    pending = set(st.session_state.get("_editor_refresh_bids", []))
    for b in st.session_state.get("bundles", []) or []:
        try:
            pending.add(c19.bundle_id(b))
        except Exception:
            pass
    if pending:
        st.session_state["_editor_refresh_bids"] = sorted(pending)


def _get_bundle_text_data(bid: str) -> Dict[str, str]:
    return copy.deepcopy((st.session_state.get("text_data_by_bundle", {}) or {}).get(bid, {}))


def _set_bundle_text_field(bid: str, field: str, value: str) -> None:
    td = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
    cur = copy.deepcopy(td.get(bid, {}))
    cur[field] = _ensure_camfmt_markdown_attr(value)
    td[bid] = cur
    st.session_state["text_data_by_bundle"] = td

def _get_bundle_format_layers(bid: str) -> Dict[str, List[dict]]:
    return copy.deepcopy((st.session_state.get("format_layers_by_bundle", {}) or {}).get(bid, {}))


def _set_bundle_format_layers(bid: str, field: str, layers: List[dict]) -> None:
    all_layers = copy.deepcopy(st.session_state.get("format_layers_by_bundle", {}))
    cur = copy.deepcopy(all_layers.get(bid, {}))
    cur[field] = list(layers or [])
    all_layers[bid] = cur
    st.session_state["format_layers_by_bundle"] = all_layers


def _clear_bundle_format_layers(bid: str, field: Optional[str] = None) -> None:
    all_layers = copy.deepcopy(st.session_state.get("format_layers_by_bundle", {}))
    if bid not in all_layers:
        return
    if field is None:
        all_layers[bid] = {}
    else:
        cur = copy.deepcopy(all_layers.get(bid, {}))
        cur[field] = []
        all_layers[bid] = cur
    st.session_state["format_layers_by_bundle"] = all_layers


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return not (a_end < b_start or b_end < a_start)


def _style_to_css(font_scale: float, bold: bool, italic: bool, align: str) -> str:
    styles: List[str] = []
    if abs(float(font_scale) - 1.0) > 0.001:
        styles.append(f"font-size:{float(font_scale):.2f}em")
    if bold:
        styles.append("font-weight:700")
    if italic:
        styles.append("font-style:italic")
    if align and align != "inherit":
        styles.append(f"text-align:{align}")
    return "; ".join(styles)


def _layer_apply_flag(layer: dict, prop: str) -> bool:
    flag_key = f"apply_{prop}"
    if flag_key in (layer or {}):
        return bool(layer.get(flag_key))
    if prop == "font_scale":
        return abs(float(layer.get("font_scale", 1.0)) - 1.0) > 0.001
    if prop == "bold":
        return bool(layer.get("bold", False))
    if prop == "italic":
        return bool(layer.get("italic", False))
    if prop == "align":
        return str(layer.get("align", "inherit")) != "inherit"
    return False


def _normalize_text_lines(text: str) -> List[str]:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return lines if lines else [""]


def _default_effective_style() -> Dict[str, Any]:
    return {
        "font_scale": 1.0,
        "bold": False,
        "italic": False,
        "align": "inherit",
    }


def _copy_effective_style(style: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = _default_effective_style()
    if not style:
        return base
    base["font_scale"] = float(style.get("font_scale", 1.0))
    base["bold"] = bool(style.get("bold", False))
    base["italic"] = bool(style.get("italic", False))
    base["align"] = str(style.get("align", "inherit") or "inherit")
    return base


def _effective_styles_equal(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return (
        abs(float(a.get("font_scale", 1.0)) - float(b.get("font_scale", 1.0))) <= 0.001
        and bool(a.get("bold", False)) == bool(b.get("bold", False))
        and bool(a.get("italic", False)) == bool(b.get("italic", False))
        and str(a.get("align", "inherit") or "inherit") == str(b.get("align", "inherit") or "inherit")
    )


def _effective_line_styles(total_lines: int, layers: List[dict]) -> List[Dict[str, Any]]:
    n = max(1, int(total_lines))
    styles = [_default_effective_style() for _ in range(n)]
    valid_layers = [
        x for x in (layers or [])
        if isinstance(x, dict) and int(x.get("start_line", 0)) > 0 and int(x.get("end_line", 0)) >= int(x.get("start_line", 0))
    ]
    if not valid_layers:
        return styles
    ordered = list(enumerate(valid_layers))
    ordered.sort(key=lambda t: (int(t[1].get("start_line", 1)), int(t[1].get("end_line", 1)), t[0]))
    for _, layer in ordered:
        start_ln = max(1, min(int(layer.get("start_line", 1)), n))
        end_ln = max(start_ln, min(int(layer.get("end_line", start_ln)), n))
        for idx in range(start_ln - 1, end_ln):
            if _layer_apply_flag(layer, "font_scale"):
                styles[idx]["font_scale"] = float(layer.get("font_scale", 1.0))
            if _layer_apply_flag(layer, "bold"):
                styles[idx]["bold"] = bool(layer.get("bold", False))
            if _layer_apply_flag(layer, "italic"):
                styles[idx]["italic"] = bool(layer.get("italic", False))
            if _layer_apply_flag(layer, "align"):
                styles[idx]["align"] = str(layer.get("align", "inherit") or "inherit")
    return styles


def _style_is_default(style: Dict[str, Any]) -> bool:
    return _effective_styles_equal(style, _default_effective_style())


def _layer_from_effective_style(style: Dict[str, Any], start_line: int, end_line: int) -> Optional[Dict[str, Any]]:
    payload = _copy_effective_style(style)
    apply_font_scale = abs(float(payload.get("font_scale", 1.0)) - 1.0) > 0.001
    apply_bold = bool(payload.get("bold", False))
    apply_italic = bool(payload.get("italic", False))
    apply_align = str(payload.get("align", "inherit") or "inherit") != "inherit"
    if not any([apply_font_scale, apply_bold, apply_italic, apply_align]):
        return None
    return {
        "start_line": int(start_line),
        "end_line": int(end_line),
        "font_scale": float(payload.get("font_scale", 1.0)),
        "bold": bool(payload.get("bold", False)),
        "italic": bool(payload.get("italic", False)),
        "align": str(payload.get("align", "inherit") or "inherit"),
        "apply_font_scale": apply_font_scale,
        "apply_bold": apply_bold,
        "apply_italic": apply_italic,
        "apply_align": apply_align,
    }


def _compress_effective_styles_to_layers(styles: List[Dict[str, Any]]) -> List[dict]:
    if not styles:
        return []
    out: List[dict] = []
    run_start = 1
    run_style = _copy_effective_style(styles[0])
    for idx in range(2, len(styles) + 1):
        cur_style = styles[idx - 1]
        if not _effective_styles_equal(run_style, cur_style):
            layer = _layer_from_effective_style(run_style, run_start, idx - 1)
            if layer is not None:
                out.append(layer)
            run_start = idx
            run_style = _copy_effective_style(cur_style)
    layer = _layer_from_effective_style(run_style, run_start, len(styles))
    if layer is not None:
        out.append(layer)
    return out


def _seed_style_for_insert(new_styles: List[Dict[str, Any]], old_styles: List[Dict[str, Any]], old_index: int) -> Dict[str, Any]:
    if new_styles:
        return _copy_effective_style(new_styles[-1])
    if 0 <= int(old_index) < len(old_styles):
        return _copy_effective_style(old_styles[int(old_index)])
    return _default_effective_style()


def _remap_format_layers_after_text_edit(old_text: str, new_text: str, layers: List[dict]) -> List[dict]:
    if not layers:
        return []
    old_lines = _normalize_text_lines(old_text)
    new_lines = _normalize_text_lines(new_text)
    old_styles = _effective_line_styles(len(old_lines), layers)
    if len(old_lines) == len(new_lines):
        return _compress_effective_styles_to_layers(old_styles[: len(new_lines)])

    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    new_styles: List[Dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old_count = max(0, i2 - i1)
        new_count = max(0, j2 - j1)
        if tag == "equal":
            for offset in range(new_count):
                new_styles.append(_copy_effective_style(old_styles[i1 + offset]))
        elif tag == "replace":
            common = min(old_count, new_count)
            for offset in range(common):
                new_styles.append(_copy_effective_style(old_styles[i1 + offset]))
            if new_count > common:
                if common > 0:
                    seed = _copy_effective_style(old_styles[i1 + common - 1])
                else:
                    seed = _seed_style_for_insert(new_styles, old_styles, i1)
                for _ in range(new_count - common):
                    new_styles.append(_copy_effective_style(seed))
        elif tag == "insert":
            seed = _seed_style_for_insert(new_styles, old_styles, i1)
            for _ in range(new_count):
                new_styles.append(_copy_effective_style(seed))
        elif tag == "delete":
            continue

    while len(new_styles) < len(new_lines):
        new_styles.append(_seed_style_for_insert(new_styles, old_styles, len(old_styles) - 1))
    if len(new_styles) > len(new_lines):
        new_styles = new_styles[: len(new_lines)]
    return _compress_effective_styles_to_layers(new_styles)


def _apply_format_layers_to_text(text: str, layers: List[dict]) -> str:
    base = _ensure_camfmt_markdown_attr(text or "")
    lines = _normalize_text_lines(base)
    if not lines:
        lines = [""]
    n = len(lines)
    effective_styles = _effective_line_styles(n, layers)
    css_by_line: List[str] = [
        _style_to_css(
            float(style.get("font_scale", 1.0)),
            bool(style.get("bold", False)),
            bool(style.get("italic", False)),
            str(style.get("align", "inherit") or "inherit"),
        )
        for style in effective_styles
    ]
    out: List[str] = []
    run_css: Optional[str] = None
    run_buf: List[str] = []

    def flush() -> None:
        nonlocal run_css, run_buf
        if not run_buf:
            return
        block = "\n".join(run_buf)
        if (run_css or "").strip():
            out.append(f'<div data-camfmt="1" markdown="1" style="{run_css}">\n{block}\n</div>')
        else:
            out.append(block)
        run_buf = []

    for line, css in zip(lines, css_by_line):
        if run_css is None:
            run_css = css
        if css != run_css:
            flush()
            run_css = css
        run_buf.append(line)
    flush()
    return "\n".join(out)

def _get_resolved_text(bid: str, field: str) -> str:
    raw = str(_get_bundle_text_data(bid).get(field, ""))
    layers = (_get_bundle_format_layers(bid) or {}).get(field, [])
    return _apply_format_layers_to_text(raw, layers)


def _get_resolved_bundle_text_data(bid: str) -> Dict[str, str]:
    td = _get_bundle_text_data(bid)
    for field in TEXT_EDITOR_FIELDS:
        td[field] = _get_resolved_text(bid, field)
    return td


def _get_effective_text_data_by_bundle() -> Dict[str, Dict[str, str]]:
    data = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
    for b in st.session_state.get("bundles", []) or []:
        try:
            bid = c19.bundle_id(b)
        except Exception:
            continue
        cur = copy.deepcopy(data.get(bid, {}))
        for field in TEXT_EDITOR_FIELDS:
            cur[field] = _get_resolved_text(bid, field)
        data[bid] = cur
    return data


def _get_editor_text(bid: str, target: str) -> str:
    widget_key = _editor_widget_key(target, bid)
    if widget_key in st.session_state:
        return _ensure_camfmt_markdown_attr(str(st.session_state.get(widget_key, "")))
    return _ensure_camfmt_markdown_attr(str(_get_bundle_text_data(bid).get(target, "")))


def _sync_editor_widget_state(bundle: c19.PassageBundle) -> None:
    bid = c19.bundle_id(bundle)
    refresh_bids = set(st.session_state.get("_editor_refresh_bids", []))
    must_refresh = bid in refresh_bids
    td = _get_bundle_text_data(bid)
    for target in TEXT_EDITOR_FIELDS:
        widget_key = _editor_widget_key(target, bid)
        if must_refresh or widget_key not in st.session_state:
            st.session_state[widget_key] = _ensure_camfmt_markdown_attr(str(td.get(target, "")))
    audio_key = f"audioscript_clean_{bid}"
    if must_refresh or audio_key not in st.session_state:
        st.session_state[audio_key] = str(td.get("audioscript_clean", ""))
    if must_refresh:
        refresh_bids.discard(bid)
        if refresh_bids:
            st.session_state["_editor_refresh_bids"] = sorted(refresh_bids)
        elif "_editor_refresh_bids" in st.session_state:
            del st.session_state["_editor_refresh_bids"]


def _split_text_blocks(text: str) -> List[Tuple[int, int, str]]:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines:
        return [(1, 1, "")]
    blocks: List[Tuple[int, int, str]] = []
    start: Optional[int] = None
    buf: List[str] = []
    for idx, line in enumerate(lines, start=1):
        if line.strip():
            if start is None:
                start = idx
            buf.append(line)
        else:
            if start is not None:
                blocks.append((start, idx - 1, "\n".join(buf)))
                start = None
                buf = []
    if start is not None:
        blocks.append((start, len(lines), "\n".join(buf)))
    return blocks or [(1, max(1, len(lines)), "\n".join(lines))]



def _block_label(block: Tuple[int, int, str]) -> str:
    start, end, body = block
    first = " ".join((body or "").split())[:90]
    if len(" ".join((body or "").split())) > 90:
        first += "..."
    return f"Dòng {start}-{end}: {first or '(trống)'}"



def _current_snapshot_path() -> Optional[Path]:
    prepared_pdf = st.session_state.get("prepared_pdf")
    prepared_cache_dir = st.session_state.get("prepared_cache_dir")
    prepared_tests = st.session_state.get("prepared_tests", [])
    if not prepared_pdf or not prepared_cache_dir:
        return None
    cache_dir = Path(str(prepared_cache_dir)).expanduser().resolve()
    snapshot_dir = cache_dir / "prepared_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    pdf_stem = Path(str(prepared_pdf)).stem or "prepared"
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", pdf_stem).strip("._") or "prepared"
    tests_label = "-".join(str(x) for x in (prepared_tests or ["all"]))
    return snapshot_dir / f"{safe_stem}__tests_{tests_label}.pkl"


def _list_snapshots(cache_dir: Path) -> List[Path]:
    snap_dir = cache_dir / "prepared_snapshots"
    if not snap_dir.exists():
        return []
    return sorted([p for p in snap_dir.glob("*.pkl") if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)


def _modern_display_title(bundle: c19.PassageBundle, skill: str = "reading") -> str:
    skill_norm = str(skill or "reading").strip().lower()
    if skill_norm == "listening":
        return f"Listening - Part {bundle.passage_num}"
    return f"Reading - Passage {bundle.passage_num}"


def _modern_audio_title(bundle: c19.PassageBundle, skill: str = "reading") -> str:
    return "Audio"


def _migrate_text_data_titles(
    bundles: List[c19.PassageBundle],
    text_data_by_bundle: Dict[str, Dict[str, Any]],
    prepared_skill: str = "reading",
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = copy.deepcopy(text_data_by_bundle or {})
    for bundle in (bundles or []):
        bid = c19.bundle_id(bundle)
        cur = copy.deepcopy(out.get(bid) or {})
        skill = str(cur.get("skill") or prepared_skill or "reading").strip().lower()

        display_title = str(cur.get("display_title") or "").strip()
        old_listening = re.compile(r"^Test\s+\d+\s*-\s*Listening\s+Section\s+\d+\s*\(Q\d+[-–]\d+\)$", re.I)
        old_reading = re.compile(r"^Test\s+\d+\s*-\s*Reading\s+Passage\s+\d+\s*\(Q\d+[-–]\d+\)$", re.I)
        if (not display_title) or old_listening.match(display_title) or old_reading.match(display_title):
            cur["display_title"] = _modern_display_title(bundle, skill)

        audio_title = str(cur.get("audio_title") or "").strip()
        old_audio = re.compile(r"^Audio\s+listening\s+part\s+\d+$", re.I)
        if skill == "listening" and ((not audio_title) or old_audio.match(audio_title)):
            cur["audio_title"] = _modern_audio_title(bundle, skill)

        out[bid] = cur
    return out


def _save_snapshot_file(snapshot_path: Path) -> Path:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "state": {k: copy.deepcopy(st.session_state[k]) for k in SNAPSHOT_STATE_KEYS if k in st.session_state},
    }
    with snapshot_path.open("wb") as f:
        pickle.dump(payload, f)
    return snapshot_path


def _autosave_snapshot() -> Optional[Path]:
    snapshot_path = _current_snapshot_path()
    if snapshot_path is None:
        return None
    return _save_snapshot_file(snapshot_path)


def _load_snapshot_file(snapshot_path: Path) -> None:
    with snapshot_path.open("rb") as f:
        payload = pickle.load(f)
    state = payload.get("state", {})
    for k, v in state.items():
        st.session_state[k] = v
    bundles = st.session_state.get("bundles") or []
    prepared_skill = str(st.session_state.get("prepared_skill") or "reading")
    st.session_state["text_data_by_bundle"] = _migrate_text_data_titles(bundles, st.session_state.get("text_data_by_bundle") or {}, prepared_skill)
    st.session_state["loaded_from_snapshot"] = True
    _mark_all_editor_refresh()


def _scan_to_dict(s: c19.PageScan) -> Dict[str, Any]:
    return dataclasses.asdict(s)


def _group_to_dict(g: c19.QuestionGroup) -> Dict[str, Any]:
    return dataclasses.asdict(g)


def _bundle_to_dict(b: c19.PassageBundle) -> Dict[str, Any]:
    data = dataclasses.asdict(b)
    data["qrange"] = list(b.qrange)
    return data


def _scan_from_dict(d: Dict[str, Any]) -> c19.PageScan:
    return c19.PageScan(**d)


def _group_from_dict(d: Dict[str, Any]) -> c19.QuestionGroup:
    return c19.QuestionGroup(**d)


def _bundle_from_dict(d: Dict[str, Any]) -> c19.PassageBundle:
    return c19.PassageBundle(
        test_num=int(d.get("test_num", 0)),
        passage_num=int(d.get("passage_num", 0)),
        qrange=(int((d.get("qrange") or [0, 0])[0]), int((d.get("qrange") or [0, 0])[1])),
        passage_pages=[int(x) for x in (d.get("passage_pages") or [])],
        question_pages=[int(x) for x in (d.get("question_pages") or [])],
        groups=[_group_from_dict(x) for x in (d.get("groups") or [])],
    )


def _serialize_keys_by_test(keys_by_test: Dict[int, Tuple[Dict[int, List[str]], List[Tuple[Tuple[int, int], List[str]]]]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for t, (singles, pairs) in (keys_by_test or {}).items():
        out[str(t)] = {
            "singles": {str(k): list(v or []) for k, v in (singles or {}).items()},
            "pairs": [{"qnums": [int(a), int(b)], "answers": list(vals or [])} for (a, b), vals in (pairs or [])],
        }
    return out


def _deserialize_keys_by_test(data: Dict[str, Any]) -> Dict[int, Tuple[Dict[int, List[str]], List[Tuple[Tuple[int, int], List[str]]]]]:
    out: Dict[int, Tuple[Dict[int, List[str]], List[Tuple[Tuple[int, int], List[str]]]]] = {}
    for t, payload in (data or {}).items():
        singles = {int(k): list(v or []) for k, v in ((payload or {}).get("singles") or {}).items()}
        pairs = []
        for item in ((payload or {}).get("pairs") or []):
            qnums = item.get("qnums") or [0, 0]
            pairs.append(((int(qnums[0]), int(qnums[1])), list(item.get("answers") or [])))
        out[int(t)] = (singles, pairs)
    return out


def _build_manifest_payload(source_label: str = "project") -> Dict[str, Any]:
    return {
        "manifest_type": "cambridge_plus_project",
        "manifest_version": MANIFEST_VERSION,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "source_label": source_label,
        "state": {
            "scans": [_scan_to_dict(x) for x in (st.session_state.get("scans") or [])],
            "bundles": [_bundle_to_dict(x) for x in (st.session_state.get("bundles") or [])],
            "keys_by_test": _serialize_keys_by_test(st.session_state.get("keys_by_test") or {}),
            "keys_original": _serialize_keys_by_test(st.session_state.get("keys_original") or {}),
            "prepared_pdf": str(st.session_state.get("prepared_pdf") or ""),
            "prepared_cache_dir": str(st.session_state.get("prepared_cache_dir") or ""),
            "prepared_tests": list(st.session_state.get("prepared_tests") or []),
            "prepared_lang": str(st.session_state.get("prepared_lang") or "eng"),
            "group_overrides": copy.deepcopy(st.session_state.get("group_overrides") or {}),
            "text_data_by_bundle": copy.deepcopy(st.session_state.get("text_data_by_bundle") or {}),
            "format_layers_by_bundle": copy.deepcopy(st.session_state.get("format_layers_by_bundle") or {}),
            "feedback_items_by_bundle": copy.deepcopy(st.session_state.get("feedback_items_by_bundle") or {}),
            "prepared_question_provider": str(st.session_state.get("prepared_question_provider") or ""),
            "prepared_passage_provider": str(st.session_state.get("prepared_passage_provider") or ""),
            "prepared_skill": str(st.session_state.get("prepared_skill") or "reading"),
            "prepared_answer_provider": str(st.session_state.get("prepared_answer_provider") or ""),
            "prepared_transcript_provider": str(st.session_state.get("prepared_transcript_provider") or ""),
            "prepared_transcript_page_mode": str(st.session_state.get("prepared_transcript_page_mode") or "auto"),
            "prepared_transcript_page_ranges": copy.deepcopy(st.session_state.get("prepared_transcript_page_ranges") or {}),
            "transcript_diagnostics_by_test": copy.deepcopy(st.session_state.get("transcript_diagnostics_by_test") or {}),
        },
    }


def _apply_manifest_payload(payload: Dict[str, Any]) -> None:
    state = copy.deepcopy((payload or {}).get("state") or {})
    st.session_state["scans"] = [_scan_from_dict(x) for x in (state.get("scans") or [])]
    st.session_state["bundles"] = [_bundle_from_dict(x) for x in (state.get("bundles") or [])]
    st.session_state["keys_by_test"] = _deserialize_keys_by_test(state.get("keys_by_test") or {})
    st.session_state["keys_original"] = _deserialize_keys_by_test(state.get("keys_original") or {})
    prepared_pdf = str(state.get("prepared_pdf") or "").strip()
    st.session_state["prepared_pdf"] = Path(prepared_pdf).expanduser().resolve() if prepared_pdf else None
    prepared_cache_dir = str(state.get("prepared_cache_dir") or "").strip()
    st.session_state["prepared_cache_dir"] = Path(prepared_cache_dir).expanduser().resolve() if prepared_cache_dir else None
    st.session_state["prepared_tests"] = [int(x) for x in (state.get("prepared_tests") or [])]
    st.session_state["prepared_lang"] = str(state.get("prepared_lang") or "eng")
    st.session_state["group_overrides"] = state.get("group_overrides") or {}
    st.session_state["text_data_by_bundle"] = _migrate_text_data_titles(st.session_state.get("bundles") or [], state.get("text_data_by_bundle") or {}, str(state.get("prepared_skill") or "reading"))
    st.session_state["format_layers_by_bundle"] = state.get("format_layers_by_bundle") or {}
    st.session_state["feedback_items_by_bundle"] = state.get("feedback_items_by_bundle") or {}
    st.session_state["prepared_question_provider"] = str(state.get("prepared_question_provider") or "")
    st.session_state["prepared_passage_provider"] = str(state.get("prepared_passage_provider") or "")
    st.session_state["prepared_skill"] = str(state.get("prepared_skill") or "reading")
    st.session_state["prepared_answer_provider"] = str(state.get("prepared_answer_provider") or "")
    st.session_state["prepared_transcript_provider"] = str(state.get("prepared_transcript_provider") or "")
    st.session_state["prepared_transcript_page_mode"] = str(state.get("prepared_transcript_page_mode") or "auto")
    st.session_state["prepared_transcript_page_ranges"] = state.get("prepared_transcript_page_ranges") or {}
    st.session_state["transcript_diagnostics_by_test"] = state.get("transcript_diagnostics_by_test") or {}
    st.session_state["loaded_from_snapshot"] = False
    _mark_all_editor_refresh()


def _save_manifest_file(manifest_path: Path, source_label: str = "project") -> Path:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _build_manifest_payload(source_label=source_label)
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def _manifest_comment(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"<!-- {MANIFEST_MARKER}{b64} -->"


def _strip_existing_manifest_comment(text: str) -> str:
    return re.sub(r"<!--\s*" + re.escape(MANIFEST_MARKER) + r"[A-Za-z0-9+/=\s]+-->", "", text, flags=re.DOTALL)


def _embed_manifest_in_xml_file(xml_path: Path, payload: Dict[str, Any]) -> None:
    txt = _strip_existing_manifest_comment(xml_path.read_text(encoding="utf-8", errors="ignore"))
    comment = _manifest_comment(payload)
    if txt.startswith("<?xml") and "?>" in txt:
        pos = txt.find("?>") + 2
        txt = txt[:pos] + "\n" + comment + txt[pos:]
    else:
        txt = comment + "\n" + txt
    xml_path.write_text(txt, encoding="utf-8")


def _embed_manifest_in_html_file(html_path: Path, payload: Dict[str, Any]) -> None:
    txt = _strip_existing_manifest_comment(html_path.read_text(encoding="utf-8", errors="ignore"))
    comment = _manifest_comment(payload)
    if "<head>" in txt:
        txt = txt.replace("<head>", "<head>\n" + comment, 1)
    else:
        txt = comment + "\n" + txt
    html_path.write_text(txt, encoding="utf-8")


def _extract_embedded_manifest_from_text(text: str) -> Optional[Dict[str, Any]]:
    m = re.search(re.escape(MANIFEST_MARKER) + r"([A-Za-z0-9+/=\s]+)", text, flags=re.DOTALL)
    if not m:
        return None
    b64 = re.sub(r"\s+", "", m.group(1))
    raw = base64.b64decode(b64.encode("ascii"))
    return json.loads(raw.decode("utf-8"))


def _export_listening_transcript_companions(out_dir: Path, bundles: List[c19.PassageBundle], text_data_by_bundle: Dict[str, Dict[str, Any]]) -> List[Path]:
    sections: List[Tuple[str, str]] = []
    for b in sorted(bundles, key=lambda x: (x.test_num, x.passage_num)):
        bid = c19.bundle_id(b)
        td = (text_data_by_bundle or {}).get(bid) or {}
        if str(td.get("skill", "")).strip() != "listening":
            continue
        transcript = str(td.get("audioscript_clean", "") or td.get("audioscript_raw", "") or "").strip()
        if not transcript:
            continue
        title = td.get("display_title") or _modern_display_title(b, "listening")
        sections.append((title, transcript))
    if not sections:
        return []
    html_parts = ["<!doctype html><html><head><meta charset='utf-8'><title>Listening transcripts review</title></head><body>", "<h1>Listening transcripts review</h1>"]
    md_parts = ["# Listening transcripts review", ""]
    for title, transcript in sections:
        html_parts.append(f"<h2>{title}</h2>")
        html_parts.append(c19._style_markdown_tables_in_html(c19._markdown_to_html(transcript)))
        html_parts.append("<hr />")
        md_parts.extend([f"## {title}", "", transcript, ""])
    html_parts.append("</body></html>")
    html_path = out_dir / "listening_transcripts_review.html"
    md_path = out_dir / "listening_transcripts_review.md"
    html_path.write_text("\n".join(html_parts), encoding="utf-8")
    md_path.write_text("\n".join(md_parts), encoding="utf-8")
    return [html_path, md_path]


def _export_listening_audio_snippet_companions(out_dir: Path, bundles: List[c19.PassageBundle], text_data_by_bundle: Dict[str, Dict[str, Any]]) -> List[Path]:
    sections: List[Tuple[str, str]] = []
    for b in sorted(bundles, key=lambda x: (x.test_num, x.passage_num)):
        bid = c19.bundle_id(b)
        td = (text_data_by_bundle or {}).get(bid) or {}
        if str(td.get("skill", "")).strip() != "listening":
            continue
        audio_path = str(td.get("audio_override_path", "")).strip()
        if not audio_path or not Path(audio_path).exists():
            continue
        fname = Path(audio_path).name
        lockid = str(td.get("audio_lockid", f"test{b.test_num}_part{b.passage_num}")).strip()
        title = str(td.get("audio_title", _modern_audio_title(b, "listening"))).strip()
        snippet = f'<p>{title}</p>\n<p><audio preload="none" controls="controls" data-lockid="{lockid}"><source src="@@PLUGINFILE@@/{fname}" type="{c19._mime_for_name(fname)}">@@PLUGINFILE@@/{fname}</audio></p>'
        sections.append((_modern_display_title(b, "listening"), snippet))
    if not sections:
        return []
    html_parts = ["<!doctype html><html><head><meta charset='utf-8'><title>Listening audio description snippets</title></head><body>", "<h1>Listening audio description snippets</h1>", "<p>Copy từng snippet này vào Description nếu bạn muốn giữ cách nhúng audio kiểu Description. Dùng chung với Additional HTML listen-once của bạn.</p>"]
    md_parts = ["# Listening audio description snippets", "", "Copy từng snippet vào Description nếu muốn.", ""]
    for title, snippet in sections:
        html_parts.append(f"<h2>{title}</h2><pre>{snippet.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')}</pre><hr />")
        md_parts.extend([f"## {title}", "", "```html", snippet, "```", ""])
    html_parts.append("</body></html>")
    html_path = out_dir / "listening_audio_description_snippets.html"
    md_path = out_dir / "listening_audio_description_snippets.md"
    html_path.write_text("\n".join(html_parts), encoding="utf-8")
    md_path.write_text("\n".join(md_parts), encoding="utf-8")
    return [html_path, md_path]




def _export_listenonce_additional_html_companion(out_dir: Path) -> List[Path]:
    head_content = r"""<style>
/* ===== Cambridge listen-once audio: visual + behaviour helpers ===== */

.cambridge-audio-wrap {
  margin: 0 0 12px 0;
  padding: 10px 12px;
  border: 1px solid #d7e4ff;
  border-radius: 14px;
  background: linear-gradient(180deg, #f8fbff 0%, #eef4ff 100%);
  box-shadow: 0 2px 10px rgba(47, 103, 216, 0.08);
}
.cambridge-audio-wrap:empty {
  display: none !important;
}
.cambridge-audio-wrap:empty + .mediaplugin,
.cambridge-audio-wrap:empty + div.mediaplugin_videojs,
.cambridge-audio-wrap:empty + .mediaplugin.mediaplugin_videojs {
  margin-top: 0 !important;
}

audio[data-cambridge-audio="1"],
.video-js[data-cambridge-audio="1"],
.video-js.cambridge-audio-player {
  width: 100% !important;
  max-width: 100% !important;
  border-radius: 14px !important;
}

audio[data-cambridge-audio="1"] {
  color-scheme: light;
  accent-color: #2f67d8;
  background: #eef4ff !important;
}

audio[data-cambridge-audio="1"]::-webkit-media-controls-enclosure,
audio[data-cambridge-audio="1"]::-webkit-media-controls-panel {
  background: #eef4ff !important;
  border-radius: 12px !important;
}

.video-js[data-cambridge-audio="1"],
.video-js.cambridge-audio-player {
  background: #eef4ff !important;
  border: 1px solid #d7e4ff !important;
  box-shadow: 0 2px 10px rgba(47, 103, 216, 0.08);
}

.video-js[data-cambridge-audio="1"] .vjs-control-bar,
.video-js.cambridge-audio-player .vjs-control-bar {
  background: #eef4ff !important;
  color: #24324a !important;
}

.video-js[data-cambridge-audio="1"] .vjs-button,
.video-js[data-cambridge-audio="1"] .vjs-time-control,
.video-js.cambridge-audio-player .vjs-button,
.video-js.cambridge-audio-player .vjs-time-control {
  color: #24324a !important;
}

.video-js[data-cambridge-audio="1"] .vjs-volume-bar,
.video-js[data-cambridge-audio="1"] .vjs-progress-holder,
.video-js.cambridge-audio-player .vjs-volume-bar,
.video-js.cambridge-audio-player .vjs-progress-holder {
  background: rgba(36, 50, 74, 0.18) !important;
}

.video-js[data-cambridge-audio="1"] .vjs-play-progress,
.video-js[data-cambridge-audio="1"] .vjs-volume-level,
.video-js.cambridge-audio-player .vjs-play-progress,
.video-js.cambridge-audio-player .vjs-volume-level {
  background: #2f67d8 !important;
}

/* ATTEMPT: hide seek UI */
body#page-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-progress-control,
body#page-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-progress-holder,
body#page-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-slider,
body#page-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-mouse-display,
body#page-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-time-tooltip,
body#page-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-play-progress,
body#page-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-load-progress,
body.path-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-progress-control,
body.path-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-progress-holder,
body.path-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-slider,
body.path-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-mouse-display,
body.path-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-time-tooltip,
body.path-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-play-progress,
body.path-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-load-progress {
  display: none !important;
}
body#page-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-progress-control.vjs-control,
body.path-mod-quiz-attempt .cambridge-audio-top .video-js .vjs-progress-control.vjs-control {
  width: 0 !important;
  min-width: 0 !important;
  max-width: 0 !important;
  padding: 0 !important;
  margin: 0 !important;
  flex: 0 0 0 !important;
}

/* REVIEW: always interactive again */
body#page-mod-quiz-review audio[data-cambridge-audio="1"],
body.path-mod-quiz-review audio[data-cambridge-audio="1"] {
  pointer-events: auto !important;
  opacity: 1 !important;
}
body#page-mod-quiz-review .video-js[data-cambridge-audio="1"],
body.path-mod-quiz-review .video-js[data-cambridge-audio="1"],
body#page-mod-quiz-review .video-js.cambridge-audio-player,
body.path-mod-quiz-review .video-js.cambridge-audio-player {
  pointer-events: auto !important;
  opacity: 1 !important;
}

.listenonce-note {
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: 10px;
  background: #fff4d6;
  color: #5a4300;
  font-weight: 600;
  font-size: 0.95em;
}
</style>"""

    top_content = r"""<script>
/**
 * TỰ ĐỘNG ÁP DỤNG BỘ LỌC CHO NGÂN HÀNG CÂU HỎI / POPUP
 * - Chỉ áp dụng với các ô trong vùng filter ('.filter-group[data-table-region]')
 * - KHÔNG tác động tới checkbox chọn câu hỏi trong bảng → không bị tự bỏ tích.
 */
(function () {
  const applyTimers = new WeakMap();

  function getFilterScope(element) {
    if (!element) return null;

    let scope = element.closest(
      '.questionbankformforpopup,' +
      '[data-region="question-bank"],' +
      '[data-region="filter"]'
    );
    if (scope) return scope;

    let current = element.closest('form, .filter-group, .card, .container, .container-fluid') || element.parentElement;
    while (current && current !== document.body) {
      if (current.querySelector && current.querySelector('button[data-filteraction="apply"]')) {
        return current;
      }
      current = current.parentElement;
    }
    return null;
  }

  function clickApply(scope) {
    if (!scope) return;
    let btn =
      scope.querySelector('[data-filterregion="actions"] button[data-filteraction="apply"]') ||
      scope.querySelector('button[data-filteraction="apply"]');
    if (btn && !btn.disabled) {
      btn.click();
    }
  }

  function scheduleApply(scope, delay) {
    if (!scope) return;
    const old = applyTimers.get(scope);
    if (old) clearTimeout(old);
    const t = setTimeout(() => clickApply(scope), delay);
    applyTimers.set(scope, t);
  }

  document.addEventListener('change', (e) => {
    const el = e.target;
    if (!el.matches('select, input[type="checkbox"], input[type="radio"]')) return;
    const filterGroup = el.closest('.filter-group[data-table-region], [data-region="filter"]');
    if (!filterGroup) return;
    const scope = getFilterScope(filterGroup);
    if (!scope) return;
    scheduleApply(scope, 150);
  });

  document.addEventListener('input', (e) => {
    const el = e.target;
    if (!el.matches('input[data-fieldtype="autocomplete"]')) return;
    const filterGroup = el.closest('.filter-group[data-table-region], [data-region="filter"]');
    if (!filterGroup) return;
    const scope = getFilterScope(filterGroup);
    if (!scope) return;
    scheduleApply(scope, 250);
  });

  document.addEventListener('click', (e) => {
    const target = e.target;
    const filterGroup = target.closest('.filter-group[data-table-region], [data-region="filter"]');
    if (!filterGroup) return;
    const scope = getFilterScope(filterGroup);
    if (!scope) return;
    const isSuggestion = target.closest('.form-autocomplete-suggestions [role="option"]');
    const isRemoveToken = target.closest(
      '.form-autocomplete-selection .badge,' +
      '.form-autocomplete-selection [data-remove],' +
      '.form-autocomplete-selection .fa-times'
    );
    if (isSuggestion || isRemoveToken) {
      scheduleApply(scope, 150);
    }
  });
})();
</script>"""

    before_close = r"""<script>
(function () {
  const AUDIO_SELECTOR = 'audio[data-cambridge-audio="1"], audio[data-lockid]';

  function pageMode() {
    const path = String(location.pathname || '').toLowerCase();
    if (path.includes('/mod/quiz/review.php')) return 'review';
    if (path.includes('/mod/quiz/attempt.php')) return 'attempt';
    if (path.includes('/mod/quiz/summary.php')) return 'summary';
    return 'other';
  }

  function getAttemptId() {
    const params = new URLSearchParams(location.search || '');
    return (
      params.get('attempt') ||
      params.get('attemptid') ||
      (document.querySelector('input[name="attempt"], input[name="attemptid"]') || {}).value ||
      ''
    );
  }

  function getLockId(audio) {
    return String(audio.getAttribute('data-lockid') || '').trim();
  }

  function storageKeys(audio) {
    const attempt = getAttemptId();
    const lockId = getLockId(audio);
    if (!attempt || !lockId) return [];
    return [`listenonce:${attempt}:${lockId}`];
  }

  function isUsed(audio) {
    return storageKeys(audio).some((k) => {
      try { if (localStorage.getItem(k) === '1') return true; } catch (e) {}
      try { if (sessionStorage.getItem(k) === '1') return true; } catch (e) {}
      return false;
    });
  }

  function setUsed(audio) {
    storageKeys(audio).forEach((k) => {
      try { localStorage.setItem(k, '1'); } catch (e) {}
      try { sessionStorage.setItem(k, '1'); } catch (e) {}
    });
  }

  function getWrapper(audio) {
    return audio.closest('.video-js') || audio;
  }

  function getPlayer(audio) {
    const wrapper = getWrapper(audio);
    if (!(window.videojs && wrapper && wrapper.id && wrapper.classList && wrapper.classList.contains('video-js'))) return null;
    try { return window.videojs.getPlayer(wrapper.id) || null; } catch (e) { return null; }
  }

  function getNoteAnchor(audio) {
    return getWrapper(audio);
  }

  function showNote(audio, message) {
    const anchor = getNoteAnchor(audio);
    let note = anchor.nextElementSibling;
    if (!note || !note.classList || !note.classList.contains('listenonce-note')) {
      note = document.createElement('div');
      note.className = 'listenonce-note';
      anchor.insertAdjacentElement('afterend', note);
    }
    note.textContent = message || 'Bạn đã dùng lượt nghe (không thể nghe lại trong lần làm bài này).';
  }

  function removeNote(audio) {
    const anchor = getNoteAnchor(audio);
    const note = anchor.nextElementSibling;
    if (note && note.classList && note.classList.contains('listenonce-note')) note.remove();
  }

  function disableUi(audio) {
    const wrapper = getWrapper(audio);
    audio.setAttribute('data-locked', '1');
    wrapper.setAttribute('data-locked', '1');
    audio.style.pointerEvents = 'none';
    audio.style.opacity = '0.72';
    wrapper.style.pointerEvents = 'none';
    wrapper.style.opacity = '0.72';
    audio.setAttribute('tabindex', '-1');
    wrapper.setAttribute('tabindex', '-1');
    wrapper.classList.add('vjs-controls-disabled');
    try { audio.pause(); } catch (e) {}
    const player = getPlayer(audio);
    if (player) {
      try { player.pause(); } catch (e) {}
      try { player.controls(false); } catch (e) {}
    }
  }

  function enableUi(audio) {
    const wrapper = getWrapper(audio);
    audio.removeAttribute('data-locked');
    wrapper.removeAttribute('data-locked');
    audio.style.pointerEvents = '';
    audio.style.opacity = '';
    wrapper.style.pointerEvents = '';
    wrapper.style.opacity = '';
    audio.removeAttribute('tabindex');
    wrapper.removeAttribute('tabindex');
    wrapper.classList.remove('vjs-controls-disabled');
    removeNote(audio);
    const player = getPlayer(audio);
    if (player) {
      try { player.controls(true); } catch (e) {}
      try { player.userActive(true); } catch (e) {}
    }
  }

  function detachHandlers(audio) {
    const handlers = audio.__cambridgeHandlers;
    if (!handlers) return;
    if (handlers.audio) {
      Object.entries(handlers.audio).forEach(([evt, fn]) => {
        try { audio.removeEventListener(evt, fn); } catch (e) {}
      });
    }
    const player = getPlayer(audio);
    if (player && handlers.player) {
      Object.entries(handlers.player).forEach(([evt, fn]) => {
        try { player.off(evt, fn); } catch (e) {}
      });
    }
    audio.__cambridgeHandlers = null;
  }

  function unlockForReview(audio) {
    detachHandlers(audio);
    enableUi(audio);
    audio.__cambridgeMode = 'review';
    audio.__cambridgeStarted = false;
    audio.__cambridgeEnded = false;
    try {
      audio.controls = true;
      audio.autoplay = false;
      audio.removeAttribute('autoplay');
      audio.playbackRate = 1;
      audio.setAttribute('controlsList', 'nodownload noplaybackrate noremoteplayback');
    } catch (e) {}
    const player = getPlayer(audio);
    if (player) {
      try { player.controls(true); } catch (e) {}
      try { player.userActive(true); } catch (e) {}
      try { player.playbackRate(1); } catch (e) {}
    }
  }

  function lockForAttempt(audio, reason) {
    detachHandlers(audio);
    audio.__cambridgeMode = 'attempt-locked';
    disableUi(audio);
    showNote(audio, reason || 'Bạn đã dùng lượt nghe (không thể nghe lại trong lần làm bài này).');
  }

  function initAttemptAudio(audio) {
    if (audio.__cambridgeMode === 'attempt-init') {
      return;
    }
    if (audio.__cambridgeMode === 'attempt-locked') {
      if (isUsed(audio)) lockForAttempt(audio);
      return;
    }
    detachHandlers(audio);
    enableUi(audio);
    audio.__cambridgeMode = 'attempt-init';
    audio.__cambridgeStarted = false;
    audio.__cambridgeEnded = false;

    if (isUsed(audio)) {
      lockForAttempt(audio);
      return;
    }

    let lastTime = 0;
    const audioHandlers = {};
    const playerHandlers = {};
    const player = getPlayer(audio);

    function currentTime() {
      if (player) {
        try { return Number(player.currentTime() || 0); } catch (e) {}
      }
      try { return Number(audio.currentTime || 0); } catch (e) {}
      return 0;
    }

    function isPaused() {
      if (player) {
        try { return !!player.paused(); } catch (e) {}
      }
      return !!audio.paused;
    }

    function isEnded() {
      if (player) {
        try { return !!player.ended(); } catch (e) {}
      }
      return !!audio.ended;
    }

    function doTimeupdate() {
      const t = currentTime();
      if (!isPaused()) lastTime = t;
      if (!audio.__cambridgeStarted && t > 0.75) {
        audio.__cambridgeStarted = true;
        setUsed(audio);
      }
    }

    function doPause() {
      if (!isEnded() && !audio.__cambridgeEnded && !audio.hasAttribute('data-locked')) {
        setTimeout(() => {
          if ((pageMode() === 'attempt' || pageMode() === 'summary') &&
              isPaused() &&
              !isEnded() &&
              !audio.__cambridgeEnded &&
              !audio.hasAttribute('data-locked')) {
            if (player) {
              try { player.play(); return; } catch (e) {}
            }
            audio.play().catch(() => {});
          }
        }, 40);
      }
    }

    function doEnded() {
      audio.__cambridgeEnded = true;
      setUsed(audio);
      lockForAttempt(audio, 'Đã nghe xong (không thể nghe lại trong lần làm bài này).');
    }

    function doSeeking() {
      const t = currentTime();
      if (Math.abs(t - lastTime) > 0.05) {
        if (player) {
          try { player.currentTime(lastTime); return; } catch (e) {}
        }
        audio.currentTime = lastTime;
      }
    }

    function doRatechange() {
      if (player) {
        try { if (player.playbackRate() !== 1) player.playbackRate(1); } catch (e) {}
      }
      try { if (audio.playbackRate !== 1) audio.playbackRate = 1; } catch (e) {}
    }

    audioHandlers.timeupdate = doTimeupdate;
    audioHandlers.pause = doPause;
    audioHandlers.ended = doEnded;
    audioHandlers.seeking = doSeeking;
    audioHandlers.ratechange = doRatechange;
    Object.entries(audioHandlers).forEach(([evt, fn]) => audio.addEventListener(evt, fn));

    if (player) {
      playerHandlers.timeupdate = doTimeupdate;
      playerHandlers.pause = doPause;
      playerHandlers.ended = doEnded;
      playerHandlers.seeking = doSeeking;
      playerHandlers.ratechange = doRatechange;
      Object.entries(playerHandlers).forEach(([evt, fn]) => {
        try { player.on(evt, fn); } catch (e) {}
      });
      try { player.controls(true); } catch (e) {}
      try { player.userActive(true); } catch (e) {}
      try { player.playbackRate(1); } catch (e) {}
    }

    audio.__cambridgeHandlers = { audio: audioHandlers, player: playerHandlers };

    try {
      audio.controls = true;
      audio.autoplay = false;
      audio.removeAttribute('autoplay');
      audio.setAttribute('controlsList', 'nodownload noplaybackrate noremoteplayback');
    } catch (e) {}
  }

  function scan() {
    const mode = pageMode();
    const audios = document.querySelectorAll(AUDIO_SELECTOR);
    audios.forEach((audio) => {
      if (mode === 'review') unlockForReview(audio);
      else if (mode === 'attempt' || mode === 'summary') initAttemptAudio(audio);
    });
  }

  let queued = false;
  function queueScan() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      scan();
    });
  }

  function boot() {
    scan();
    const mo = new MutationObserver(queueScan);
    mo.observe(document.body || document.documentElement, { childList: true, subtree: true });
    window.addEventListener('pageshow', queueScan);
    window.addEventListener('hashchange', queueScan);
    window.addEventListener('popstate', queueScan);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) queueScan(); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
</script>"""

    readme = """Use 3 snippet files together in Moodle Additional HTML:

1. moodle_additional_html_head_listenonce.html -> Within HEAD
2. moodle_additional_html_topofbody_listenonce.html -> After BODY is opened
3. moodle_additional_html_before_body_close_listenonce.html -> Before BODY is closed

Purpose:
- attempt + summary: listen once
- review: can listen again freely
- keeps question-bank filter auto-apply logic
"""

    paths = []
    p1 = out_dir / 'moodle_additional_html_head_listenonce.html'
    p1.write_text(head_content, encoding='utf-8')
    paths.append(p1)
    p2 = out_dir / 'moodle_additional_html_topofbody_listenonce.html'
    p2.write_text(top_content, encoding='utf-8')
    paths.append(p2)
    p3 = out_dir / 'moodle_additional_html_before_body_close_listenonce.html'
    p3.write_text(before_close, encoding='utf-8')
    paths.append(p3)
    p4 = out_dir / 'moodle_additional_html_listenonce_README.txt'
    p4.write_text(readme, encoding='utf-8')
    paths.append(p4)
    # compatibility file for old workflow
    compat = out_dir / 'moodle_additional_html_listenonce_enhanced.html'
    compat.write_text(before_close, encoding='utf-8')
    paths.append(compat)
    return paths

def _candidate_companion_manifest_paths(path: Path) -> List[Path]:
    return [
        path.with_name(path.name + ".camplus.json"),
        path.with_name(path.stem + ".camplus.json"),
        path.with_suffix(path.suffix + ".camplus.json"),
        path.with_suffix(".camplus.json"),
    ]


def _load_manifest_from_local_path(path: Path) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".json" or path.name.endswith(".camplus.json"):
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    text = path.read_text(encoding="utf-8", errors="ignore")
    payload = _extract_embedded_manifest_from_text(text)
    if payload:
        return payload
    for cand in _candidate_companion_manifest_paths(path):
        if cand.exists():
            return json.loads(cand.read_text(encoding="utf-8", errors="ignore"))
    raise RuntimeError("File này chưa có manifest nhúng/đi kèm. Hãy export lại bằng v7_full_fix6 hoặc import trực tiếp file .camplus.json.")


def _load_manifest_from_uploaded_bytes(name: str, data: bytes) -> Dict[str, Any]:
    lname = (name or "").lower()
    if lname.endswith(".json") or lname.endswith(".camplus.json"):
        return json.loads(data.decode("utf-8", errors="ignore"))
    text = data.decode("utf-8", errors="ignore")
    payload = _extract_embedded_manifest_from_text(text)
    if payload:
        return payload
    raise RuntimeError("File upload này chưa có manifest nhúng. Với export mới từ v7_full_fix6, hãy import lại XML/HTML có manifest nhúng hoặc upload file .camplus.json đi kèm.")


def _strip_camfmt_wrapper(text: str) -> str:
    s = (text or "").strip()
    pattern = r'^\s*<div\s+[^>]*data-camfmt=["\']1["\'][^>]*>\s*(.*?)\s*</div>\s*$'
    while True:
        new_s = re.sub(pattern, r"\1", s, flags=re.DOTALL | re.IGNORECASE)
        if new_s == s:
            break
        s = new_s.strip()
    return s


def _ensure_camfmt_markdown_attr(text: str) -> str:
    src = text or ""
    def repl(m: re.Match[str]) -> str:
        tag = m.group(0)
        if re.search(r"\bmarkdown\s*=\s*['\"]1['\"]", tag, flags=re.IGNORECASE):
            return tag
        return tag[:-1] + ' markdown="1">'
    return re.sub(r"<div\s+[^>]*data-camfmt=['\"]1['\"][^>]*>", repl, src, flags=re.IGNORECASE)


def _apply_simple_formatting(text: str, start_line: int, end_line: int, font_scale: float, bold: bool, italic: bool, align: str, remove_only: bool = False) -> str:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.split("\n")
    if not lines:
        lines = [""]
    n = len(lines)
    start = max(1, min(int(start_line), n))
    end = max(start, min(int(end_line), n))
    selected = "\n".join(lines[start - 1:end])
    selected = _strip_camfmt_wrapper(selected)
    if remove_only:
        replacement = selected
    else:
        styles: List[str] = []
        if abs(float(font_scale) - 1.0) > 0.001:
            styles.append(f"font-size:{float(font_scale):.2f}em")
        if bold:
            styles.append("font-weight:700")
        if italic:
            styles.append("font-style:italic")
        if align and align != "inherit":
            styles.append(f"text-align:{align}")
        replacement = selected
        if styles:
            replacement = f'<div data-camfmt="1" markdown="1" style="{"; ".join(styles)}">\n{selected}\n</div>'
    new_lines = lines[: start - 1] + replacement.split("\n") + lines[end:]
    return "\n".join(new_lines)


def _save_text_edits(bundle: c19.PassageBundle) -> None:
    bid = c19.bundle_id(bundle)
    all_layers = copy.deepcopy(st.session_state.get("format_layers_by_bundle", {}))
    current_bundle_layers = copy.deepcopy(all_layers.get(bid, {}))
    for field in TEXT_EDITOR_FIELDS:
        widget_key = _editor_widget_key(field, bid)
        old_value = str(_get_bundle_text_data(bid).get(field, ""))
        current = str(st.session_state.get(widget_key, old_value))
        if current != old_value:
            remapped = _remap_format_layers_after_text_edit(old_value, current, current_bundle_layers.get(field, []) or [])
            current_bundle_layers[field] = remapped
        _set_bundle_text_field(bid, field, current)
    all_layers[bid] = current_bundle_layers
    st.session_state["format_layers_by_bundle"] = all_layers
    td = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
    cur = copy.deepcopy(td.get(bid, {}))
    audio_key = f"audioscript_clean_{bid}"
    if audio_key in st.session_state:
        cur["audioscript_clean"] = str(st.session_state.get(audio_key, cur.get("audioscript_clean", "")))
    cur["question_image_page_override"] = str(st.session_state.get(f"question_image_page_override_{bid}", cur.get("question_image_page_override", ""))).strip()
    cur["question_image_position"] = str(st.session_state.get(f"question_image_position_{bid}", cur.get("question_image_position", "top"))).strip() or "top"
    cur["question_image_after_label"] = str(st.session_state.get(f"question_image_after_label_{bid}", cur.get("question_image_after_label", ""))).strip()
    cur["question_image_after_keyword"] = str(st.session_state.get(f"question_image_after_keyword_{bid}", cur.get("question_image_after_keyword", ""))).strip()
    cur["question_image_disabled"] = bool(st.session_state.get(f"question_image_disabled_{bid}", cur.get("question_image_disabled", False)))
    cur["audio_lockid"] = str(st.session_state.get(f"audio_lockid_{bid}", cur.get("audio_lockid", ""))).strip()
    cur["audio_title"] = str(st.session_state.get(f"audio_title_{bid}", cur.get("audio_title", ""))).strip()
    cur["audio_show_in_review"] = bool(st.session_state.get(f"audio_show_in_review_{bid}", cur.get("audio_show_in_review", True)))
    cur["choice_layout"] = str(st.session_state.get(f"choice_layout_{bid}", cur.get("choice_layout", "vertical"))).strip() or "vertical"
    cur["passage_label_style"] = str(st.session_state.get(f"passage_label_style_{bid}", cur.get("passage_label_style", "plain"))).strip() or "plain"
    td[bid] = cur
    st.session_state["text_data_by_bundle"] = td


def _apply_formatting_to_field(
    bundle: c19.PassageBundle,
    target: str,
    start_line: int,
    end_line: int,
    font_scale: float,
    bold: bool,
    italic: bool,
    align: str,
    props: List[str],
    remove_only: bool = False,
) -> str:
    bid = c19.bundle_id(bundle)
    current_text = str(_get_bundle_text_data(bid).get(target, ""))
    total_lines = max(1, len(_normalize_text_lines(current_text)))
    start = max(1, min(int(start_line), total_lines))
    end = max(start, min(int(end_line), total_lines))
    effective_styles = _effective_line_styles(total_lines, list((_get_bundle_format_layers(bid) or {}).get(target, [])))

    if remove_only:
        for idx in range(start - 1, end):
            effective_styles[idx] = _default_effective_style()
    else:
        if not props:
            return target
        for idx in range(start - 1, end):
            cur_style = _copy_effective_style(effective_styles[idx])
            if "size" in props:
                cur_style["font_scale"] = float(font_scale)
            if "bold" in props:
                cur_style["bold"] = bool(bold)
            if "italic" in props:
                cur_style["italic"] = bool(italic)
            if "align" in props:
                cur_style["align"] = str(align or "inherit")
            effective_styles[idx] = cur_style

    _set_bundle_format_layers(bid, target, _compress_effective_styles_to_layers(effective_styles))
    _autosave_snapshot()
    return target


def _apply_text_formatting(bundle: c19.PassageBundle, remove_only: bool = False) -> str:
    bid = c19.bundle_id(bundle)
    target = str(st.session_state.get(f"fmt_target_{bid}", "question_markup"))
    current_text = str(_get_bundle_text_data(bid).get(target, ""))
    total_lines = max(1, len(_normalize_text_lines(current_text)))
    mode = str(st.session_state.get(f"fmt_mode_{bid}", "Theo block"))

    if mode == "Toàn bộ":
        start_line, end_line = 1, total_lines
    elif mode == "Theo dòng":
        start_line = int(st.session_state.get(f"fmt_start_{bid}", 1))
        end_line = int(st.session_state.get(f"fmt_end_{bid}", total_lines))
    else:
        blocks = _split_text_blocks(current_text)
        block_index = int(st.session_state.get(f"fmt_block_idx_{bid}", 0))
        block_index = max(0, min(block_index, len(blocks) - 1))
        start_line, end_line, _ = blocks[block_index]

    font_scale = float(st.session_state.get(f"fmt_size_{bid}", 1.0))
    bold = bool(st.session_state.get(f"fmt_bold_{bid}", False))
    italic = bool(st.session_state.get(f"fmt_italic_{bid}", False))
    align = str(st.session_state.get(f"fmt_align_{bid}", "inherit"))
    props = list(st.session_state.get(f"fmt_props_{bid}", []) or [])
    return _apply_formatting_to_field(
        bundle=bundle,
        target=target,
        start_line=start_line,
        end_line=end_line,
        font_scale=font_scale,
        bold=bold,
        italic=italic,
        align=align,
        props=props,
        remove_only=remove_only,
    )


def _inline_toolbar_key(bid: str, field: str, name: str) -> str:
    return f"inlinefmt_{name}_{field}_{bid}"


def _inline_toolbar_pending_key(bid: str, field: str) -> str:
    return _inline_toolbar_key(bid, field, "pending")


def _inline_toolbar_flash_key(bid: str, field: str) -> str:
    return _inline_toolbar_key(bid, field, "flash")


def _inline_editor_dom_token(bid: str, field: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_-]+", "-", f"{field}-{bid}").strip("-")
    return token or "inline-editor"


def _inline_editor_textarea_label(bid: str, field: str, label: str) -> str:
    return f"cambridge_inline_editor::{field}::{bid}::{label}"


def _apply_pending_inline_toolbar_state(bid: str, field: str, total_lines: int) -> None:
    pending_key = _inline_toolbar_pending_key(bid, field)
    pending = st.session_state.pop(pending_key, None)
    if not pending:
        return
    action_type = pending.get("type") if isinstance(pending, dict) else str(pending)
    if action_type != "select_all":
        return
    start_key = _inline_toolbar_key(bid, field, "start")
    end_key = _inline_toolbar_key(bid, field, "end")
    st.session_state[start_key] = 1
    st.session_state[end_key] = max(1, int(total_lines or 1))


def _ensure_inline_toolbar_state(bid: str, field: str, total_lines: int) -> None:
    start_key = _inline_toolbar_key(bid, field, "start")
    end_key = _inline_toolbar_key(bid, field, "end")
    props_key = _inline_toolbar_key(bid, field, "props")
    size_key = _inline_toolbar_key(bid, field, "size")
    bold_key = _inline_toolbar_key(bid, field, "bold")
    italic_key = _inline_toolbar_key(bid, field, "italic")
    align_key = _inline_toolbar_key(bid, field, "align")

    if start_key not in st.session_state:
        st.session_state[start_key] = 1
    if end_key not in st.session_state:
        st.session_state[end_key] = min(total_lines, 1)
    st.session_state[start_key] = max(1, min(int(st.session_state.get(start_key, 1) or 1), total_lines))
    st.session_state[end_key] = max(
        int(st.session_state[start_key]),
        min(int(st.session_state.get(end_key, st.session_state[start_key]) or st.session_state[start_key]), total_lines),
    )
    if props_key not in st.session_state:
        st.session_state[props_key] = []
    if size_key not in st.session_state:
        st.session_state[size_key] = 1.0
    if bold_key not in st.session_state:
        st.session_state[bold_key] = False
    if italic_key not in st.session_state:
        st.session_state[italic_key] = False
    if align_key not in st.session_state:
        st.session_state[align_key] = "inherit"


def _line_number_gutter_html(text: str, height_px: int, gutter_id: Optional[str] = None) -> str:
    lines = _normalize_text_lines(text)
    width = max(2, len(str(len(lines))))
    nums = "\n".join(str(i).rjust(width) for i in range(1, len(lines) + 1))
    id_attr = f' id="{html.escape(str(gutter_id), quote=True)}"' if gutter_id else ""
    return (
        f'<div{id_attr} class="cambridge-inline-gutter" style="height:{int(height_px)}px; overflow:hidden; background:#f8fafc; border:1px solid rgba(49,51,63,0.18); '
        f'border-radius:0.5rem; padding:0.9rem 0.45rem; box-sizing:border-box; font-family:Consolas, Menlo, monospace; font-size:0.92rem; '
        f'line-height:1.55; text-align:right; white-space:pre; color:#667085; user-select:none;">{html.escape(nums)}</div>'
    )


def _render_inline_editor_scrollsync_bridge(textarea_label: str, gutter_id: str, editor_token: str) -> None:
    bridge_html = f"""
    <script>
    const textareaLabel = {json.dumps(textarea_label)};
    const gutterId = {json.dumps(gutter_id)};
    const editorToken = {json.dumps(editor_token)};

    function findTextarea(doc, label) {{
      const nodes = Array.from(doc.querySelectorAll('textarea'));
      return nodes.find((el) => (el.getAttribute('aria-label') || '') === label) || null;
    }}

    function bindInlineEditor() {{
      try {{
        const doc = window.parent.document;
        const gutter = doc.getElementById(gutterId);
        const textarea = findTextarea(doc, textareaLabel);
        if (!gutter || !textarea) {{
          return false;
        }}

        textarea.setAttribute('wrap', 'off');
        textarea.style.whiteSpace = 'pre';
        textarea.style.overflowWrap = 'normal';
        textarea.style.wordBreak = 'normal';
        textarea.style.fontFamily = 'Consolas, Menlo, monospace';
        textarea.style.lineHeight = '1.55';
        textarea.style.tabSize = '4';
        textarea.style.resize = 'vertical';

        const syncStyles = () => {{
          const cs = window.getComputedStyle(textarea);
          gutter.style.height = textarea.offsetHeight + 'px';
          gutter.style.paddingTop = cs.paddingTop;
          gutter.style.paddingBottom = cs.paddingBottom;
          gutter.style.fontSize = cs.fontSize;
          gutter.style.lineHeight = cs.lineHeight;
          gutter.style.overflowX = 'hidden';
          gutter.style.overflowY = 'hidden';
        }};

        const syncScroll = () => {{
          gutter.scrollTop = textarea.scrollTop;
          gutter.scrollLeft = 0;
        }};

        syncStyles();
        syncScroll();

        if (textarea.dataset.cambridgeInlineBound !== editorToken) {{
          textarea.dataset.cambridgeInlineBound = editorToken;
          textarea.addEventListener('scroll', syncScroll, {{ passive: true }});
          textarea.addEventListener('input', syncStyles, {{ passive: true }});
          textarea.addEventListener('keyup', syncStyles, {{ passive: true }});
          gutter.addEventListener('wheel', (event) => {{
            textarea.scrollTop += event.deltaY;
            textarea.scrollLeft += event.deltaX;
            syncScroll();
            event.preventDefault();
          }}, {{ passive: false }});
          if (typeof ResizeObserver !== 'undefined' && !textarea._cambridgeInlineResizeObserver) {{
            const ro = new ResizeObserver(() => {{
              syncStyles();
              syncScroll();
            }});
            ro.observe(textarea);
            textarea._cambridgeInlineResizeObserver = ro;
          }}
        }}

        return true;
      }} catch (err) {{
        return false;
      }}
    }}

    let tries = 0;
    const maxTries = 120;
    const timer = window.setInterval(() => {{
      tries += 1;
      if (bindInlineEditor() || tries >= maxTries) {{
        window.clearInterval(timer);
      }}
    }}, 120);
    window.addEventListener('load', bindInlineEditor);
    </script>
    """
    components.html(bridge_html, height=0, width=0)


def _render_inline_text_editor(
    bundle: c19.PassageBundle,
    field: str,
    label: str,
    height: int,
    help_text: str = "",
) -> Dict[str, Any]:
    bid = c19.bundle_id(bundle)
    widget_key = _editor_widget_key(field, bid)
    current_text = _ensure_camfmt_markdown_attr(str(st.session_state.get(widget_key, _get_bundle_text_data(bid).get(field, ""))))
    total_lines = max(1, len(_normalize_text_lines(current_text)))
    _apply_pending_inline_toolbar_state(bid, field, total_lines)
    _ensure_inline_toolbar_state(bid, field, total_lines)
    prefix = lambda suffix: _inline_toolbar_key(bid, field, suffix)
    active_layers = list((_get_bundle_format_layers(bid) or {}).get(field, []))
    flash_key = _inline_toolbar_flash_key(bid, field)
    flash_message = str(st.session_state.pop(flash_key, "") or "").strip()
    gutter_id = f"cambridge-inline-gutter-{_inline_editor_dom_token(bid, field)}"
    textarea_label = _inline_editor_textarea_label(bid, field, label)

    st.markdown(f"**{label}**")
    meta_bits = [f"{total_lines} dòng", f"{len(active_layers)} format range"]
    if help_text:
        meta_bits.append(help_text)
    st.caption(" • ".join(meta_bits))
    if flash_message:
        st.info(flash_message)

    tb1, tb2, tb3, tb4 = st.columns([0.65, 0.65, 1.2, 0.95])
    with tb1:
        st.number_input("Từ dòng", min_value=1, max_value=total_lines, step=1, key=prefix("start"))
    with tb2:
        st.number_input("Đến dòng", min_value=1, max_value=total_lines, step=1, key=prefix("end"))
    with tb3:
        st.multiselect(
            "Thuộc tính cần áp dụng",
            options=["size", "bold", "italic", "align"],
            format_func=lambda x: {"size": "Cỡ chữ", "bold": "In đậm", "italic": "In nghiêng", "align": "Căn lề"}[x],
            key=prefix("props"),
            help="Chỉ các thuộc tính được chọn mới thay đổi. Các format sẵn có ở dòng khác hoặc thuộc tính khác sẽ được giữ nguyên.",
        )
    with tb4:
        st.slider("Cỡ chữ", min_value=0.8, max_value=1.4, step=0.05, key=prefix("size"))

    tb5, tb6, tb7, tb8, tb9 = st.columns([0.7, 0.7, 1.05, 0.9, 0.9])
    with tb5:
        st.checkbox("In đậm", key=prefix("bold"))
    with tb6:
        st.checkbox("In nghiêng", key=prefix("italic"))
    with tb7:
        st.selectbox("Căn lề", options=["inherit", "left", "center", "right", "justify"], key=prefix("align"))
    with tb8:
        action_select_all = st.form_submit_button(f"↕️ Chọn tất cả {label}")
    with tb9:
        st.caption("Toolbar giữ logic format layer cũ: chỉnh vùng này không làm đè format còn lại.")

    act1, act2 = st.columns(2)
    with act1:
        action_apply = st.form_submit_button(f"🎨 Apply toolbar cho {label}")
    with act2:
        action_clear = st.form_submit_button(f"🧹 Clear range của {label}")

    gut1, gut2 = st.columns([0.11, 0.89], gap="small")
    with gut1:
        st.markdown(_line_number_gutter_html(current_text, height, gutter_id=gutter_id), unsafe_allow_html=True)
    with gut2:
        st.text_area(textarea_label, key=widget_key, height=height, label_visibility="collapsed")
        _render_inline_editor_scrollsync_bridge(textarea_label, gutter_id, _inline_editor_dom_token(bid, field))

    return {
        "field": field,
        "label": label,
        "total_lines": total_lines,
        "apply": action_apply,
        "clear": action_clear,
        "select_all": action_select_all,
    }



def _handle_inline_text_editor_action(bundle: c19.PassageBundle, action: Dict[str, Any]) -> Optional[str]:
    if not action:
        return None
    bid = c19.bundle_id(bundle)
    field = str(action.get("field", "question_markup"))
    label = str(action.get("label", field))
    total_lines = max(1, int(action.get("total_lines", 1) or 1))
    start_key = _inline_toolbar_key(bid, field, "start")
    end_key = _inline_toolbar_key(bid, field, "end")
    props_key = _inline_toolbar_key(bid, field, "props")
    size_key = _inline_toolbar_key(bid, field, "size")
    bold_key = _inline_toolbar_key(bid, field, "bold")
    italic_key = _inline_toolbar_key(bid, field, "italic")
    align_key = _inline_toolbar_key(bid, field, "align")
    flash_key = _inline_toolbar_flash_key(bid, field)

    if action.get("select_all"):
        st.session_state[_inline_toolbar_pending_key(bid, field)] = {"type": "select_all"}
        st.session_state[flash_key] = f"Đã chọn toàn bộ {label} ({total_lines} dòng) cho toolbar."
        return str(st.session_state.get(flash_key, ""))

    if action.get("apply"):
        selected_props = list(st.session_state.get(props_key, []) or [])
        if not selected_props:
            return f"Bạn chưa chọn thuộc tính nào cho {label}, nên toolbar chưa thay đổi format."
        _save_text_edits(bundle)
        _apply_formatting_to_field(
            bundle=bundle,
            target=field,
            start_line=int(st.session_state.get(start_key, 1)),
            end_line=int(st.session_state.get(end_key, total_lines)),
            font_scale=float(st.session_state.get(size_key, 1.0)),
            bold=bool(st.session_state.get(bold_key, False)),
            italic=bool(st.session_state.get(italic_key, False)),
            align=str(st.session_state.get(align_key, "inherit") or "inherit"),
            props=selected_props,
            remove_only=False,
        )
        return f"Đã áp dụng toolbar format cho {label}."

    if action.get("clear"):
        _save_text_edits(bundle)
        _apply_formatting_to_field(
            bundle=bundle,
            target=field,
            start_line=int(st.session_state.get(start_key, 1)),
            end_line=int(st.session_state.get(end_key, total_lines)),
            font_scale=1.0,
            bold=False,
            italic=False,
            align="inherit",
            props=[],
            remove_only=True,
        )
        return f"Đã gỡ format ở range đã chọn của {label}."

    return None

def _reset_state() -> None:
    keys = [
        "scans", "bundles", "keys_by_test", "keys_original", "prepared_pdf", "prepared_cache_dir",
        "prepared_tests", "prepared_lang", "group_overrides", "text_data_by_bundle", "format_layers_by_bundle",
        "feedback_items_by_bundle", "prepared_question_provider", "prepared_passage_provider", "prepared_skill",
        "prepared_answer_provider", "prepared_transcript_provider", "prepared_transcript_page_mode", "prepared_transcript_page_ranges", "transcript_diagnostics_by_test", "loaded_from_snapshot", "_editor_refresh_bids",
    ]
    for k in keys:
        if k in st.session_state:
            del st.session_state[k]


def _base_groups_for_bundle(bundle: c19.PassageBundle) -> List[c19.QuestionGroup]:
    bid = c19.bundle_id(bundle)
    raw_source = (st.session_state.get("text_data_by_bundle", {}).get(bid) or {}).get("question_source", "")
    return c19.merge_groups_from_raw_source(bundle, raw_source)


def _effective_groups(bundle: c19.PassageBundle) -> List[c19.QuestionGroup]:
    bid = c19.bundle_id(bundle)
    raw_source = (st.session_state.get("text_data_by_bundle", {}).get(bid) or {}).get("question_source", "")
    return c19.effective_groups_from_source(bundle, raw_source, st.session_state.get("group_overrides", {}).get(bid, {}))


def _format_choice_map_for_textarea(mapping: Optional[Dict[str, str]], ordered_letters: Optional[List[str]] = None) -> str:
    mapping = {str(k or "").strip().upper(): str(v or "").strip() for k, v in (mapping or {}).items() if str(k or "").strip()}
    if not mapping:
        return ""
    letters = [str(x or "").strip().upper() for x in (ordered_letters or []) if str(x or "").strip()]
    if not letters:
        letters = sorted(mapping.keys())
    lines: List[str] = []
    seen: set[str] = set()
    for letter in letters:
        if letter not in mapping:
            continue
        txt = mapping.get(letter, "")
        lines.append(f"{letter}. {txt}" if txt else f"{letter}.")
        seen.add(letter)
    for letter in sorted(mapping.keys()):
        if letter in seen:
            continue
        txt = mapping.get(letter, "")
        lines.append(f"{letter}. {txt}" if txt else f"{letter}.")
    return "\n".join(lines).strip()


def _parse_choice_map_from_textarea(raw: str, allowed_letters: List[str]) -> Dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = c19._extract_choice_map_from_blocks([text], allowed_letters)
    except Exception:
        parsed = {}
    if parsed:
        return {str(k or "").strip().upper(): str(v or "").strip() for k, v in parsed.items() if str(k or "").strip()}
    out: Dict[str, str] = {}
    allowed = {str(x or "").strip().upper() for x in (allowed_letters or []) if str(x or "").strip()}
    for raw_line in text.replace("\r", "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        m = re.match(r"^\(?([A-Z])\)?(?:\s*[\.):-]|\s+)\s*(.*)$", line)
        if not m:
            continue
        letter = m.group(1).upper()
        if allowed and letter not in allowed:
            continue
        out[letter] = re.sub(r"\s+", " ", m.group(2)).strip()
    return out


def _choice_label_for_letter(letter: str, mapping: Optional[Dict[str, str]]) -> str:
    txt = str((mapping or {}).get(str(letter or "").strip().upper(), "")).strip()
    return f"{letter}. {txt}" if txt else str(letter)


def _build_review_items(
    bundle: c19.PassageBundle,
    groups: List[c19.QuestionGroup],
    singles: Dict[int, List[str]],
    pairs: List[Tuple[Tuple[int, int], List[str]]],
) -> List[dict]:
    q_start, q_end = bundle.qrange
    pair_map = {(min(a, b), max(a, b)): [v.strip().upper() for v in vals if v.strip()] for (a, b), vals in pairs}
    items: List[dict] = []
    used: set[int] = set()

    def norm_single(ans_list: List[str]) -> List[str]:
        return [a.strip() for a in (ans_list or []) if a and a.strip()]

    for g in sorted(groups, key=lambda x: min(x.qnums) if x.qnums else 10**9):
        if not g.qnums or max(g.qnums) < q_start or min(g.qnums) > q_end:
            continue
        g_problem = g.group_type == "unknown"
        gk = c19.group_key(g)
        if g.group_type == "choose_two_letters" and len(g.qnums) == 2:
            a, b = sorted(g.qnums)
            options = c19.letters_for_group(g) or list("ABCDE")
            corr = pair_map.get((a, b), [])
            choice_map = c19.choice_texts_for_group(g)
            if not corr:
                la = norm_single(singles.get(a, []))
                lb = norm_single(singles.get(b, []))
                corr = []
                if la:
                    corr.append(c19._maybe_fix_letter_token(la[0]))
                if lb:
                    corr.append(c19._maybe_fix_letter_token(lb[0]))
                corr = [x for x in corr if x]
            if len(corr) != 2:
                corr = [options[0], options[1] if len(options) > 1 else options[0]]
                g_problem = True
            items.append({
                "kind": "choose_two", "label": f"{a}-{b}", "qnums": [a, b], "group_type": g.group_type,
                "options": options, "correct": corr, "problem": g_problem, "raw_block": g.raw_block,
                "group_key": gk, "choice_text_map": choice_map,
            })
            used.update([a, b])
            continue

        for q in sorted(g.qnums):
            if q < q_start or q > q_end or q in used:
                continue
            ans = norm_single(singles.get(q, []))
            ans0 = ans[0] if ans else ""
            if g.group_type == "tfng":
                options = ["TRUE", "FALSE", "NOT GIVEN"]
                corr = c19._clean_key_answer(ans0).upper() if ans0 else options[0]
                if corr == "NOTGIVEN":
                    corr = "NOT GIVEN"
                items.append({"kind": "select", "label": str(q), "qnums": [q], "group_type": g.group_type, "options": options, "correct": corr if corr in options else options[0], "problem": g_problem or corr not in options, "raw_block": g.raw_block})
            elif g.group_type == "yesno":
                options = ["YES", "NO", "NOT GIVEN"]
                corr = c19._clean_key_answer(ans0).upper() if ans0 else options[0]
                if corr == "NOTGIVEN":
                    corr = "NOT GIVEN"
                items.append({"kind": "select", "label": str(q), "qnums": [q], "group_type": g.group_type, "options": options, "correct": corr if corr in options else options[0], "problem": g_problem or corr not in options, "raw_block": g.raw_block})
            elif g.group_type in ("mc_letters", "letter_dropdown"):
                default_letters = "ABCD" if g.group_type == "mc_letters" else "ABCDEFG"
                options = c19.letters_for_group(g) or list(default_letters)
                corr = c19._maybe_fix_letter_token(ans0) if ans0 else options[0]
                choice_map = c19.choice_texts_for_question(g, q)
                items.append({"kind": "select", "label": str(q), "qnums": [q], "group_type": g.group_type, "options": options, "correct": corr if corr in options else options[0], "problem": g_problem or corr not in options, "raw_block": g.raw_block, "group_key": gk, "choice_text_map": choice_map})
            else:
                items.append({"kind": "text", "label": str(q), "qnums": [q], "group_type": g.group_type, "options": None, "correct": ans or [""], "problem": g_problem or not (ans and ans[0].strip()), "raw_block": g.raw_block, "group_key": gk})
            used.add(q)

    for q in range(q_start, q_end + 1):
        if q in used:
            continue
        ans = [a.strip() for a in (singles.get(q, []) or []) if a.strip()]
        items.append({"kind": "text", "label": str(q), "qnums": [q], "group_type": "ungrouped_text", "options": None, "correct": ans or [""], "problem": True, "raw_block": "", "group_key": ""})

    items.sort(key=lambda it: int(it["label"].split("-")[0]))
    return items


def _apply_review_edits(bundle: c19.PassageBundle, items_all: List[dict]) -> None:
    bid = c19.bundle_id(bundle)
    test_num = bundle.test_num
    singles, pairs = st.session_state["keys_by_test"][test_num]
    singles = copy.deepcopy(singles)
    pair_map = {(min(a, b), max(a, b)): [v.strip().upper() for v in vals if v.strip()] for (a, b), vals in pairs}
    manual_choice_by_q: Dict[str, Dict[str, Dict[str, str]]] = {}
    manual_choice_group: Dict[str, Dict[str, str]] = {}

    for it in items_all:
        label = it["label"]
        if it["kind"] == "select":
            singles[it["qnums"][0]] = [str(st.session_state.get(f"ans_{bid}_{label}", it["correct"]))]
            if it.get("group_type") in ("mc_letters", "letter_dropdown"):
                raw_choices = str(st.session_state.get(f"choice_text_{bid}_{label}", ""))
                choice_map = _parse_choice_map_from_textarea(raw_choices, list(it.get("options") or []))
                if choice_map and it.get("group_key"):
                    manual_choice_by_q.setdefault(str(it.get("group_key")), {})[str(it["qnums"][0])] = choice_map
        elif it["kind"] == "text":
            raw = str(st.session_state.get(f"ans_{bid}_{label}", ""))
            parts = []
            for p in raw.replace("\r", "\n").split("\n"):
                p = p.strip()
                if not p:
                    continue
                parts.extend([x.strip() for x in p.split("/") if x.strip()])
            singles[it["qnums"][0]] = parts
        else:
            v1 = str(st.session_state.get(f"ans_{bid}_{label}_1", it["correct"][0])).strip().upper()
            v2 = str(st.session_state.get(f"ans_{bid}_{label}_2", it["correct"][1])).strip().upper()
            a, b = sorted(it["qnums"])
            pair_map[(a, b)] = [v1, v2]
            raw_choices = str(st.session_state.get(f"choice_text_{bid}_{label}", ""))
            choice_map = _parse_choice_map_from_textarea(raw_choices, list(it.get("options") or []))
            if choice_map and it.get("group_key"):
                manual_choice_group[str(it.get("group_key"))] = choice_map

    st.session_state["keys_by_test"][test_num] = (singles, [((a, b), vals) for (a, b), vals in sorted(pair_map.items())])

    overrides = copy.deepcopy(st.session_state.get("group_overrides", {}))
    bundle_map = overrides.setdefault(bid, {})
    for g in _base_groups_for_bundle(bundle):
        gk = c19.group_key(g)
        if not gk:
            continue
        gtype = st.session_state.get(f"gtype_{bid}_{gk}", g.group_type)
        letters_spec = st.session_state.get(f"gletters_{bid}_{gk}", "")
        existing_meta = copy.deepcopy((bundle_map.get(gk) or {}).get("meta") or {})
        preserved_choice_by_q = existing_meta.get("choice_text_by_q_json")
        preserved_choice = existing_meta.get("choice_text_json")
        meta = copy.deepcopy(c19.default_meta_for_type(gtype))
        meta.update(c19.parse_letters_spec(letters_spec))
        if preserved_choice_by_q:
            meta["choice_text_by_q_json"] = preserved_choice_by_q
        if preserved_choice:
            meta["choice_text_json"] = preserved_choice
        if gk in manual_choice_by_q:
            meta["choice_text_by_q_json"] = json.dumps(manual_choice_by_q[gk], ensure_ascii=False)
        if gk in manual_choice_group:
            meta["choice_text_json"] = json.dumps(manual_choice_group[gk], ensure_ascii=False)
        bundle_map[gk] = {"group_type": gtype, "meta": meta}
    st.session_state["group_overrides"] = overrides

    _save_text_edits(bundle)


def _render_bundle_preview(bundle: c19.PassageBundle, preview_zoom: float, jpeg_quality: int, shuffle_choices: bool) -> None:
    pdf_path_local = st.session_state.get("prepared_pdf")
    cache_dir = st.session_state.get("prepared_cache_dir")
    if not pdf_path_local or not Path(str(pdf_path_local)).exists() or not cache_dir:
        st.info("Bundle này đang được mở từ snapshot/manifest/XML/HTML nên app không còn PDF gốc để render ảnh. UI sẽ dùng rich preview từ text/markup đã lưu, không gọi lại API.")
        _render_rich_text_preview(bundle, shuffle_choices)
        return
    pdf_path_local = Path(str(pdf_path_local))
    cache_dir = Path(str(cache_dir))
    bid = c19.bundle_id(bundle)
    singles, pairs = st.session_state["keys_by_test"][bundle.test_num]
    groups = _effective_groups(bundle)
    fields = c19._qnums_to_fields(bundle.qrange, groups, singles, pairs, prefer_radio_small=True, shuffle=bool(shuffle_choices), feedback_by_label=st.session_state.get("feedback_items_by_bundle", {}).get(bid, {}), choice_layout=str((_get_resolved_bundle_text_data(bid) or {}).get("choice_layout", "vertical") or "vertical"))
    text_data = _get_resolved_bundle_text_data(bid)
    feedback_html = c19.explanations_to_generalfeedback_html({"items": list(st.session_state.get("feedback_items_by_bundle", {}).get(bid, {}).values())}) if st.session_state.get("feedback_items_by_bundle", {}).get(bid) else ""

    doc = c19.fitz.open(str(pdf_path_local))
    try:
        c19._ensure_dir(cache_dir / "images")
        passage_inline: List[Tuple[str, bytes]] = []
        question_inline: List[Tuple[str, bytes]] = []
        def add_page_image(pno: int, prefix: str) -> Tuple[str, bytes]:
            name = f"{prefix}_t{bundle.test_num}_p{pno:03d}.jpg"
            img_path = cache_dir / "images" / name
            if img_path.exists():
                img_bytes = img_path.read_bytes()
            else:
                img = c19._render_page(doc, pno, zoom=float(preview_zoom))
                img_bytes = c19._img_to_jpeg_bytes(img, quality=int(jpeg_quality))
                img_path.write_bytes(img_bytes)
            return name, img_bytes
        for pno in bundle.passage_pages:
            passage_inline.append(add_page_image(pno, f"passage{bundle.passage_num}"))
        for pno in bundle.question_pages:
            question_inline.append(add_page_image(pno, f"q{bundle.passage_num}"))
        title = text_data.get("display_title") or (_modern_display_title(bundle, str(text_data.get("skill", "reading") or "reading")))
        html = c19.build_preview_html(
            title=title,
            passage_images=passage_inline,
            question_images=question_inline,
            fields=fields,
            passage_text=text_data.get("passage_text", ""),
            question_markup=text_data.get("question_markup", ""),
            explanation_html=feedback_html,
            pluginfile=False,
            question_visual_images=_question_visual_override_entries(bundle, preview_zoom=float(preview_zoom), jpeg_quality=int(jpeg_quality)),
            question_visual_position=str(text_data.get("question_image_position", "top") or "top"),
            question_visual_after_label=str(text_data.get("question_image_after_label", "") or ""),
            question_visual_after_keyword=str(text_data.get("question_image_after_keyword", "") or ""),
            skill=str(text_data.get("skill", st.session_state.get("prepared_skill", "reading")) or "reading"),
            review_left_markdown=str(text_data.get("audioscript_clean", "") or text_data.get("audioscript_raw", "") or text_data.get("passage_text", "")),
            audio_entries=([(Path(str(text_data.get("audio_override_path"))).name, Path(str(text_data.get("audio_override_path"))).read_bytes())] if str(text_data.get("audio_override_path", "")).strip() and Path(str(text_data.get("audio_override_path"))).exists() else []),
            audio_title=str(text_data.get("audio_title", "") or ""),
            audio_lockid=str(text_data.get("audio_lockid", f"test{bundle.test_num}_part{bundle.passage_num}") or f"test{bundle.test_num}_part{bundle.passage_num}"),
            audio_show_in_review=bool(text_data.get("audio_show_in_review", True)),
            study_keywords=text_data.get("study_keywords"),
            passage_label_style=str(text_data.get("passage_label_style", "plain") or "plain"),
        )
        components.html(html, height=860, scrolling=True)
    finally:
        doc.close()



def _render_rich_text_preview(bundle: c19.PassageBundle, shuffle_choices: bool) -> None:
    bid = c19.bundle_id(bundle)
    singles, pairs = st.session_state["keys_by_test"][bundle.test_num]
    groups = _effective_groups(bundle)
    fields = c19._qnums_to_fields(
        bundle.qrange, groups, singles, pairs, prefer_radio_small=True, shuffle=bool(shuffle_choices),
        feedback_by_label=st.session_state.get("feedback_items_by_bundle", {}).get(bid, {}),
        choice_layout=str((_get_resolved_bundle_text_data(bid) or {}).get("choice_layout", "vertical") or "vertical"),
    )
    text_data = _get_resolved_bundle_text_data(bid)
    left_source = text_data.get("passage_text", "") or "<p><em>(Chưa có passage text)</em></p>"
    if str(text_data.get("skill", st.session_state.get("prepared_skill", "reading"))) == "listening":
        left_source = text_data.get("audioscript_clean", "") or text_data.get("audioscript_raw", "") or left_source
    left_html = c19._style_markdown_tables_in_html(c19._markdown_to_html(left_source))
    visual_images = _question_visual_override_entries(bundle, preview_zoom=1.2, jpeg_quality=80)
    right_html = c19.render_question_markup_with_fields(
        text_data.get("question_markup", "") or "<p><em>(Chưa có question markup)</em></p>",
        fields,
        question_visual_images=visual_images,
        question_visual_position=str(text_data.get("question_image_position", "top") or "top"),
        question_visual_after_label=str(text_data.get("question_image_after_label", "") or ""),
        question_visual_after_keyword=str(text_data.get("question_image_after_keyword", "") or ""),
        pluginfile=False,
    )
    feedback_html = c19.explanations_to_generalfeedback_html({"items": list(st.session_state.get("feedback_items_by_bundle", {}).get(bid, {}).values())}) if st.session_state.get("feedback_items_by_bundle", {}).get(bid) else ""
    extra = f"<div class='cambridge-feedback-common'><strong>Answer explanations / Giải thích đáp án</strong>{feedback_html}</div>" if feedback_html else ""
    vocab_html = ""
    kw = text_data.get("study_keywords") or {}
    if (kw.get("items") or []):
        vocab_html = '<div class="cambridge-vocab-bar"><div class="cambridge-vocab-bar-title">Vocabulary  / Từ vựng  cần học</div>' + c19.keywords_to_bar_html(kw) + '</div>'
    html = f"""
    {c19._course_scoped_layout_css()}
    <div class="cambridge-ielts-reading-layout">
      {vocab_html}
      <table class="cambridge-split">
        <tr>
          <td class="cambridge-col cambridge-col-left"><div class="cambridge-scrollpane cambridge-leftpane">{left_html}</div></td>
          <td class="cambridge-col cambridge-col-right"><div class="cambridge-scrollpane cambridge-rightpane">{right_html}{extra}</div></td>
        </tr>
      </table>
    </div>
    """
    components.html(html, height=760, scrolling=True)


def _render_raw_markdown_panel(bundle: c19.PassageBundle) -> None:
    bid = c19.bundle_id(bundle)
    raw_data = _get_bundle_text_data(bid)
    resolved_data = _get_resolved_bundle_text_data(bid)
    tabs = [
        "Question source raw",
        "Question markup gốc",
        "Question markup hiệu lực",
        "Passage text gốc",
        "Passage text hiệu lực",
    ]
    if str(raw_data.get("skill", st.session_state.get("prepared_skill", "reading"))) == "listening":
        tabs.extend(["Audioscript raw", "Audioscript clean", "Audioscript normalized"])
    tab_objs = st.tabs(tabs)
    with tab_objs[0]:
        st.code(raw_data.get("question_source", "") or "(trống)", language="markdown")
    with tab_objs[1]:
        st.code(raw_data.get("question_markup", "") or "(trống)", language="markdown")
    with tab_objs[2]:
        st.code(resolved_data.get("question_markup", "") or "(trống)", language="markdown")
    with tab_objs[3]:
        st.code(raw_data.get("passage_text", "") or "(trống)", language="markdown")
    with tab_objs[4]:
        st.code(resolved_data.get("passage_text", "") or "(trống)", language="markdown")
    if len(tab_objs) > 5:
        with tab_objs[5]:
            st.code(raw_data.get("audioscript_raw", "") or "(trống)", language="markdown")
        with tab_objs[6]:
            st.code(raw_data.get("audioscript_clean", "") or "(trống)", language="markdown")
        with tab_objs[7]:
            st.code(raw_data.get("audioscript_normalized", "") or "(trống)", language="markdown")

def _render_compare_preview(bundle: c19.PassageBundle, shuffle_choices: bool) -> None:
    bid = c19.bundle_id(bundle)
    raw_data = _get_bundle_text_data(bid)
    resolved_data = _get_resolved_bundle_text_data(bid)
    col_a, col_b = st.columns(2)
    with col_a:
        st.caption("Raw markdown/source")
        st.code(raw_data.get("question_source", "") or "(trống)", language="markdown")
        st.caption("Question markup hiệu lực")
        st.code(resolved_data.get("question_markup", "") or "(trống)", language="markdown")
    with col_b:
        st.caption("Rendered rich text hiện tại")
        _render_rich_text_preview(bundle, shuffle_choices)

def _prepare_internal(pdf_path: Path, tests: List[int], lang: str, cache_dir: Path, skill: str, question_provider: str, passage_provider: str, answer_provider: str, transcript_provider: str, llama_api_key: str, llama_tier: str, transcript_page_mode: str = "auto", transcript_page_ranges: Optional[Dict[int, Any]] = None) -> None:
    scans = c19.scan_pdf_headers(pdf_path, cache_dir, lang=lang)
    bundles: List[c19.PassageBundle] = []
    keys_by_test: Dict[int, Tuple[Dict[int, List[str]], List[Tuple[Tuple[int, int], List[str]]]]] = {}
    transcript_maps: Dict[int, Dict[int, Dict[str, str]]] = {}
    transcript_diagnostics_by_test: Dict[int, Dict[str, Any]] = {}
    for t in tests:
        if skill == "listening":
            bundles.extend(c19l.build_listening_bundles_for_test(pdf_path, scans, t, cache_dir, lang=lang))
            manual_pages = (transcript_page_ranges or {}).get(int(t), []) if transcript_page_mode != "auto" else []
            transcript_maps[t] = c19l.extract_listening_audioscripts_for_test(pdf_path, scans, t, cache_dir, provider=transcript_provider, lang=lang, llama_api_key=llama_api_key, llama_tier=llama_tier, manual_pages=manual_pages)
            transcript_diagnostics_by_test[t] = {str(sec): {"status": payload.get("status", "missing"), "page_range": payload.get("page_range", "")} for sec, payload in (transcript_maps[t] or {}).items()}
        else:
            bundles.extend(c19.build_passages_for_test(pdf_path, scans, t, cache_dir, lang=lang))
        keys_by_test[t] = c19l.load_answer_keys_for_test(pdf_path, scans, t, cache_dir, skill=skill, provider=answer_provider, lang=lang, llama_api_key=llama_api_key, llama_tier=llama_tier)

    group_overrides: Dict[str, Dict[str, Dict[str, Any]]] = {}
    text_data_by_bundle: Dict[str, Dict[str, str]] = {}
    for b in bundles:
        bid = c19.bundle_id(b)
        group_overrides[bid] = {}
        if skill == "listening":
            text_data_by_bundle[bid] = c19l.prepare_listening_bundle_text_artifacts(
                pdf_path=pdf_path,
                bundle=b,
                cache_dir=cache_dir,
                transcript_by_section=transcript_maps.get(b.test_num, {}),
                question_provider=question_provider,
                lang=lang,
                group_overrides=group_overrides[bid],
                llama_api_key=llama_api_key,
                llama_tier=llama_tier,
            )
        else:
            text_data_by_bundle[bid] = c19.prepare_bundle_text_artifacts(
                pdf_path=pdf_path,
                bundle=b,
                cache_dir=cache_dir,
                question_provider=question_provider,
                passage_provider=passage_provider,
                lang=lang,
                group_overrides=group_overrides[bid],
                llama_api_key=llama_api_key,
                llama_tier=llama_tier,
            )
            text_data_by_bundle[bid]["skill"] = "reading"

    st.session_state["scans"] = scans
    st.session_state["bundles"] = bundles
    st.session_state["keys_by_test"] = keys_by_test
    st.session_state["keys_original"] = copy.deepcopy(keys_by_test)
    st.session_state["prepared_pdf"] = pdf_path
    st.session_state["prepared_cache_dir"] = cache_dir
    st.session_state["prepared_tests"] = list(tests)
    st.session_state["prepared_lang"] = lang
    st.session_state["group_overrides"] = group_overrides
    st.session_state["text_data_by_bundle"] = text_data_by_bundle
    st.session_state["format_layers_by_bundle"] = {bid: {"passage_text": [], "question_markup": []} for bid in text_data_by_bundle.keys()}
    st.session_state["feedback_items_by_bundle"] = {}
    st.session_state["prepared_question_provider"] = question_provider
    st.session_state["prepared_passage_provider"] = passage_provider
    st.session_state["prepared_answer_provider"] = answer_provider
    st.session_state["prepared_transcript_provider"] = transcript_provider
    st.session_state["prepared_skill"] = skill
    st.session_state["prepared_transcript_page_mode"] = transcript_page_mode
    st.session_state["prepared_transcript_page_ranges"] = transcript_page_ranges or {}
    st.session_state["transcript_diagnostics_by_test"] = transcript_diagnostics_by_test
    st.session_state["loaded_from_snapshot"] = False
    _mark_all_editor_refresh()


def _generate_feedback_for_bundle(bundle: c19.PassageBundle, gemini_api_key: str, gemini_model: str) -> None:
    bid = c19.bundle_id(bundle)
    text_data = st.session_state.get("text_data_by_bundle", {}).get(bid, {})
    question_text = text_data.get("question_markup", "") or text_data.get("question_source", "")
    skill = str(text_data.get("skill") or st.session_state.get("prepared_skill") or "reading")
    if skill == "listening":
        transcript_text = text_data.get("audioscript_normalized", "") or text_data.get("audioscript_clean", "") or text_data.get("audioscript_raw", "")
        if not str(transcript_text).strip() or not str(question_text).strip():
            raise RuntimeError("Bundle listening này chưa có đủ audioscript hoặc question text để sinh explanation.")
    else:
        transcript_text = text_data.get("passage_text", "")
        if not str(transcript_text).strip() or not str(question_text).strip():
            raise RuntimeError("Bundle này chưa có đủ passage_text hoặc question text để sinh explanation.")
    singles, pairs = st.session_state["keys_by_test"][bundle.test_num]
    answer_items = c19.build_answer_context(bundle, _effective_groups(bundle), singles, pairs)
    raw = c19.generate_explanations_gemini(transcript_text, question_text, answer_items, gemini_api_key, gemini_model)
    bundle_map = {str(it.get("label", "")): it for it in raw.get("items", []) if str(it.get("label", "")).strip()}
    all_fb = copy.deepcopy(st.session_state.get("feedback_items_by_bundle", {}))
    all_fb[bid] = bundle_map
    st.session_state["feedback_items_by_bundle"] = all_fb

def _generate_keywords_for_bundle(bundle: c19.PassageBundle, gemini_api_key: str, gemini_model: str) -> None:
    bid = c19.bundle_id(bundle)
    text_data = _get_resolved_bundle_text_data(bid)
    skill = str(text_data.get("skill") or st.session_state.get("prepared_skill") or "reading")
    if skill == "listening":
        source_text = text_data.get("audioscript_normalized", "") or text_data.get("audioscript_clean", "") or text_data.get("audioscript_raw", "")
    else:
        source_text = text_data.get("passage_text", "")
    question_text = text_data.get("question_markup", "") or text_data.get("question_source", "")
    if not str(source_text).strip() or not str(question_text).strip():
        raise RuntimeError("Bundle này chưa có đủ source text hoặc question text để sinh 5 keyword.")
    raw = c19.generate_keywords_gemini(source_text, question_text, gemini_api_key, gemini_model)
    td = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
    cur = copy.deepcopy(td.get(bid, {}))
    cur["study_keywords"] = raw
    td[bid] = cur
    st.session_state["text_data_by_bundle"] = td


with st.expander("Yêu cầu hệ thống", expanded=False):
    st.markdown(
        """
- `pip install -r requirements.txt`
- Windows cần cài Tesseract OCR nếu muốn dùng OCR local.
- Nếu dùng LlamaParse, set `LLAMA_CLOUD_API_KEY` hoặc nhập trực tiếp trong UI.
- Nếu dùng Gemini giải thích đáp án, set `GEMINI_API_KEY` hoặc nhập trực tiếp trong UI.
- v7_full_fix6 khuyên dùng preset `Llama cho toàn bộ parse` để cả Reading và Listening đều đi qua LlamaParse ở các phần text chính.
- Bản này thêm import ngược XML/HTML/manifest và manifest portable để mở lại preview không tốn quota API.
"""
    )

st.subheader("A) Chọn PDF đầu vào")

with st.expander("Hướng dẫn nhanh v7_full_fix6 (Reading + Listening, Llama-first + import portable)", expanded=True):
    st.markdown("""
**Preset khuyên dùng**
- Chọn `Parser preset = Llama cho toàn bộ parse`.
- Khi đó Reading và Listening đều sẽ ưu tiên LlamaParse cho các phần text chính:
  - Reading: `question source + passage text + answer key`
  - Listening: `question source + answer key + audioscript`

**Reading**
1. Chọn `Kỹ năng = Reading`.
2. Giữ `Parser preset = Llama cho toàn bộ parse` nếu bạn muốn chất lượng parse tốt nhất.
3. Bấm **Prepare**.
4. Vào **REVIEW** để sửa group type, answer key, question markup, passage text.
5. Bấm **Export** để ra Moodle XML/HTML.

**Listening**
1. Chọn `Kỹ năng = Listening`.
2. Giữ `Parser preset = Llama cho toàn bộ parse`.
3. Bấm **Prepare**. App sẽ tìm `Section 1-4`, question source, answer key và `Audioscripts`.
4. Trong REVIEW, bạn sẽ thấy thêm `Audioscript clean` và `Audioscript raw`. Có thể sửa tay nếu transcript chưa sạch.
5. Khi export, transcript sẽ được đưa vào **General feedback** để xem lại sau khi làm bài, không đặt ngay trong question text.

**Snapshot**
- Sau mỗi lần Prepare/Apply/Generate explanation, app tự lưu snapshot.
- Dùng `Load snapshot cũ` để mở lại mà không tốn quota API.

**Lưu ý kỹ thuật**
- v7_full_fix6 tăng độ chắc cho Listening ở phần detect `Section 1-4` và làm sạch `audioscript clean` tốt hơn trước.
- Khi export, app sẽ tạo thêm manifest portable (`.camplus.json`) và nhúng manifest vào XML/HTML để bạn mở lại preview mà không cần gọi API.
- Map/plan/diagram labeling vẫn có thể cần review tay.
""")

col_pdf1, col_pdf2 = st.columns([2, 1])
with col_pdf1:
    pdf_path_input = st.text_input("Đường dẫn PDF", value="", placeholder=r"E:\\Download\\Cambridge 19.pdf")
with col_pdf2:
    pdf_upload = st.file_uploader("Hoặc upload PDF", type=["pdf"])

pdf_path: Optional[Path] = None
tmp_dir = Path(".ui_tmp")
tmp_dir.mkdir(exist_ok=True)
if pdf_path_input.strip():
    p = Path(pdf_path_input.strip().strip('"'))
    if p.exists():
        pdf_path = p.resolve()
    else:
        st.warning("Không thấy file theo path đã nhập.")
elif pdf_upload is not None:
    saved = tmp_dir / "uploaded.pdf"
    saved.write_bytes(pdf_upload.getvalue())
    pdf_path = saved.resolve()
if pdf_path:
    st.success(f"Đã chọn PDF: {pdf_path}")

st.subheader("B) Tùy chọn xử lý")
col1, col2, col3, col4, col5 = st.columns(5)
with col1:
    skill = st.selectbox("Kỹ năng", options=["reading", "listening"], index=0, format_func=lambda x: "Reading" if x == "reading" else "Listening")
with col2:
    tests = st.multiselect("Tests cần xử lý", options=[1, 2, 3, 4], default=[1])
with col3:
    lang = st.text_input("Tesseract language", value="eng")
with col4:
    image_zoom = st.slider("Export image zoom", min_value=1.2, max_value=3.5, value=2.0, step=0.1)
with col5:
    jpeg_quality = st.slider("Export JPEG quality", min_value=30, max_value=95, value=80, step=1)

st.markdown("##### Preview")
colp1, colp2, colp3 = st.columns([1, 1, 2])
with colp1:
    enable_review = st.checkbox("Bật REVIEW", value=True)
with colp2:
    preview_zoom = st.slider("Preview zoom", min_value=1.0, max_value=2.2, value=1.3, step=0.1)
with colp3:
    show_only_issues = st.checkbox("Chỉ hiện câu có vấn đề", value=False)

st.markdown("##### Parser preset")
preset_col1, preset_col2 = st.columns([1.2, 2.8])
with preset_col1:
    parser_preset = st.selectbox(
        "Parser preset",
        options=["llama_all", "custom"],
        index=0,
        format_func=lambda x: "Llama cho toàn bộ parse" if x == "llama_all" else "Tùy chỉnh thủ công",
        help="Llama cho toàn bộ parse sẽ tự đặt Reading/Listening sang LlamaParse ở mọi phần text chính. Dùng Custom nếu bạn muốn tự hạ một parser về native/OCR.",
    )
with preset_col2:
    if parser_preset == "llama_all":
        st.info("Preset hiện tại: Reading dùng Llama cho question + passage + answer key. Listening dùng Llama cho question + answer key + audioscript.")
    else:
        st.caption("Bạn đang ở chế độ custom: có thể phối hợp native/OCR/Llama theo từng parser.")

st.markdown("##### Text parsing")
if parser_preset == "llama_all":
    q_default = "llamaparse_markdown"
    p_default = "llamaparse_markdown" if skill == "reading" else "none"
    a_default = "llamaparse_markdown"
    t_default = "llamaparse_markdown" if skill == "listening" else "none"
else:
    q_default = "llamaparse_markdown"
    p_default = "llamaparse_markdown" if skill == "reading" else "none"
    a_default = "llamaparse_markdown"
    t_default = "llamaparse_markdown" if skill == "listening" else "none"

colt1, colt2, colt3, colt4 = st.columns([1.1, 1.1, 1.1, 1.4])
with colt1:
    q_opts = ["llamaparse_markdown", "native_pdf_text", "ocr_text", "none"]
    question_provider = st.selectbox("Question parser", options=q_opts, index=q_opts.index(q_default), disabled=(parser_preset == "llama_all"))
with colt2:
    p_opts = ["llamaparse_markdown", "native_pdf_text", "ocr_text", "none"]
    passage_provider = st.selectbox("Passage parser", options=p_opts, index=p_opts.index(p_default), disabled=(parser_preset == "llama_all" and skill == "reading"))
with colt3:
    a_opts = ["llamaparse_markdown", "native_pdf_text", "ocr_text"]
    answer_provider = st.selectbox("Answer key parser", options=a_opts, index=a_opts.index(a_default), disabled=(parser_preset == "llama_all"))
with colt4:
    llama_tier = st.selectbox("LlamaParse tier", options=["fast", "cost_effective", "agentic", "agentic_plus"], index=2)

coll1, coll2, coll3 = st.columns(3)
with coll1:
    llama_api_key = st.text_input("LLAMA_CLOUD_API_KEY", value=os.environ.get("LLAMA_CLOUD_API_KEY", ""), type="password")
with coll2:
    gemini_api_key = st.text_input("GEMINI_API_KEY", value=os.environ.get("GEMINI_API_KEY", ""), type="password")
with coll3:
    tr_opts = ["llamaparse_markdown", "native_pdf_text", "ocr_text", "none"]
    transcript_provider = st.selectbox("Transcript/Audioscript parser", options=tr_opts, index=tr_opts.index(t_default), disabled=(parser_preset == "llama_all" and skill == "listening"))

if skill == "listening":
    st.markdown("##### Transcript/Audioscript page ranges")
    tpm1, tpm2 = st.columns([1.2, 2.8])
    with tpm1:
        transcript_page_mode = st.selectbox(
            "Transcript page range mode",
            options=["auto", "manual_global", "manual_per_test", "manual_per_section"],
            index=0,
            format_func=lambda x: {"auto": "Auto", "manual_global": "Manual global", "manual_per_test": "Manual theo từng test", "manual_per_section": "Manual theo từng part/section"}[x],
        )
    with tpm2:
        if transcript_page_mode == "auto":
            st.caption("App tự dò các trang Audioscripts. Dùng manual khi có section bị trống hoặc chia sai.")
        elif transcript_page_mode == "manual_global":
            st.text_input("Trang audioscripts cho tất cả tests (ví dụ: 120-135, 140)", key="transcript_pages_global")
        elif transcript_page_mode == "manual_per_test":
            for t in tests or [1, 2, 3, 4]:
                st.text_input(f"Trang audioscripts cho Test {t}", key=f"transcript_pages_t{t}")
        else:
            for t in tests or [1, 2, 3, 4]:
                st.markdown(f"**Test {t}**")
                csec1, csec2 = st.columns(2)
                with csec1:
                    st.text_input(f"Test {t} - Part/Section 1", key=f"transcript_pages_t{t}_s1")
                    st.text_input(f"Test {t} - Part/Section 2", key=f"transcript_pages_t{t}_s2")
                with csec2:
                    st.text_input(f"Test {t} - Part/Section 3", key=f"transcript_pages_t{t}_s3")
                    st.text_input(f"Test {t} - Part/Section 4", key=f"transcript_pages_t{t}_s4")
else:
    transcript_page_mode = "auto"

gemini_model = st.text_input("Gemini model", value="gemini-2.5-flash")
category = st.text_input("Moodle category", value="IELTS/Reading" if skill == "reading" else "IELTS/Listening")
shuffle_choices = st.checkbox("Shuffle choices", value=False)
enable_specific_feedback = st.checkbox("Xuất specific feedback cho từng sub-question", value=True)
transcript_export_mode = "review_only"
if skill == "listening":
    transcript_export_mode = st.selectbox(
        "Cách export audioscript/transcript",
        options=["review_only", "generalfeedback"],
        index=0,
        format_func=lambda x: "Không nhúng transcript vào General feedback (khuyên dùng)" if x == "review_only" else "Nhúng transcript vào General feedback của từng section",
        help="Khuyên dùng: để General feedback chỉ chứa explanation. Transcript sẽ được xuất ra file companion riêng để bạn dùng cho review sau bài thi."
    )

auto_explain_on_export = st.checkbox("Tự sinh explanation khi export nếu bundle chưa có", value=False)

st.markdown("#### Output")
col_ot1, col_ot2, col_ot3 = st.columns([1, 1, 2])
with col_ot1:
    export_xml = st.checkbox("Xuất Moodle XML", value=True)
with col_ot2:
    export_html = st.checkbox("Xuất HTML preview", value=True)
with col_ot3:
    output_mode = st.radio("Output mode", options=["1 file cho tất cả tests", "Mỗi test 1 file"], index=0, horizontal=True)

html_mode = st.selectbox("HTML mode", options=["standalone", "moodle"], index=0)
export_html_assets = st.checkbox("Xuất kèm html_assets khi HTML mode = moodle", value=False, disabled=(html_mode != "moodle"))
xml_name = st.text_input("Tên file XML", value=("moodle_reading_v7_full_fix6_hotfix4.xml" if skill == "reading" else "moodle_listening_v7_full_fix6_hotfix4.xml"), disabled=(output_mode != "1 file cho tất cả tests" or not export_xml))
html_name = st.text_input("Tên file HTML", value=("reading_preview_v7_full_fix6_hotfix4.html" if skill == "reading" else "listening_preview_v7_full_fix6_hotfix4.html"), disabled=(output_mode != "1 file cho tất cả tests" or not export_html))
export_dual_xml_vocab = st.checkbox("Xuất 2 file XML: practice có từ vựng + exam không có từ vựng", value=True, disabled=(not export_xml), help="Practice XML sẽ hiện thanh 5 keyword để học từ vựng. Exam XML sẽ không hiện để dùng cho thi thật.")

st.subheader("C) Output folders")
col_out1, col_out2 = st.columns([2, 1])
with col_out1:
    out_dir_input = st.text_input("Thư mục output", value=str(Path.cwd() / "out_cambridge_plus_v7_full_fix6"))
with col_out2:
    cache_dir_input = st.text_input("Cache folder", value="")

if "keys_by_test" not in st.session_state:
    _reset_state()

st.subheader("D) Quy trình")
can_export = bool((pdf_path is not None or st.session_state.get("prepared_pdf") is not None) and (tests or st.session_state.get("prepared_tests")) and (export_xml or export_html))
colb1, colb2, colb3 = st.columns([1, 1, 2])
with colb1:
    btn_prepare = st.button("🔍 Prepare", disabled=(pdf_path is None or not tests))
with colb2:
    btn_export = st.button("🚀 Export", type="primary", disabled=(not can_export))
with colb3:
    if st.button("🧹 Reset"):
        _reset_state()
        st.success("Đã reset trạng thái.")

ui_out_dir = Path(out_dir_input).expanduser().resolve()
ui_cache_dir = Path(cache_dir_input).expanduser().resolve() if cache_dir_input.strip() else (ui_out_dir / ".cache")
ui_cache_dir.mkdir(parents=True, exist_ok=True)
available_snapshots = _list_snapshots(ui_cache_dir)
st.markdown("##### Snapshot preview/review")
colsnap1, colsnap2, colsnap3 = st.columns([3, 1, 1])
with colsnap1:
    if available_snapshots:
        selected_snapshot = st.selectbox(
            "Dữ liệu preview/review đã lưu",
            options=available_snapshots,
            index=0,
            format_func=lambda p: f"{p.name} — {datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')}",
        )
    else:
        selected_snapshot = None
        st.caption("Chưa có snapshot nào trong cache hiện tại. Sau khi Prepare hoặc Apply, app sẽ tự lưu snapshot để bạn mở lại mà không tốn quota API.")
with colsnap2:
    if st.button("📂 Load snapshot cũ", disabled=(selected_snapshot is None)):
        try:
            _load_snapshot_file(Path(selected_snapshot))
            st.success(f"Đã nạp snapshot: {Path(selected_snapshot).name}")
            st.rerun()
        except Exception as e:
            st.error(f"Load snapshot lỗi: {e}")
with colsnap3:
    if st.button("💾 Save snapshot hiện tại", disabled=("bundles" not in st.session_state)):
        try:
            saved_snapshot = _autosave_snapshot()
            if saved_snapshot is None:
                st.warning("Chưa có dữ liệu prepared để lưu snapshot.")
            else:
                st.success(f"Đã lưu snapshot: {saved_snapshot.name}")
        except Exception as e:
            st.error(f"Lưu snapshot lỗi: {e}")

st.markdown("##### Import XML/HTML/manifest để mở lại preview cũ")
colimp1, colimp2 = st.columns([3, 2])
with colimp1:
    import_artifact_path = st.text_input(
        "Đường dẫn XML / HTML / .camplus.json",
        value="",
        placeholder=r"E:\Download\moodle_listening.xml hoặc E:\Download\moodle_listening.xml.camplus.json",
    )
with colimp2:
    uploaded_artifact = st.file_uploader("Hoặc upload XML / HTML / manifest", type=["xml", "html", "json"])
impcol1, impcol2 = st.columns([1, 3])
with impcol1:
    if st.button("📥 Import artifact"):
        try:
            if import_artifact_path.strip():
                payload = _load_manifest_from_local_path(Path(import_artifact_path.strip().strip('"')).expanduser().resolve())
            elif uploaded_artifact is not None:
                payload = _load_manifest_from_uploaded_bytes(uploaded_artifact.name, uploaded_artifact.getvalue())
            else:
                raise RuntimeError("Hãy nhập path hoặc upload XML/HTML/manifest trước.")
            _apply_manifest_payload(payload)
            st.success("Đã import project từ artifact mà không cần gọi API.")
            st.rerun()
        except Exception as e:
            st.error(f"Import artifact lỗi: {e}")
with impcol2:
    st.caption("Với file export từ v7_full_fix6 trở đi, XML/HTML sẽ có manifest nhúng và có thêm file `.camplus.json` đi kèm. Import bất kỳ file nào trong số đó để mở lại preview/review cũ mà không cần quota API.")

if btn_prepare:
    _apply_tesseract_cmd_if_set()
    pdf_for_export = pdf_path if pdf_path is not None else Path(str(st.session_state.get("prepared_pdf"))).expanduser().resolve()
    tests_for_export = tests or st.session_state.get("prepared_tests", [])
    out_dir = Path(out_dir_input).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(cache_dir_input).expanduser().resolve() if cache_dir_input.strip() else (out_dir / ".cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        with st.spinner("Đang prepare OCR + bundles + keys + text parsing..."):
            transcript_page_ranges = _collect_transcript_page_ranges(transcript_page_mode, tests)
            _prepare_internal(pdf_path, tests, lang, cache_dir, skill, question_provider, passage_provider, answer_provider, transcript_provider, llama_api_key, llama_tier, transcript_page_mode=transcript_page_mode, transcript_page_ranges=transcript_page_ranges)
        _autosave_snapshot()
        st.success("✅ Prepare xong. Đã lưu snapshot để bạn có thể mở lại sau mà không cần gọi API lần nữa.")
    except Exception as e:
        st.error(f"Prepare lỗi: {e}")

prepared_pdf_state = st.session_state.get("prepared_pdf")
prepared = ("bundles" in st.session_state and "keys_by_test" in st.session_state and prepared_pdf_state is not None and Path(str(prepared_pdf_state)).exists())

if enable_review and prepared:
    st.subheader("E) REVIEW")
    bundles: List[c19.PassageBundle] = st.session_state["bundles"]
    keys_by_test = st.session_state["keys_by_test"]
    total_items = 0
    total_issues = 0
    issues_by_test: Dict[int, int] = {}
    for t in st.session_state.get("prepared_tests", []):
        test_bundles = [b for b in bundles if b.test_num == t]
        singles, pairs = keys_by_test[t]
        for b in test_bundles:
            items = _build_review_items(b, _effective_groups(b), singles, pairs)
            total_items += len(items)
            n_issues = sum(1 for it in items if it["problem"])
            total_issues += n_issues
            issues_by_test[t] = issues_by_test.get(t, 0) + n_issues

    csum1, csum2, csum3 = st.columns([1, 1, 2])
    with csum1:
        st.metric("Tổng câu/ô đáp án", total_items)
    with csum2:
        st.metric("Câu cần kiểm tra", total_issues)
    with csum3:
        st.write("Issues theo test:", issues_by_test)

    t_sel = st.selectbox("Chọn test để review", options=sorted(keys_by_test.keys()), index=0)
    test_bundles = sorted([b for b in bundles if b.test_num == t_sel], key=lambda x: x.passage_num)
    b_sel = st.selectbox("Chọn passage", options=list(range(len(test_bundles))), format_func=lambda i: (f"Passage {test_bundles[i].passage_num} (Q{test_bundles[i].qrange[0]}-{test_bundles[i].qrange[1]})" if st.session_state.get("prepared_skill", "reading") == "reading" else f"Section {test_bundles[i].passage_num} (Q{test_bundles[i].qrange[0]}-{test_bundles[i].qrange[1]})"), index=0)
    bundle = test_bundles[b_sel]
    bid = c19.bundle_id(bundle)
    singles, pairs = keys_by_test[t_sel]
    items_all = _build_review_items(bundle, _effective_groups(bundle), singles, pairs)
    items = [it for it in items_all if (not show_only_issues or it["problem"])]
    _sync_editor_widget_state(bundle)
    text_data = _get_bundle_text_data(bid)

    if st.session_state.get("prepared_skill") == "listening":
        diag = (st.session_state.get("transcript_diagnostics_by_test", {}) or {}).get(t_sel) or (st.session_state.get("transcript_diagnostics_by_test", {}) or {}).get(str(t_sel), {}) or {}
        if diag:
            st.markdown("##### Transcript diagnostics")
            cols = st.columns(4)
            for idx, sec in enumerate((1, 2, 3, 4)):
                payload = diag.get(str(sec)) or diag.get(sec) or {}
                cols[idx].metric(f"Section {sec}", payload.get("status", "missing"), payload.get("page_range", "") or None)

    st.markdown("##### Preview")
    with st.expander("Mở preview", expanded=True):
        preview_tab1, preview_tab2, preview_tab3, preview_tab4 = st.tabs(["Split preview", "Raw markdown", "Rendered rich text", "Compare raw ↔ rendered"])
        with preview_tab1:
            _render_bundle_preview(bundle, preview_zoom, jpeg_quality, shuffle_choices)
        with preview_tab2:
            _render_raw_markdown_panel(bundle)
        with preview_tab3:
            _render_rich_text_preview(bundle, shuffle_choices)
        with preview_tab4:
            _render_compare_preview(bundle, shuffle_choices)

    colx1, colx2, colx3, colx4 = st.columns([1, 1, 1, 2])
    with colx1:
        if st.button("↩️ Reset đáp án Test này về OCR"):
            st.session_state["keys_by_test"][t_sel] = copy.deepcopy(st.session_state["keys_original"][t_sel])
            _autosave_snapshot()
            st.success("Đã reset answer key của test này.")
            st.rerun()
    with colx2:
        if st.button("♻️ Regenerate text cho passage này"):
            try:
                if st.session_state.get("prepared_skill") == "listening":
                    manual_pages = _manual_transcript_pages_for_test(bundle.test_num)
                    transcript_map = c19l.extract_listening_audioscripts_for_test(
                        st.session_state["prepared_pdf"], st.session_state["scans"], bundle.test_num, st.session_state["prepared_cache_dir"],
                        provider=transcript_provider, lang=lang, llama_api_key=llama_api_key, llama_tier=llama_tier, manual_pages=manual_pages,
                    )
                    data = c19l.prepare_listening_bundle_text_artifacts(
                        pdf_path=st.session_state["prepared_pdf"], bundle=bundle, cache_dir=st.session_state["prepared_cache_dir"],
                        transcript_by_section=transcript_map, question_provider=question_provider, lang=lang,
                        group_overrides=st.session_state.get("group_overrides", {}).get(bid, {}),
                        llama_api_key=llama_api_key, llama_tier=llama_tier,
                    )
                else:
                    data = c19.prepare_bundle_text_artifacts(
                        pdf_path=st.session_state["prepared_pdf"], bundle=bundle, cache_dir=st.session_state["prepared_cache_dir"],
                        question_provider=question_provider, passage_provider=passage_provider, lang=lang,
                        group_overrides=st.session_state.get("group_overrides", {}).get(bid, {}),
                        llama_api_key=llama_api_key, llama_tier=llama_tier,
                    )
                td = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
                td[bid] = data
                st.session_state["text_data_by_bundle"] = td
                _mark_editor_refresh(bid)
                _autosave_snapshot()
                st.success("Đã regenerate passage/question text cho bundle này.")
                st.rerun()
            except Exception as e:
                st.error(f"Regenerate text lỗi: {e}")
    with colx3:
        if st.button("🧱 Rebuild markup từ raw source"):
            try:
                raw_source = (text_data or {}).get("question_source", "")
                if not str(raw_source).strip():
                    raise RuntimeError("Bundle này chưa có question_source raw để rebuild.")
                rebuilt = c19.build_auto_question_markdown(bundle, _effective_groups(bundle), raw_source)
                td = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
                cur = copy.deepcopy(td.get(bid, {}))
                cur["question_markup"] = rebuilt
                td[bid] = cur
                st.session_state["text_data_by_bundle"] = td
                _mark_editor_refresh(bid)
                _autosave_snapshot()
                st.success("Đã rebuild question markup từ raw source mà không cần Prepare lại.")
                st.rerun()
            except Exception as e:
                st.error(f"Rebuild markup lỗi: {e}")
    with colx4:
        st.info("Question markup dùng token `[[1]]`, `[[2]]` hoặc `[[21-22]]`. Nếu bạn vừa đổi group type thì hãy Apply rồi bấm Rebuild markup từ raw source.")

    with st.form(f"review_form_{bid}"):
        st.markdown("###### 1) Group type overrides")
        for g in _base_groups_for_bundle(bundle):
            gk = c19.group_key(g)
            saved = st.session_state.get("group_overrides", {}).get(bid, {}).get(gk, {})
            cur_type = saved.get("group_type", g.group_type)
            meta = saved.get("meta", g.meta or {})
            cur_letters = meta.get("letters") or meta.get("letters_list") or ""
            c1, c2 = st.columns([1, 1])
            with c1:
                st.selectbox(f"Group {gk} - loại câu hỏi", options=c19.QUESTION_TYPE_OPTIONS, index=c19.QUESTION_TYPE_OPTIONS.index(cur_type if cur_type in c19.QUESTION_TYPE_OPTIONS else "unknown"), key=f"gtype_{bid}_{gk}")
            with c2:
                st.text_input(f"Group {gk} - letters/options (vd A-D hoặc A,B,C,D)", value=cur_letters, key=f"gletters_{bid}_{gk}")
            if g.raw_block:
                with st.expander(f"Instruction OCR group {gk}", expanded=False):
                    st.code(g.raw_block)

        st.markdown("###### 2) Answer key review")
        for it in items:
            flag = "⚠️" if it["problem"] else "✅"
            st.markdown(f"**{flag} Q {it['label']}** — `{it['group_type']}`")
            if it["kind"] == "select":
                opts = it["options"]
                current = it["correct"] if it["correct"] in opts else opts[0]
                choice_map = it.get("choice_text_map") or {}
                st.selectbox(
                    "Đáp án đúng",
                    options=opts,
                    index=opts.index(current),
                    key=f"ans_{bid}_{it['label']}",
                    format_func=(lambda x, mp=choice_map: _choice_label_for_letter(x, mp)) if choice_map else (lambda x: x),
                )
                if it.get("group_type") in ("mc_letters", "letter_dropdown"):
                    textarea_default = _format_choice_map_for_textarea(choice_map, opts)
                    st.text_area(
                        "Nhập bù option text (mỗi dòng: A. nội dung)",
                        value=textarea_default,
                        key=f"choice_text_{bid}_{it['label']}",
                        height=max(110, 28 * (len(opts) + 1)),
                        help="Nếu app parse thiếu phần A/B/C..., bạn có thể dán hoặc gõ tay ở đây. Sau khi Apply, app sẽ tự dùng text này để render radio có đầy đủ nội dung đáp án.",
                    )
            elif it["kind"] == "text":
                st.text_input("Đáp án đúng (phân cách bằng / hoặc xuống dòng)", value=" / ".join(it["correct"] or []), key=f"ans_{bid}_{it['label']}")
            else:
                opts = it["options"]
                cur = it["correct"] if len(it["correct"]) == 2 else [opts[0], opts[1] if len(opts) > 1 else opts[0]]
                choice_map = it.get("choice_text_map") or {}
                c1, c2 = st.columns(2)
                with c1:
                    st.selectbox(
                        "Letter 1",
                        options=opts,
                        index=opts.index(cur[0] if cur[0] in opts else opts[0]),
                        key=f"ans_{bid}_{it['label']}_1",
                        format_func=(lambda x, mp=choice_map: _choice_label_for_letter(x, mp)) if choice_map else (lambda x: x),
                    )
                with c2:
                    st.selectbox(
                        "Letter 2",
                        options=opts,
                        index=opts.index(cur[1] if cur[1] in opts else (opts[1] if len(opts) > 1 else opts[0])),
                        key=f"ans_{bid}_{it['label']}_2",
                        format_func=(lambda x, mp=choice_map: _choice_label_for_letter(x, mp)) if choice_map else (lambda x: x),
                    )
                if choice_map or it.get("group_type") == "choose_two_letters":
                    textarea_default = _format_choice_map_for_textarea(choice_map, opts)
                    st.text_area(
                        "Option text cho checkbox (mỗi dòng: A. nội dung)",
                        value=textarea_default,
                        key=f"choice_text_{bid}_{it['label']}",
                        height=max(120, 28 * (len(opts) + 1)),
                        help="Tuỳ chọn này cho phép sửa tay text của các đáp án checkbox nếu source parse chưa đủ.",
                    )
            st.markdown("---")

        st.markdown("###### 3) Passage / question text")
        st.selectbox("Hiển thị lựa chọn MC / checkbox", options=["vertical", "horizontal"], index=["vertical", "horizontal"].index(str(text_data.get("choice_layout", "vertical")) if str(text_data.get("choice_layout", "vertical")) in ["vertical", "horizontal"] else "vertical"), format_func=lambda x: "Hàng dọc" if x == "vertical" else "Hàng ngang", key=f"choice_layout_{bid}", help="Áp dụng cho câu TRUE/FALSE, YES/NO, MC letters và Choose two letters dạng checkbox. Letter dropdown vẫn giữ dạng dropdown.")
        st.selectbox("Kiểu hiển thị nhãn đoạn A/B/C... ở passage", options=["plain", "badge"], index=["plain", "badge"].index(str(text_data.get("passage_label_style", "plain")) if str(text_data.get("passage_label_style", "plain")) in ["plain", "badge"] else "plain"), format_func=lambda x: "Chỉ chữ cái" if x == "plain" else "Có nền mờ", key=f"passage_label_style_{bid}", help="Plain: chỉ hiện chữ cái in đậm đứng riêng. Badge: hiện chữ cái với nền mờ bo tròn như bản trước.")
        passage_editor_action = _render_inline_text_editor(
            bundle=bundle,
            field="passage_text",
            label="Passage / Left panel text",
            height=220,
            help_text="Có cột số dòng bên trái để bạn chọn range format nhanh hơn.",
        )
        question_editor_action = _render_inline_text_editor(
            bundle=bundle,
            field="question_markup",
            label="Question markup",
            height=340,
            help_text="Dùng [[qnum]] để chèn ô trả lời inline và dùng toolbar ở trên để format theo dòng.",
        )

        with st.expander("Ảnh minh hoạ câu hỏi (map/plan/diagram)", expanded=False):
            st.caption("Bạn có thể chèn ảnh ở đầu/cuối khối, sau đúng câu số/nhãn, hoặc ngay sau một dòng chứa từ khóa. Ngoài upload ảnh crop sẵn, bản này cho phép crop trực tiếp ngay trong app từ trang PDF hoặc từ ảnh override hiện có.")
            st.checkbox("Ẩn toàn bộ ảnh minh hoạ của bundle này", value=bool(text_data.get("question_image_disabled", False)), key=f"question_image_disabled_{bid}", help="Bật mục này nếu app tự nhận diện nhầm ảnh hoặc bạn muốn xoá hẳn ảnh của bundle này khi export.")
            st.text_input("Trang ảnh override (1-based, ví dụ: 52 hoặc 52-53)", value=text_data.get("question_image_page_override", ""), key=f"question_image_page_override_{bid}")
            st.selectbox(
                "Vị trí ảnh hiển thị",
                options=["top", "bottom", "after_label", "after_keyword"],
                index=["top", "bottom", "after_label", "after_keyword"].index(str(text_data.get("question_image_position", "top")) if str(text_data.get("question_image_position", "top")) in ["top", "bottom", "after_label", "after_keyword"] else "top"),
                format_func=lambda x: {"top": "Đầu khối câu hỏi", "bottom": "Cuối khối câu hỏi", "after_label": "Sau đúng câu số / nhãn", "after_keyword": "Sau một dòng chứa từ khóa"}[x],
                key=f"question_image_position_{bid}",
            )
            st.text_input("Nếu chọn 'Sau đúng câu số / nhãn', nhập nhãn câu (ví dụ: 16, 16-20)", value=text_data.get("question_image_after_label", ""), key=f"question_image_after_label_{bid}")
            st.text_input("Nếu chọn 'Sau một dòng chứa từ khóa', nhập từ khóa hoặc một phần dòng cần dò", value=text_data.get("question_image_after_keyword", ""), key=f"question_image_after_keyword_{bid}")
            upload_img = st.file_uploader("Upload ảnh override (.png/.jpg/.webp)", type=["png", "jpg", "jpeg", "webp"], key=f"question_image_override_upload_{bid}")
            cimg1, cimg2 = st.columns(2)
            with cimg1:
                if st.form_submit_button("Lưu ảnh override", disabled=(upload_img is None)):
                    _save_text_edits(bundle)
                    saved = _save_uploaded_bundle_image(bid, upload_img)
                    if saved:
                        st.success(f"Đã lưu ảnh override: {saved.name}")
                        _autosave_snapshot()
                        st.rerun()
            with cimg2:
                if st.form_submit_button("Xoá ảnh override"):
                    _save_text_edits(bundle)
                    _clear_uploaded_bundle_image(bid)
                    st.success("Đã xoá ảnh override.")
                    _autosave_snapshot()
                    st.rerun()
            if st.form_submit_button("Xoá mọi ảnh của bundle này (kể cả ảnh app tự nhận diện)"):
                _save_text_edits(bundle)
                td = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
                cur = copy.deepcopy(td.get(bid, {}))
                cur["question_image_disabled"] = True
                cur["question_image_page_override"] = ""
                cur["question_image_after_label"] = ""
                cur["question_image_after_keyword"] = ""
                cur.pop("question_image_override_path", None)
                cur.pop("question_image_override_name", None)
                td[bid] = cur
                st.session_state["text_data_by_bundle"] = td
                st.session_state[f"question_image_disabled_{bid}"] = True
                st.session_state[f"question_image_page_override_{bid}"] = ""
                st.session_state[f"question_image_after_label_{bid}"] = ""
                st.session_state[f"question_image_after_keyword_{bid}"] = ""
                _clear_uploaded_bundle_image(bid)
                st.success("Đã xoá/tắt toàn bộ ảnh minh hoạ của bundle này.")
                _autosave_snapshot()
                st.rerun()
            current_pages_spec = str(st.session_state.get(f"question_image_page_override_{bid}", text_data.get("question_image_page_override", ""))).strip()
            source_entry = _resolve_question_visual_source(bundle, pages_spec=current_pages_spec, uploaded_file=upload_img, prefer_existing_override=True, preview_zoom=float(preview_zoom), jpeg_quality=int(jpeg_quality))
            cur_td = _get_bundle_text_data(bid)
            if str(cur_td.get("question_image_override_path", "")).strip():
                st.caption(f"Ảnh override hiện tại: {Path(str(cur_td.get('question_image_override_path'))).name}")
            if bool(cur_td.get("question_image_disabled", False)):
                st.info("Ảnh minh hoạ của bundle này hiện đang bị tắt.")
            elif source_entry:
                src_name, src_bytes = source_entry
                try:
                    src_img = Image.open(io.BytesIO(src_bytes))
                    st.caption(f"Nguồn crop hiện tại: {src_name} — kích thước {src_img.width}×{src_img.height}")
                    st.image(src_img, caption="Preview nguồn ảnh để crop", use_container_width=True)
                    cc1, cc2, cc3, cc4 = st.columns(4)
                    with cc1:
                        crop_x = st.number_input("Crop X", min_value=0, max_value=max(0, src_img.width - 1), value=0, step=1, key=f"crop_x_{bid}")
                    with cc2:
                        crop_y = st.number_input("Crop Y", min_value=0, max_value=max(0, src_img.height - 1), value=0, step=1, key=f"crop_y_{bid}")
                    with cc3:
                        crop_w = st.number_input("Crop width", min_value=1, max_value=max(1, src_img.width), value=max(1, src_img.width), step=1, key=f"crop_w_{bid}")
                    with cc4:
                        crop_h = st.number_input("Crop height", min_value=1, max_value=max(1, src_img.height), value=max(1, src_img.height), step=1, key=f"crop_h_{bid}")
                    px = int(crop_x); py = int(crop_y); pw = int(crop_w); ph = int(crop_h)
                    pw = max(1, min(pw, max(1, src_img.width - px)))
                    ph = max(1, min(ph, max(1, src_img.height - py)))
                    crop_preview = src_img.crop((px, py, px + pw, py + ph))
                    st.image(crop_preview, caption="Preview vùng crop sẽ lưu làm ảnh override", use_container_width=True)
                    if st.form_submit_button("Crop và lưu ảnh override"):
                        _save_text_edits(bundle)
                        saved = _save_cropped_bundle_image(bid, src_name, src_bytes, (px, py, pw, ph))
                        if saved:
                            st.success(f"Đã crop và lưu ảnh override: {saved.name}")
                            _autosave_snapshot()
                            st.rerun()
                except Exception as img_err:
                    st.warning(f"Không preview/crop được nguồn ảnh hiện tại: {img_err}")
            else:
                st.info("Chưa có nguồn ảnh để crop. Hãy nhập trang ảnh override hoặc upload ảnh trước.")
        if str(text_data.get("skill", st.session_state.get("prepared_skill", "reading"))) == "listening":
            with st.expander("Audio MP3 cho part/section (Listening)", expanded=False):
                st.caption('Upload trực tiếp file MP3/WAV/M4A/OGG cho section này. Khi export Moodle, app sẽ sinh thẻ <audio preload="none" controls data-lockid="..."> để dùng tốt với additional HTML listen-once của bạn.')
                default_lockid = str(text_data.get("audio_lockid", f"test{bundle.test_num}_part{bundle.passage_num}"))
                st.text_input("data-lockid", value=default_lockid, key=f"audio_lockid_{bid}")
                st.text_input("Tiêu đề audio (tuỳ chọn)", value=str(text_data.get("audio_title", _modern_audio_title(bundle, "listening"))), key=f"audio_title_{bid}")
                st.checkbox("Hiện audio cả ở trang review Moodle", value=bool(text_data.get("audio_show_in_review", True)), key=f"audio_show_in_review_{bid}", help="Attempt luôn hiện audio. Nếu bật mục này, review cũng sẽ hiện audio để nghe lại; additional HTML của bạn chỉ khóa ở trang attempt nên review vẫn nghe lại được.")
                upload_audio = st.file_uploader("Upload audio (.mp3/.wav/.m4a/.ogg)", type=["mp3", "wav", "m4a", "ogg"], key=f"audio_override_upload_{bid}")
                ac1, ac2 = st.columns(2)
                with ac1:
                    if st.form_submit_button("Lưu audio", disabled=(upload_audio is None)):
                        _save_text_edits(bundle)
                        saved = _save_uploaded_bundle_audio(bid, upload_audio)
                        if saved:
                            st.success(f"Đã lưu audio: {saved.name}")
                            _autosave_snapshot()
                            st.rerun()
                with ac2:
                    if st.form_submit_button("Xoá audio"):
                        _save_text_edits(bundle)
                        _clear_uploaded_bundle_audio(bid)
                        st.success("Đã xoá audio của section này.")
                        _autosave_snapshot()
                        st.rerun()
                audio_preview = _bundle_audio_preview_bytes(bid)
                if audio_preview:
                    audio_bytes, audio_ext = audio_preview
                    fmt = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg"}.get(audio_ext, "audio/mpeg")
                    st.audio(audio_bytes, format=fmt)
                else:
                    st.info("Chưa có audio được upload cho section này.")
            st.text_area("Audioscript clean (Listening)", value=text_data.get("audioscript_clean", ""), key=f"audioscript_clean_{bid}", height=240)
            with st.expander("Audioscript raw", expanded=False):
                st.code(text_data.get("audioscript_raw", "") or "(trống)", language="markdown")
        with st.expander("Question source raw (để bạn đối chiếu khi chỉnh)", expanded=False):
            st.code(text_data.get("question_source", "") or "(trống)")

        st.markdown("###### 4) Định dạng nhanh nâng cao / backup (v8)")
        st.caption("Toolbar mới đã được gắn ngay phía trên hai vùng văn bản. Mục này được giữ lại để bạn vẫn có thể chọn theo block hoặc theo toàn bộ văn bản như workflow cũ.")
        fmt_target = st.selectbox(
            "Áp dụng cho",
            options=["question_markup", "passage_text"],
            index=0,
            format_func=lambda x: "Question markup" if x == "question_markup" else "Passage text",
            key=f"fmt_target_{bid}",
        )
        fmt_text_for_count = _get_editor_text(bid, fmt_target)
        total_lines_fmt = max(1, len((fmt_text_for_count or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")))
        blocks_fmt = _split_text_blocks(fmt_text_for_count)
        fmt_mode = st.radio("Chế độ chọn vùng", options=["Theo block", "Theo dòng", "Toàn bộ"], horizontal=True, key=f"fmt_mode_{bid}")
        if fmt_mode == "Theo block":
            st.selectbox(
                "Chọn block",
                options=list(range(len(blocks_fmt))),
                format_func=lambda i: _block_label(blocks_fmt[i]),
                key=f"fmt_block_idx_{bid}",
            )
            selected_block = blocks_fmt[max(0, min(int(st.session_state.get(f"fmt_block_idx_{bid}", 0)), len(blocks_fmt) - 1))]
            st.caption(f"Block đang chọn: dòng {selected_block[0]}-{selected_block[1]}")
            st.code(selected_block[2] or "(trống)", language="markdown")
        elif fmt_mode == "Theo dòng":
            fmtc1, fmtc2 = st.columns(2)
            with fmtc1:
                st.number_input("Từ dòng", min_value=1, max_value=total_lines_fmt, value=1, step=1, key=f"fmt_start_{bid}")
            with fmtc2:
                st.number_input("Đến dòng", min_value=1, max_value=total_lines_fmt, value=min(total_lines_fmt, 3), step=1, key=f"fmt_end_{bid}")

        st.multiselect(
            "Chỉ áp dụng các thuộc tính được chọn (để không đè format cũ)",
            options=["size", "bold", "italic", "align"],
            default=[],
            format_func=lambda x: {"size": "Cỡ chữ", "bold": "In đậm", "italic": "In nghiêng", "align": "Căn lề"}[x],
            key=f"fmt_props_{bid}",
        )
        fmtc4, fmtc5, fmtc6 = st.columns([0.9, 0.8, 1.0])
        with fmtc4:
            st.slider("Cỡ chữ", min_value=0.8, max_value=1.4, value=1.0, step=0.05, key=f"fmt_size_{bid}")
        with fmtc5:
            st.checkbox("In đậm", value=False, key=f"fmt_bold_{bid}")
            st.checkbox("In nghiêng", value=False, key=f"fmt_italic_{bid}")
        with fmtc6:
            st.selectbox("Căn lề", options=["inherit", "left", "center", "right", "justify"], index=0, key=f"fmt_align_{bid}")
        active_layers = list((_get_bundle_format_layers(bid) or {}).get(fmt_target, []))
        if active_layers:
            with st.expander(f"Các format layer đang áp dụng cho {fmt_target} ({len(active_layers)})", expanded=False):
                for idx, layer in enumerate(active_layers, start=1):
                    st.write(
                        f"{idx}. dòng {layer.get('start_line')}-{layer.get('end_line')} | size={'set='+str(layer.get('font_scale', 1.0)) if layer.get('apply_font_scale', False) else 'keep'} | "
                        f"bold={'set='+('Y' if layer.get('bold') else 'N') if layer.get('apply_bold', False) else 'keep'} | italic={'set='+('Y' if layer.get('italic') else 'N') if layer.get('apply_italic', False) else 'keep'} | align={'set='+str(layer.get('align', 'inherit')) if layer.get('apply_align', False) else 'keep'}"
                    )
        st.caption("Format layer nâng cấp: mỗi lần chỉ áp dụng các thuộc tính bạn chọn, các format cũ ở vùng khác hoặc thuộc tính khác sẽ được giữ lại. Dùng Remove formatting để gỡ toàn bộ format ở vùng đã chọn.")

        csub1, csub2, csub3, csub4 = st.columns(4)
        with csub1:
            submitted = st.form_submit_button("✅ Apply changes")
        with csub2:
            submitted_rebuild = st.form_submit_button("🧱 Apply + auto rebuild markup")
        with csub3:
            submitted_format = st.form_submit_button("🎨 Apply formatting")
        with csub4:
            submitted_clear_format = st.form_submit_button("🧹 Remove formatting")
        inline_editor_message = _handle_inline_text_editor_action(bundle, passage_editor_action) or _handle_inline_text_editor_action(bundle, question_editor_action)
        if submitted or submitted_rebuild or submitted_format or submitted_clear_format or inline_editor_message:
            if inline_editor_message:
                st.success(inline_editor_message)
            if submitted or submitted_rebuild:
                _apply_review_edits(bundle, items_all)
                if submitted_rebuild:
                    td = copy.deepcopy(st.session_state.get("text_data_by_bundle", {}))
                    cur = copy.deepcopy(td.get(bid, {}))
                    raw_source = cur.get("question_source", "")
                    rebuilt = c19.build_auto_question_markdown(bundle, _effective_groups(bundle), raw_source) if str(raw_source).strip() else ""
                    cur["question_markup"] = rebuilt
                    td[bid] = cur
                    st.session_state["text_data_by_bundle"] = td
                    _clear_bundle_format_layers(bid, "question_markup")
                    _mark_editor_refresh(bid)
                    _autosave_snapshot()
                    st.success("Đã áp dụng chỉnh sửa và auto rebuild question markup từ raw source.")
                else:
                    _autosave_snapshot()
                    st.success("Đã áp dụng chỉnh sửa cho answer key, group overrides và question/passage text.")
            elif submitted_format:
                _save_text_edits(bundle)
                formatted_target = _apply_text_formatting(bundle, remove_only=False)
                st.success(f"Đã áp dụng định dạng nhanh cho {formatted_target}.")
            elif submitted_clear_format:
                _save_text_edits(bundle)
                formatted_target = _apply_text_formatting(bundle, remove_only=True)
                st.success(f"Đã gỡ định dạng nhanh ở vùng đã chọn của {formatted_target}.")
            st.rerun()

    st.markdown("##### Explanation")
    cfb1, cfb2 = st.columns([1, 3])
    with cfb1:
        if st.button("✨ Sinh explanation cho passage này"):
            try:
                _generate_feedback_for_bundle(bundle, gemini_api_key, gemini_model)
                _autosave_snapshot()
                st.success("Đã sinh explanation cho passage hiện tại.")
                st.rerun()
            except Exception as e:
                st.error(f"Sinh explanation lỗi: {e}")
    with cfb2:
        if st.button("✨ Sinh explanation cho tất cả bundles đã prepare"):
            try:
                with st.spinner("Đang gọi Gemini cho tất cả bundles..."):
                    for b in bundles:
                        _generate_feedback_for_bundle(b, gemini_api_key, gemini_model)
                _autosave_snapshot()
                st.success("Đã sinh explanation cho tất cả bundles.")
                st.rerun()
            except Exception as e:
                st.error(f"Sinh explanation hàng loạt lỗi: {e}")

    st.markdown("##### Vocabulary  / Từ vựng  cần học")
    kw_items = (((_get_bundle_text_data(bid).get("study_keywords") or {}).get("items")) or [])
    kwc1, kwc2 = st.columns([1, 3])
    with kwc1:
        if st.button("🧠 Sinh 5 từ vựng  cho passage/section này", key=f"gen_kw_one_{bid}"):
            try:
                _generate_keywords_for_bundle(bundle, gemini_api_key, gemini_model)
                _autosave_snapshot()
                st.success("Đã sinh 5 từ vựng  cho bundle hiện tại.")
                st.rerun()
            except Exception as e:
                st.error(f"Sinh từ vựng  lỗi: {e}")
    with kwc2:
        if st.button("🧠 Sinh 5 từ vựng  cho tất cả bundles đã prepare", key=f"gen_kw_all_{bid}"):
            try:
                with st.spinner("Đang gọi Gemini để sinh keyword cho tất cả bundles..."):
                    for b in bundles:
                        _generate_keywords_for_bundle(b, gemini_api_key, gemini_model)
                _autosave_snapshot()
                st.success("Đã sinh từ vựng  cho tất cả bundles.")
                st.rerun()
            except Exception as e:
                st.error(f"Sinh từ vựng  hàng loạt lỗi: {e}")
    if kw_items:
        st.markdown('<div class="cambridge-vocab-bar"><div class="cambridge-vocab-bar-title">Vocabulary  / Từ vựng  cần học</div>' + c19.keywords_to_bar_html({"items": kw_items}) + '</div>', unsafe_allow_html=True)
    else:
        st.caption("Chưa có bộ 5 từ vựng  cho bundle này.")

if btn_export:
    _apply_tesseract_cmd_if_set()
    pdf_for_export = pdf_path if pdf_path is not None else Path(str(st.session_state.get("prepared_pdf"))).expanduser().resolve()
    tests_for_export = tests or st.session_state.get("prepared_tests", [])
    out_dir = Path(out_dir_input).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(cache_dir_input).expanduser().resolve() if cache_dir_input.strip() else (out_dir / ".cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not prepared:
        st.info("Chưa Prepare. UI sẽ auto-prepare trước khi export...")
        try:
            with st.spinner("Prepare nhanh..."):
                _prepare_internal(pdf_for_export, tests_for_export, lang, cache_dir, skill, question_provider, passage_provider, answer_provider, transcript_provider, llama_api_key, llama_tier, transcript_page_mode=transcript_page_mode, transcript_page_ranges=_collect_transcript_page_ranges(transcript_page_mode, list(tests_for_export)))
        except Exception as e:
            st.error(f"Auto-prepare lỗi: {e}")
            st.stop()

    bundles = st.session_state["bundles"]
    scans = st.session_state["scans"]
    keys_by_test = st.session_state["keys_by_test"]
    group_overrides = st.session_state.get("group_overrides", {})
    text_data_by_bundle = _get_effective_text_data_by_bundle()
    feedback_items_by_bundle = copy.deepcopy(st.session_state.get("feedback_items_by_bundle", {})) if enable_specific_feedback else {}

    if auto_explain_on_export and gemini_api_key:
        try:
            with st.spinner("Đang sinh explanation còn thiếu trước khi export..."):
                for b in bundles:
                    bid = c19.bundle_id(b)
                    if bid not in feedback_items_by_bundle:
                        _generate_feedback_for_bundle(b, gemini_api_key, gemini_model)
                feedback_items_by_bundle = copy.deepcopy(st.session_state.get("feedback_items_by_bundle", {})) if enable_specific_feedback else {}
        except Exception as e:
            st.warning(f"Không sinh được explanation cho toàn bộ bundles: {e}")

    outputs_xml: List[Path] = []
    outputs_html: List[Path] = []
    assets_zip_paths: List[Path] = []
    try:
        if output_mode == "1 file cho tất cả tests":
            if export_xml:
                with st.spinner("Đang build XML..."):
                    if export_dual_xml_vocab:
                        stem = Path(xml_name).stem
                        suffix = Path(xml_name).suffix or ".xml"
                        xml_practice = c19.build_moodle_xml_reading(pdf_for_export, bundles, scans, out_dir, cache_dir, lang, float(image_zoom), int(jpeg_quality), category, bool(shuffle_choices), f"{stem}_practice_vocab{suffix}", keys_by_test, group_overrides, text_data_by_bundle, feedback_items_by_bundle, transcript_export_mode=transcript_export_mode, include_keywords=True)
                        xml_exam = c19.build_moodle_xml_reading(pdf_for_export, bundles, scans, out_dir, cache_dir, lang, float(image_zoom), int(jpeg_quality), category, bool(shuffle_choices), f"{stem}_exam_no_vocab{suffix}", keys_by_test, group_overrides, text_data_by_bundle, feedback_items_by_bundle, transcript_export_mode=transcript_export_mode, include_keywords=False)
                        outputs_xml.extend([xml_practice, xml_exam])
                    else:
                        xml_path = c19.build_moodle_xml_reading(pdf_for_export, bundles, scans, out_dir, cache_dir, lang, float(image_zoom), int(jpeg_quality), category, bool(shuffle_choices), xml_name, keys_by_test, group_overrides, text_data_by_bundle, feedback_items_by_bundle, transcript_export_mode=transcript_export_mode, include_keywords=True)
                        outputs_xml.append(xml_path)
            if export_html:
                with st.spinner("Đang build HTML..."):
                    html_path, assets_dir = c19.build_html_reading(pdf_for_export, bundles, scans, out_dir, cache_dir, lang, float(image_zoom), int(jpeg_quality), bool(shuffle_choices), html_mode, html_name, bool(export_html_assets), keys_by_test, group_overrides, text_data_by_bundle, feedback_items_by_bundle, transcript_export_mode=transcript_export_mode, include_keywords=True)
                outputs_html.append(html_path)
                if assets_dir:
                    assets_zip_paths.append(_zip_dir(assets_dir, out_dir / "html_assets.zip"))
        else:
            bundles_by_test: Dict[int, List[c19.PassageBundle]] = {}
            for b in bundles:
                bundles_by_test.setdefault(b.test_num, []).append(b)
            for t in sorted(bundles_by_test.keys()):
                tb = bundles_by_test[t]
                kb = {t: keys_by_test[t]}
                if export_xml:
                    with st.spinner(f"Đang build XML Test {t}..."):
                        base_name = (f"moodle_reading_test{t}.xml" if st.session_state.get("prepared_skill", skill) == "reading" else f"moodle_listening_test{t}.xml")
                        stem = Path(base_name).stem
                        suffix = Path(base_name).suffix or ".xml"
                        if export_dual_xml_vocab:
                            xml_practice = c19.build_moodle_xml_reading(pdf_for_export, tb, scans, out_dir, cache_dir, lang, float(image_zoom), int(jpeg_quality), category, bool(shuffle_choices), f"{stem}_practice_vocab{suffix}", kb, group_overrides, text_data_by_bundle, feedback_items_by_bundle, transcript_export_mode=transcript_export_mode, include_keywords=True)
                            xml_exam = c19.build_moodle_xml_reading(pdf_for_export, tb, scans, out_dir, cache_dir, lang, float(image_zoom), int(jpeg_quality), category, bool(shuffle_choices), f"{stem}_exam_no_vocab{suffix}", kb, group_overrides, text_data_by_bundle, feedback_items_by_bundle, transcript_export_mode=transcript_export_mode, include_keywords=False)
                            outputs_xml.extend([xml_practice, xml_exam])
                        else:
                            xml_path = c19.build_moodle_xml_reading(pdf_for_export, tb, scans, out_dir, cache_dir, lang, float(image_zoom), int(jpeg_quality), category, bool(shuffle_choices), base_name, kb, group_overrides, text_data_by_bundle, feedback_items_by_bundle, transcript_export_mode=transcript_export_mode, include_keywords=True)
                            outputs_xml.append(xml_path)
                if export_html:
                    with st.spinner(f"Đang build HTML Test {t}..."):
                        html_path, assets_dir = c19.build_html_reading(pdf_for_export, tb, scans, out_dir, cache_dir, lang, float(image_zoom), int(jpeg_quality), bool(shuffle_choices), html_mode, (f"reading_preview_test{t}.html" if st.session_state.get("prepared_skill", skill) == "reading" else f"listening_preview_test{t}.html"), bool(export_html_assets), kb, group_overrides, text_data_by_bundle, feedback_items_by_bundle, transcript_export_mode=transcript_export_mode, include_keywords=True)
                    outputs_html.append(html_path)
                    if assets_dir:
                        assets_zip_paths.append(_zip_dir(assets_dir, out_dir / f"html_assets_test{t}.zip"))
        manifest_path = out_dir / f"cambridge_plus_{st.session_state.get('prepared_skill', skill)}_{MANIFEST_VERSION}.camplus.json"
        manifest_path = _save_manifest_file(manifest_path, source_label="export")
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        companion_paths: List[Path] = [manifest_path]
        for p in outputs_xml:
            companion = p.with_name(p.name + ".camplus.json")
            companion.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            companion_paths.append(companion)
            _embed_manifest_in_xml_file(p, manifest_payload)
        for p in outputs_html:
            companion = p.with_name(p.name + ".camplus.json")
            companion.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            companion_paths.append(companion)
            _embed_manifest_in_html_file(p, manifest_payload)
        transcript_companion_paths: List[Path] = []
        audio_snippet_companion_paths: List[Path] = []
        listenonce_companion_paths: List[Path] = []
        if st.session_state.get("prepared_skill", skill) == "listening":
            transcript_companion_paths = _export_listening_transcript_companions(out_dir, bundles, text_data_by_bundle)
            audio_snippet_companion_paths = _export_listening_audio_snippet_companions(out_dir, bundles, text_data_by_bundle)
            listenonce_companion_paths = _export_listenonce_additional_html_companion(out_dir)
            if not transcript_companion_paths:
                html_fallback = out_dir / "listening_transcripts_review.html"
                md_fallback = out_dir / "listening_transcripts_review.md"
                html_fallback.write_text("<!doctype html><html><head><meta charset='utf-8'><title>Listening transcripts review</title></head><body><p>No listening transcript content was found at export time.</p></body></html>", encoding="utf-8")
                md_fallback.write_text("No listening transcript content was found at export time.\n", encoding="utf-8")
                transcript_companion_paths = [html_fallback, md_fallback]
            if not audio_snippet_companion_paths:
                html_fallback = out_dir / "listening_audio_description_snippets.html"
                md_fallback = out_dir / "listening_audio_description_snippets.md"
                html_fallback.write_text("<!doctype html><html><head><meta charset='utf-8'><title>Listening audio description snippets</title></head><body><p>No listening audio files were attached at export time.</p></body></html>", encoding="utf-8")
                md_fallback.write_text("No listening audio files were attached at export time.\n", encoding="utf-8")
                audio_snippet_companion_paths = [html_fallback, md_fallback]
        st.success("✅ Export xong.")
        for p in outputs_xml:
            st.write(f"XML: `{p}`")
        for p in outputs_html:
            st.write(f"HTML: `{p}`")
        for p in assets_zip_paths:
            st.write(f"Assets ZIP: `{p}`")
        for p in companion_paths:
            st.write(f"Manifest: `{p}`")
        for p in transcript_companion_paths:
            st.write(f"Transcript companion: `{p}`")
        for p in audio_snippet_companion_paths:
            st.write(f"Audio description snippet companion: `{p}`")
        for p in listenonce_companion_paths:
            st.write(f"Additional HTML companion: `{p}`")
    except Exception as e:
        st.error(f"Export lỗi: {e}")
