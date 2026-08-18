"""炸开主流程：一张图 -> 分层可编辑文档。

输出目录结构：
  workspace/<job_id>/
    base.png        原图（文档基准像素，保真渲染的底）
    clean.png       所有元素擦除后的干净背景（元素被移动/改写时用来打补丁）
    layers/<id>.png 每个元素的 RGBA 切片
    layout.json     图层与样式数据
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import asdict
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

import inpainter
import ocr_engines
import segmenter
import style_infer
from ocr_engines import TextLine
from style_infer import RegionAnalysis, TextStyle

MAX_ANALYZE_SIDE = 3000


def _rgb_hex(rgb: Sequence[int]) -> str:
    r, g, b = (int(max(0, min(255, v))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def load_image(path: str, max_side: int = MAX_ANALYZE_SIDE) -> Tuple[np.ndarray, np.ndarray, float]:
    """读取图片，返回 (BGR 分析图, BGRA 原始图, 缩放比例)。"""
    raw = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if raw is None:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGBA")
            raw = cv2.cvtColor(np.array(im), cv2.COLOR_RGBA2BGRA)

    if raw.ndim == 2:
        raw = cv2.cvtColor(raw, cv2.COLOR_GRAY2BGRA)
    elif raw.shape[2] == 3:
        raw = cv2.cvtColor(raw, cv2.COLOR_BGR2BGRA)

    h, w = raw.shape[:2]
    scale = 1.0
    long_side = max(h, w)
    if long_side > max_side:
        scale = max_side / float(long_side)
        raw = cv2.resize(raw, (int(round(w * scale)), int(round(h * scale))),
                         interpolation=cv2.INTER_AREA)

    # 透明区域合成到白底用于分析，避免 alpha 干扰 OCR 与阈值
    bgra = raw
    alpha = bgra[:, :, 3].astype(np.float32) / 255.0
    bgr = bgra[:, :, :3].astype(np.float32)
    white = np.full_like(bgr, 255.0)
    analyze = (bgr * alpha[..., None] + white * (1 - alpha[..., None])).astype(np.uint8)
    return analyze, bgra, scale


def _stamp_mask(canvas: np.ndarray, patch_mask: np.ndarray,
                rect: Tuple[int, int, int, int]) -> None:
    """把局部掩码合并进全幅掩码画布（原地取最大值）。"""
    x, y, w, h = rect
    ph, pw = patch_mask.shape[:2]
    w, h = min(w, pw, canvas.shape[1] - x), min(h, ph, canvas.shape[0] - y)
    if w <= 0 or h <= 0:
        return
    sub = canvas[y:y + h, x:x + w]
    np.maximum(sub, patch_mask[:h, :w], out=sub)


def _rects_overlap(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def _contains(outer: Sequence[float], inner: Sequence[float], ratio: float) -> bool:
    """inner 有 ratio 以上的面积落在 outer 里。"""
    ax, ay, aw, ah = outer
    bx, by, bw, bh = inner
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return False
    return (x2 - x1) * (y2 - y1) >= bw * bh * ratio


def _layer_name(text: str, limit: int = 12) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


SHAPE_NAMES = {
    "rect": "矩形色块",
    "rounded-rect": "圆角色块",
    "ellipse": "椭圆",
    "line": "线条",
    "icon": "图标",
    "image": "图像",
}


def explode(image_path: str,
            out_dir: str,
            job_id: Optional[str] = None,
            ocr_engine: str = "auto",
            detect_text: bool = True,
            detect_shapes: bool = True,
            detect_images: bool = True,
            strength: str = "standard",
            max_side: int = MAX_ANALYZE_SIDE,
            min_text_conf: float = 0.3) -> Dict:
    started = time.time()
    job_id = job_id or uuid.uuid4().hex[:12]
    job_dir = os.path.join(out_dir, job_id)
    layers_dir = os.path.join(job_dir, "layers")
    os.makedirs(layers_dir, exist_ok=True)

    analyze, bgra, scale = load_image(image_path, max_side=max_side)
    h, w = analyze.shape[:2]

    cv2.imwrite(os.path.join(job_dir, "base.png"), bgra)

    # ---------------- 文字 ---------------- #
    lines: List[TextLine] = []
    engines_used: List[str] = []
    if detect_text:
        lines, engines_used = ocr_engines.detect_text(
            analyze, engine=ocr_engine, multi_scale=True, min_conf=min_text_conf)

    analyses: List[RegionAnalysis] = []
    kept_lines: List[TextLine] = []
    for line in lines:
        result = style_infer.analyze_text_region(analyze, line)
        if result is None:
            continue
        st = result.style
        # 墨迹占满整框且对比度低，多半是把色块误当成文字
        if st.coverage > 0.93 and st.contrast < 0.18:
            continue
        if st.contrast < 0.05:
            continue
        analyses.append(result)
        kept_lines.append(line)

    text_regions: List[Tuple[np.ndarray, Tuple[int, int, int, int]]] = [
        (inpainter.fill_large_holes(
            res.erase_mask if res.erase_mask is not None else res.mask), res.patch_rect)
        for res in analyses
    ]
    text_mask_union = np.zeros((h, w), np.uint8)
    for mask_local, rect in text_regions:
        _stamp_mask(text_mask_union, mask_local, rect)
    text_boxes = [rect for _, rect in text_regions]

    styles = [res.style for res in analyses]
    style_infer.normalize_weights(styles)

    # ---------------- 非文字元素 ---------------- #
    elements: List[segmenter.Element] = []
    if detect_shapes or detect_images:
        elements = segmenter.segment_elements(
            analyze, text_mask=text_mask_union, strength=strength,
            detect_shapes=detect_shapes)
        if not detect_shapes:
            elements = [e for e in elements if e.kind in ("image", "icon")]
        if not detect_images:
            elements = [e for e in elements if e.kind not in ("image", "icon")]

    # ---------------- 剔掉图标里的假文字 ---------------- #
    # OCR 会把图标内部的图形当字读（实测一个齿轮图标被读成「5」）。留着它有三重坏处：
    # 图层列表多一条垃圾、干净底图会把图标挖掉一块、批量转清晰时真的把「5」画到图标上。
    # 短、置信度低、又整个躺在一个小图标里，三条同时满足才剔除，正常的图上文字不会中招。
    icon_boxes = [e.bbox for e in elements
                  if e.kind in ("icon", "image") and e.area_ratio < 0.02]
    if icon_boxes:
        keep_idx = []
        for idx, (line, res) in enumerate(zip(kept_lines, analyses)):
            ink = res.style.ink_bbox
            if (len([c for c in line.text if not c.isspace()]) <= 2
                    and line.conf < 0.6
                    and any(_contains(box, ink, 0.85) for box in icon_boxes)):
                continue
            keep_idx.append(idx)
        if len(keep_idx) < len(kept_lines):
            kept_lines = [kept_lines[i] for i in keep_idx]
            analyses = [analyses[i] for i in keep_idx]
            text_regions = [text_regions[i] for i in keep_idx]
            text_boxes = [rect for _, rect in text_regions]
            text_mask_union = np.zeros((h, w), np.uint8)
            for mask_local, rect in text_regions:
                _stamp_mask(text_mask_union, mask_local, rect)
            styles = [res.style for res in analyses]

    # 段落分组要在剔除之后做，否则对齐方式会按旧下标错位到别的行上
    groups = style_infer.group_paragraphs(kept_lines, styles) if kept_lines else []
    para_of: Dict[int, int] = {}
    align_of: Dict[int, str] = {}
    for gi, group in enumerate(groups):
        align = style_infer.infer_alignment(kept_lines, group)
        for idx in group:
            para_of[idx] = gi
            align_of[idx] = align

    # ---------------- 分级干净背景 ---------------- #
    # clean_text：只擦文字，保留横幅/卡片等底板 —— 文字被移动或改写时用它打补丁，
    #             这样标题挪个位置不会露出一块白。
    # clean_all ：在此基础上再擦掉图形元素 —— 色块/图片被移动时才用它。
    clean_text = inpainter.build_clean_background(analyze, text_regions, dilate=2)
    element_regions = [(e.mask, e.bbox) for e in elements]
    clean_all = (inpainter.build_clean_background(clean_text, element_regions, dilate=2)
                 if element_regions else clean_text)

    for name, img in (("clean_text.png", clean_text), ("clean_all.png", clean_all)):
        out = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        out[:, :, 3] = bgra[:, :, 3]
        cv2.imwrite(os.path.join(job_dir, name), out)

    # ---------------- 组装图层 ---------------- #
    layers: List[Dict] = []

    for i, el in enumerate(elements):
        lid = f"e{i}"
        x, y, bw, bh = el.bbox
        # 主切片取自原图，重绘时和原图逐像素一致。
        rgba = segmenter.cutout_rgba(analyze, el.mask, el.bbox, feather=0.8)
        slice_name = f"{lid}.png"
        cv2.imwrite(os.path.join(layers_dir, slice_name),
                    cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))

        # 压着文字的元素（横幅、按钮等）额外存一份「已擦字」切片：只有当它上面的
        # 文字真的被改动或挪走时才用，否则原文字会随着这个元素一起被重新画回来。
        slice_clean_name = None
        if any(_rects_overlap(el.bbox, r) for r in text_boxes):
            clean_rgba = segmenter.cutout_rgba(clean_text, el.mask, el.bbox, feather=0.8)
            slice_clean_name = f"{lid}_clean.png"
            cv2.imwrite(os.path.join(layers_dir, slice_clean_name),
                        cv2.cvtColor(clean_rgba, cv2.COLOR_RGBA2BGRA))
        is_shape = el.kind in ("rect", "rounded-rect", "ellipse", "line") and el.fill is not None
        layers.append({
            "id": lid,
            "type": "shape" if is_shape else "image",
            "shape": el.kind if is_shape else None,
            "name": SHAPE_NAMES.get(el.kind, "元素"),
            "x": float(x), "y": float(y), "w": float(bw), "h": float(bh),
            "rotation": 0.0, "opacity": 1.0, "visible": True, "locked": False,
            "fill": _rgb_hex(el.fill) if el.fill else None,
            "radius": float(el.radius),
            "slice": f"layers/{slice_name}",
            "sliceClean": f"layers/{slice_clean_name}" if slice_clean_name else None,
            "sliceRect": [float(x), float(y), float(bw), float(bh)],
            "kind": el.kind,
            "depth": el.depth,
            "areaRatio": round(el.area_ratio, 5),
            "meta": el.meta,
        })

    for i, (line, res) in enumerate(zip(kept_lines, analyses)):
        lid = f"t{i}"
        st = res.style
        px, py, pw, ph = res.patch_rect
        slice_name = f"{lid}.png"
        cv2.imwrite(os.path.join(layers_dir, slice_name),
                    cv2.cvtColor(res.rgba, cv2.COLOR_RGBA2BGRA))

        tx, ty, tw, th = line.bbox
        ink = st.ink_bbox
        angle = line.angle_deg
        rotation = angle if abs(angle) > 2.0 else 0.0

        layers.append({
            "id": lid,
            "type": "text",
            "name": _layer_name(line.text),
            "x": float(px), "y": float(py), "w": float(pw), "h": float(ph),
            "rotation": float(round(rotation, 2)),
            "opacity": 1.0, "visible": True, "locked": False,
            "text": line.text,
            "fontSize": st.font_size,
            "fontFamily": None,
            "fontWeight": st.font_weight,
            "italicDeg": st.italic_deg,
            "serif": st.serif,
            "color": _rgb_hex(st.color),
            "bgColor": _rgb_hex(st.bg_color),
            "letterSpacing": st.letter_spacing,
            "lineHeight": 1.25,
            "align": align_of.get(i, "left"),
            "baselineOffset": st.baseline_offset,
            "inkBox": [float(v) for v in ink],
            "textBox": [float(tx), float(ty), float(tw), float(th)],
            "slice": f"layers/{slice_name}",
            "sliceRect": [float(px), float(py), float(pw), float(ph)],
            "isCJK": st.is_cjk,
            "confidence": round(float(line.conf), 4),
            "quality": st.quality,
            "strokeWidth": st.stroke_width,
            "paragraph": para_of.get(i, i),
            "ocr": line.source,
        })

    layout = {
        "version": 2,
        "jobId": job_id,
        "width": int(w),
        "height": int(h),
        "sourceScale": round(scale, 5),
        "assets": {"base": "base.png", "cleanText": "clean_text.png",
                   "cleanAll": "clean_all.png"},
        "layers": layers,
        "stats": {
            "textLayers": len(kept_lines),
            "elementLayers": len(elements),
            "ocrEngines": engines_used or ocr_engines.available_engine_names(),
            "strength": strength,
            "elapsed": round(time.time() - started, 2),
        },
    }

    with open(os.path.join(job_dir, "layout.json"), "w", encoding="utf-8") as fh:
        json.dump(layout, fh, ensure_ascii=False, indent=1)
    return layout


def extract_region(image_path: str, job_dir: str, rect: Tuple[int, int, int, int],
                   mode: str = "grabcut", max_side: int = MAX_ANALYZE_SIDE) -> Dict:
    """用户在画布上框选一块区域，抠成新图层。"""
    analyze, _bgra, _scale = load_image(image_path, max_side=max_side)
    layers_dir = os.path.join(job_dir, "layers")
    os.makedirs(layers_dir, exist_ok=True)

    if mode == "rect":
        h, w = analyze.shape[:2]
        x, y, rw, rh = (int(max(0, rect[0])), int(max(0, rect[1])),
                        int(rect[2]), int(rect[3]))
        rw, rh = max(1, min(rw, w - x)), max(1, min(rh, h - y))
        mask = np.full((rh, rw), 255, np.uint8)
        tight = (x, y, rw, rh)
    else:
        mask, tight = segmenter.grabcut_cutout(analyze, tuple(int(v) for v in rect))

    lid = f"m{uuid.uuid4().hex[:8]}"
    rgba = segmenter.cutout_rgba(analyze, mask, tight, feather=0.6)
    cv2.imwrite(os.path.join(layers_dir, f"{lid}.png"),
                cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))

    x, y, bw, bh = tight
    return {
        "id": lid,
        "type": "image",
        "name": "手动提取",
        "x": float(x), "y": float(y), "w": float(bw), "h": float(bh),
        "rotation": 0.0, "opacity": 1.0, "visible": True, "locked": False,
        "slice": f"layers/{lid}.png",
        "sliceRect": [float(x), float(y), float(bw), float(bh)],
        "kind": "manual",
    }


def _ink_height(crop_bgr: np.ndarray) -> float:
    """估计裁片里字的实际墨迹高度（不含留白），用来定放大倍数。"""
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _t, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # 文字可能是亮底暗字也可能是暗底亮字，占比少的那一类才是笔画
    if int((binary > 0).sum()) * 2 > binary.size:
        binary = cv2.bitwise_not(binary)
    rows = np.where(binary.any(axis=1))[0]
    if rows.size < 2:
        return float(crop_bgr.shape[0])
    return float(rows[-1] - rows[0] + 1)


def reread_text(image_path: str, rect: Tuple[float, float, float, float],
                engine: str = "auto", target_height: float = 96.0,
                max_side: int = MAX_ANALYZE_SIDE) -> Dict:
    """把一行字裁出来放大后重认，返回若干候选读法供前端裁决。

    整图 OCR 的输入尺度对小字很不友好：字高只有二三十像素时，模型看到的笔画已经糊成一团，
    「刻」认成「翅」这种错就来了。单独裁出来 Lanczos 放大再认，识别率高得多。

    不在这里下结论选哪个读法：不同引擎的置信度不是一个量纲（实测 RapidOCR 给 0.82 的
    「爱限」是错的，Vision 给 0.3 的「受限」才对），跨引擎比分数会挑错。所以把去重后的
    候选连同票数一起返回，由前端拿各自渲染出来和原图笔画比对，谁更像选谁。
    """
    analyze, _bgra, _scale = load_image(image_path, max_side=max_side)
    h, w = analyze.shape[:2]
    x, y, rw, rh = (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    names = ocr_engines.available_engine_names() if engine == "auto" else [engine]
    pool: Dict[str, Dict] = {}
    ink_h = max(6.0, rh)
    all_scales: List[float] = []

    # 裁得紧、裁得松各来一遍：贴着笔画裁容易掉笔锋（实测紧裁把「EUV光刻机」读成「机光效」），
    # 留白太多又会把邻行也框进来。两种都认一遍，正确读法自然拿到更多票。
    for pad_ratio in (0.18, 0.5):
        pad = max(4.0, min(rw, rh) * pad_ratio)
        x0 = int(max(0, math.floor(x - pad)))
        y0 = int(max(0, math.floor(y - pad)))
        x1 = int(min(w, math.ceil(x + rw + pad)))
        y1 = int(min(h, math.ceil(y + rh + pad)))
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue

        crop = analyze[y0:y1, x0:x1]
        ink_h = max(6.0, _ink_height(crop))
        # 一个尺度可能刚好把某个字认坏，多给两档（够大 / 更大）再投票
        scales = sorted({round(float(np.clip(t / ink_h, 1.0, 10.0)), 2)
                         for t in (target_height, target_height * 1.75)})
        all_scales.extend(scales)
        for s in scales:
            img = crop if s <= 1.01 else cv2.resize(crop, None, fx=s, fy=s,
                                                    interpolation=cv2.INTER_LANCZOS4)
            for name in names:
                try:
                    lines, _used = ocr_engines.detect_text(img, engine=name,
                                                          multi_scale=False, min_conf=0.05)
                except Exception:  # noqa: BLE001  某个引擎挂了不该影响其它引擎
                    continue
                if not lines:
                    continue
                # 裁出来的是一行字，个别引擎会切成几段，按阅读顺序拼回去
                lines.sort(key=lambda l: (round(l.bbox[1] / max(8.0, ink_h * s * 0.6)),
                                          l.bbox[0]))
                text = "".join(l.text for l in lines).strip()
                if not text:
                    continue
                conf = float(sum(l.conf for l in lines) / len(lines))
                slot = pool.setdefault(text, {"text": text, "conf": 0.0, "votes": 0,
                                              "engines": [], "scales": []})
                slot["conf"] = max(slot["conf"], round(conf, 4))
                slot["votes"] += 1
                if name not in slot["engines"]:
                    slot["engines"].append(name)
                slot["scales"].append(s)

    candidates = sorted(pool.values(), key=lambda c: (-c["votes"], -c["conf"]))
    best = candidates[0] if candidates else {"text": "", "conf": 0.0, "engines": []}
    return {
        "text": best["text"],
        "conf": best["conf"],
        "engine": (best.get("engines") or [""])[0],
        "inkHeight": round(ink_h, 1),
        "scales": sorted(set(all_scales)),
        "candidates": candidates,
    }


def rebuild_clean(image_path: str, job_dir: str, rects: List[Sequence[float]],
                  max_side: int = MAX_ANALYZE_SIDE) -> str:
    """按给定矩形列表重算干净背景（用于手动新增擦除区域）。"""
    analyze, bgra, _scale = load_image(image_path, max_side=max_side)
    h, w = analyze.shape[:2]
    regions = []
    for r in rects:
        x, y, rw, rh = (int(max(0, r[0])), int(max(0, r[1])), int(r[2]), int(r[3]))
        rw, rh = max(1, min(rw, w - x)), max(1, min(rh, h - y))
        regions.append((np.full((rh, rw), 255, np.uint8), (x, y, rw, rh)))
    clean = inpainter.build_clean_background(analyze, regions, dilate=1)
    out = cv2.cvtColor(clean, cv2.COLOR_BGR2BGRA)
    out[:, :, 3] = bgra[:, :, 3]
    name = f"clean_{uuid.uuid4().hex[:6]}.png"
    cv2.imwrite(os.path.join(job_dir, name), out)
    return name
