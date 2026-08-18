"""从像素反推文字样式：颜色、字号、字重、衬线、倾斜、字距、软抠图 alpha。

核心思路：
  1. 在文字外扩区域内做 Otsu 二值化，用「边缘环颜色」判定前景/背景极性；
  2. 前景色取笔画核心（腐蚀后）像素中位数，避免抗锯齿边缘拉低饱和度；
  3. 用线性色彩解混得到软 alpha，使抠出的文字保留抗锯齿，移动后边缘不毛刺；
  4. 字号由墨迹高度按字符构成（CJK / 有无升降部）折算，前端再用 measureText 精调。
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from ocr_engines import TextLine

# 拉丁字母的垂直度量近似（相对 em 的比例）
_ASCENDER_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZbdfhkltβ0123456789$#@&()[]{}/\\|!?")
_DESCENDER_CHARS = set("gjpqy,;()[]{}/\\|Q")


def has_cjk(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        if (0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF
                or 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF
                or 0xF900 <= code <= 0xFAFF or 0x3000 <= code <= 0x303F):
            return True
    return False


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class TextStyle:
    color: Tuple[int, int, int] = (0, 0, 0)          # RGB
    bg_color: Tuple[int, int, int] = (255, 255, 255)  # RGB
    bg_uniform: bool = True
    bg_std: float = 0.0
    contrast: float = 1.0
    font_size: float = 16.0
    font_weight: int = 400
    stroke_width: float = 1.0
    serif: bool = False
    italic_deg: float = 0.0
    letter_spacing: float = 0.0
    baseline_offset: float = 0.0   # 基线相对 ink 顶部的偏移（像素）
    ink_bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    is_cjk: bool = False
    coverage: float = 0.0          # 墨迹占比，用于剔除误检
    quality: float = 1.0           # 样式推断可信度


@dataclass
class RegionAnalysis:
    style: TextStyle
    mask: np.ndarray                    # patch 坐标系下的文字掩码 0/255
    alpha: np.ndarray                   # patch 坐标系下的软 alpha 0~255
    patch_rect: Tuple[int, int, int, int]
    rgba: Optional[np.ndarray] = None    # 抠出的 RGBA 切片（patch 尺寸）


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def expand_rect(rect: Sequence[float], pad: float,
                w: int, h: int) -> Tuple[int, int, int, int]:
    x, y, rw, rh = rect
    x0 = int(max(0, math.floor(x - pad)))
    y0 = int(max(0, math.floor(y - pad)))
    x1 = int(min(w, math.ceil(x + rw + pad)))
    y1 = int(min(h, math.ceil(y + rh + pad)))
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


def _border_ring_mean(patch: np.ndarray, ring: int = 2) -> np.ndarray:
    """取 patch 最外圈像素的中位数颜色，作为背景色的先验。"""
    h, w = patch.shape[:2]
    ring = max(1, min(ring, h // 2, w // 2))
    parts = [
        patch[:ring, :].reshape(-1, patch.shape[2]),
        patch[-ring:, :].reshape(-1, patch.shape[2]),
        patch[:, :ring].reshape(-1, patch.shape[2]),
        patch[:, -ring:].reshape(-1, patch.shape[2]),
    ]
    stacked = np.concatenate(parts, axis=0).astype(np.float32)
    return np.median(stacked, axis=0)


def _surround_median(patch: np.ndarray, inner: Tuple[int, int, int, int]) -> np.ndarray:
    """取「紧贴文字框外面那一圈」的中位数颜色，作为背景色先验。

    不用 patch 最外圈：文字框常常紧挨着另一个区块的边界（比如一段深色页脚带的上
    沿），最外圈可能整条都落在隔壁区块里，判出来的极性就反了。取环形区域的中位数
    则由离文字最近的那一圈说话，少数越界的边不影响结果。
    """
    x0, y0, x1, y1 = inner
    ring_mask = np.ones(patch.shape[:2], dtype=bool)
    ring_mask[y0:y1, x0:x1] = False
    sel = patch[ring_mask]
    if sel.size == 0:
        return _border_ring_mean(patch)
    return np.median(sel.astype(np.float32), axis=0)


def _median_color(patch: np.ndarray, mask: np.ndarray) -> np.ndarray:
    sel = patch[mask > 0]
    if sel.size == 0:
        return np.array([0.0, 0.0, 0.0], dtype=np.float32)
    return np.median(sel.astype(np.float32), axis=0)


def _drop_tiny_components(mask: np.ndarray, min_area: float) -> np.ndarray:
    num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = np.zeros_like(mask)
    for i in range(1, num):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            out[labels == i] = 255
    return out


def _ink_bbox(mask: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)


# --------------------------------------------------------------------------- #
# 字号 / 字重 / 倾斜
# --------------------------------------------------------------------------- #

def estimate_font_size(text: str, ink_h: float, chars: Sequence = ()) -> Tuple[float, float]:
    """由墨迹高度反推字号，返回 (font_size, baseline_offset_from_ink_top)。

    行墨迹并集高度会被标点下沉、抗锯齿、相邻元素污染而偏大，因此仅在拿不到
    逐字符包围盒时使用；有字符框时由 estimate_font_size_from_chars 主导。
    """
    if ink_h <= 0:
        return 16.0, 12.0

    if has_cjk(text):
        # 实测标定：紧致墨迹高度 ≈ 0.938 em（含标点下沉的行并集）
        size = ink_h / 0.938
        # 汉字字形底部基本落在基线上，略留 2% 余量
        baseline = ink_h * 1.02
        return size, baseline

    stripped = [c for c in text if not c.isspace()]
    has_asc = any(c in _ASCENDER_CHARS for c in stripped)
    has_desc = any(c in _DESCENDER_CHARS for c in stripped)
    has_lower_only = bool(stripped) and not has_asc

    if has_asc and has_desc:
        ratio, base_ratio = 0.945, 0.755 / 0.945
    elif has_asc:
        ratio, base_ratio = 0.765, 1.0
    elif has_desc and has_lower_only:
        ratio, base_ratio = 0.735, 0.72
    else:
        ratio, base_ratio = 0.525, 1.0

    size = ink_h / ratio
    baseline = ink_h * base_ratio
    return size, baseline


def estimate_font_size_from_chars(text: str, chars: Sequence) -> Optional[float]:
    """用 CJK 字符墨迹宽度反推字号。

    实测（macOS Vision）：CJK 字符框宽度 ≈ 1.037 em，离散度远小于框高度（1.14 em，
    方差大），也不受上下相邻元素污染，因此是最可靠的单一信号。拉丁字宽不等宽，
    这条路不适用，交由行墨迹高度处理。
    """
    if not chars:
        return None
    cjk = [c for c in chars if has_cjk(c.text) and c.bbox[2] > 0.5 and c.bbox[3] > 0.5]
    if len(cjk) < 2:
        return None
    # 75 分位代表「满宽」的字，规避「一」「、」等窄字形
    w75 = float(np.percentile([c.bbox[2] for c in cjk], 75))
    return w75 / 1.037


def _run_coverage_map(mask: np.ndarray, cover: np.ndarray) -> np.ndarray:
    """每个前景像素所在的水平连续段，覆盖率积分是多少（= 该段的亚像素宽度）。"""
    h, w = mask.shape
    out = np.zeros((h, w), np.float32)
    pad = np.zeros((h, w + 2), np.int8)
    pad[:, 1:-1] = mask.astype(np.int8)
    edges = np.diff(pad, axis=1)
    csum = np.zeros((h, w + 1), np.float32)
    np.cumsum(cover, axis=1, out=csum[:, 1:])
    for y in range(h):
        starts = np.flatnonzero(edges[y] == 1)
        ends = np.flatnonzero(edges[y] == -1)
        for s, e in zip(starts, ends):
            # 段两侧各扩 1 像素，把抗锯齿过渡带的那部分覆盖率也算进宽度
            lo = max(0, s - 1)
            hi = min(w, e + 1)
            out[y, s:e] = csum[y, hi] - csum[y, lo]
    return out


def estimate_stroke_width(mask: np.ndarray, alpha: Optional[np.ndarray] = None) -> float:
    """估计笔画粗细：横向、纵向连续段的亚像素宽度取小者，再取中位数。

    横笔画的横向段是笔画长度、纵向段才是它的粗细，竖笔画正好相反，取两者较小值就
    总是拿到粗细；取中位数则让少数糊在一起的笔画不左右结果。宽度用覆盖率积分算而
    不是数像素个数——小号字的笔画只有两三个像素宽，按整数数会跳档，同样粗细的两行
    小字可能一个算 2px 一个算 3px，字重就跟着乱跳。

    早先用距离变换均值的做法在小号汉字上会严重偏大（笔画一糊成块，块内距离迅速变
    大），导致 20px 的正文测出来比 52px 的粗体标题还粗。
    """
    m = mask > 0
    if not np.any(m):
        return 1.0
    cover = (alpha.astype(np.float32) / 255.0) if alpha is not None else m.astype(np.float32)
    cover = np.clip(cover, 0.0, 1.0)
    horiz = _run_coverage_map(m, cover)
    vert = _run_coverage_map(m.T, cover.T).T
    widths = np.minimum(horiz, vert)[m]
    widths = widths[widths > 0]
    if widths.size == 0:
        return 1.0
    return float(np.median(widths))


def normalize_weights(styles: Sequence["TextStyle"]) -> None:
    """在文档内相对判定字重（就地修改 font_weight）。

    笔画粗细的绝对比值会被抗锯齿（小字偏粗）和字形复杂度（汉字交叉点多）系统性
    带偏，绝对阈值很难同时适配大标题和小字注释。而同一张图里「哪些字更粗」是稳定
    可比的，所以以组内中位数为基准做相对判定；样本太少时退回绝对阈值。
    """
    offset = _antialias_offset(styles)
    for is_cjk in (True, False):
        group = [s for s in styles if s.is_cjk == is_cjk and s.font_size > 0]
        if len(group) < 3:
            continue
        ratios = [max(s.stroke_width - offset, 0.2) / s.font_size for s in group]
        median = float(np.median(ratios))
        if median <= 1e-6:
            continue
        for style, ratio in zip(group, ratios):
            rel = ratio / median
            if rel > 1.45:
                style.font_weight = 800
            elif rel > 1.13:
                style.font_weight = 700
            elif rel < 0.82:
                style.font_weight = 300
            else:
                style.font_weight = 400


def _antialias_offset(styles: Sequence["TextStyle"]) -> float:
    """估计笔画测量里那段与字号无关的固定偏差。

    抗锯齿的过渡带宽度、以及小字里相邻笔画糊成一团，都会给测出来的笔画粗细加上
    大致恒定的几个像素。不减掉它，小字的「粗细占字号比例」会系统性地高于大字——
    结果一张图里最粗的大标题反而被判成最细。用 stroke ≈ a·size + c 拟合出 c，
    剔掉一轮离群点（真正的粗体/细体）再拟合，得到的 c 就是这段固定偏差。
    """
    pts = [(s.font_size, s.stroke_width) for s in styles if s.font_size > 4]
    if len(pts) < 5:
        return 0.0
    sizes = np.array([p[0] for p in pts], dtype=np.float64)
    strokes = np.array([p[1] for p in pts], dtype=np.float64)
    if float(sizes.max() - sizes.min()) < 8.0:
        return 0.0        # 字号跨度太小，拟合不出斜率，宁可不减

    keep = np.ones(len(sizes), dtype=bool)
    slope = intercept = 0.0
    for _ in range(2):
        slope, intercept = np.polyfit(sizes[keep], strokes[keep], 1)
        resid = np.abs(strokes - (slope * sizes + intercept))
        limit = float(np.percentile(resid, 70))
        keep = resid <= max(limit, 1e-6)
        if keep.sum() < 4:
            break
    if slope <= 0:
        return 0.0
    return float(np.clip(intercept, 0.0, float(np.min(strokes)) * 0.85))


def classify_weight(stroke: float, font_size: float, is_cjk: bool) -> int:
    if font_size <= 0:
        return 400
    ratio = stroke / font_size
    if is_cjk:
        heavy, bold, light = 0.148, 0.116, 0.076
    else:
        heavy, bold, light = 0.152, 0.119, 0.072
    if ratio > heavy:
        return 800
    if ratio > bold:
        return 700
    if ratio < light:
        return 300
    return 400


def estimate_italic(mask: np.ndarray) -> float:
    """通过剪切变换后列投影方差最大化估计倾斜角（度）。"""
    h, w = mask.shape[:2]
    if h < 6 or w < 6 or not np.any(mask):
        return 0.0
    best_k, best_score = 0.0, -1.0
    ys, xs = np.nonzero(mask)
    yc = h / 2.0
    for k in np.arange(-0.45, 0.46, 0.05):
        shifted = np.round(xs + k * (ys - yc)).astype(np.int32)
        np.clip(shifted, 0, w - 1, out=shifted)
        hist = np.bincount(shifted, minlength=w).astype(np.float32)
        score = float(np.var(hist))
        if score > best_score:
            best_score, best_k = score, float(k)
    if abs(best_k) < 0.12:
        return 0.0
    return math.degrees(math.atan(best_k))


def estimate_serif(mask: np.ndarray, is_cjk: bool) -> bool:
    if is_cjk or not np.any(mask):
        return False
    dist = cv2.distanceTransform((mask > 0).astype(np.uint8), cv2.DIST_L2, 5)
    vals = dist[mask > 0]
    if vals.size < 20:
        return False
    mean = float(np.mean(vals))
    if mean <= 0:
        return False
    return float(np.std(vals)) / mean > 0.72


def estimate_letter_spacing(chars: Sequence, font_size: float, text: str) -> float:
    """CJK 等宽场景下估算额外字距（tracking）。

    em 直接由字符墨迹宽度自洽推出，不复用基于高度的 font_size，避免高度估计
    误差被放大成夸张的负字距。负值与微小值一律归零：宁可不加字距，也不排歪。
    """
    if not chars or font_size <= 0 or not has_cjk(text):
        return 0.0
    cjk = sorted([c for c in chars if has_cjk(c.text)], key=lambda c: c.bbox[0])
    if len(cjk) < 3:
        return 0.0
    advances = [cjk[i + 1].bbox[0] - cjk[i].bbox[0] for i in range(len(cjk) - 1)]
    advances = [a for a in advances if 0 < a < font_size * 2.4]
    if len(advances) < 2:
        return 0.0
    adv = float(np.median(advances))
    em = float(np.percentile([c.bbox[2] for c in cjk], 75)) / 0.92
    spacing = adv - em
    if spacing < font_size * 0.10:
        return 0.0
    return round(min(spacing, font_size * 1.2), 2)


# --------------------------------------------------------------------------- #
# 主分析入口
# --------------------------------------------------------------------------- #

def analyze_text_region(image_bgr: np.ndarray, line: TextLine) -> Optional[RegionAnalysis]:
    h, w = image_bgr.shape[:2]
    bx, by, bw, bh = line.bbox
    if bw < 2 or bh < 2:
        return None

    pad = max(2.0, bh * 0.25)
    px, py, pw, ph = expand_rect((bx, by, bw, bh), pad, w, h)
    patch = image_bgr[py:py + ph, px:px + pw]
    if patch.size == 0:
        return None

    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _thr, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    ix0 = int(max(0, bx - px - 1))
    iy0 = int(max(0, by - py - 1))
    ix1 = int(min(pw, bx - px + bw + 1))
    iy1 = int(min(ph, by - py + bh + 1))
    inner = np.zeros((ph, pw), np.uint8)
    inner[iy0:iy1, ix0:ix1] = 255

    # 极性判定：与紧邻文字框那一圈颜色更接近的一类是背景
    ring = _surround_median(patch, (ix0, iy0, ix1, iy1))
    ring_gray = float(0.114 * ring[0] + 0.587 * ring[1] + 0.299 * ring[2])
    hi_mask = cv2.bitwise_and(binary, inner)
    lo_mask = cv2.bitwise_and(cv2.bitwise_not(binary), inner)
    hi_area = int(np.count_nonzero(hi_mask))
    lo_area = int(np.count_nonzero(lo_mask))
    hi_mean = float(np.mean(gray[hi_mask > 0])) if hi_area else 0.0
    lo_mean = float(np.mean(gray[lo_mask > 0])) if lo_area else 0.0
    text_mask = lo_mask if abs(hi_mean - ring_gray) < abs(lo_mean - ring_gray) else hi_mask

    # 兜底：文字几乎不可能占满文字框，占比压倒性大的那一类只能是背景。渐变背景、或
    # 文字框外圈混进了别的区块时，上面按颜色判的极性会反过来，这里把它掰正。
    total = hi_area + lo_area
    if total > 0:
        picked_hi = text_mask is hi_mask
        picked_area = hi_area if picked_hi else lo_area
        other_area = lo_area if picked_hi else hi_area
        if picked_area / total > 0.78 and other_area / total > 0.06:
            text_mask = lo_mask if picked_hi else hi_mask

    min_area = max(2.0, (bh * bh) * 0.004)
    text_mask = _drop_tiny_components(text_mask, min_area)
    if not np.any(text_mask):
        return None

    bg_mask = cv2.bitwise_and(cv2.bitwise_not(text_mask), inner)
    if not np.any(bg_mask):
        bg_mask = cv2.bitwise_not(text_mask)

    # 笔画核心色（腐蚀掉抗锯齿边缘）
    core = cv2.erode(text_mask, np.ones((3, 3), np.uint8), iterations=1)
    if not np.any(core):
        core = text_mask
    fg_bgr = _median_color(patch, core)
    bg_bgr = _median_color(patch, bg_mask)
    if not np.any(bg_mask):
        bg_bgr = ring

    diff = fg_bgr - bg_bgr
    contrast = float(np.linalg.norm(diff)) / 441.67  # 归一化到 0~1

    # 线性色彩解混得到软 alpha：p = a*fg + (1-a)*bg
    denom = float(np.dot(diff, diff))
    if denom > 1e-3:
        flat = patch.astype(np.float32).reshape(-1, 3) - bg_bgr
        alpha_f = (flat @ diff) / denom
        alpha = np.clip(alpha_f.reshape(ph, pw), 0.0, 1.0)
        # 用硬掩码抑制背景噪声：仅在墨迹附近保留软 alpha
        near = cv2.dilate(text_mask, np.ones((3, 3), np.uint8), iterations=1)
        alpha = alpha * (near > 0)
        alpha_u8 = (alpha * 255).astype(np.uint8)
    else:
        alpha_u8 = text_mask.copy()

    # 墨迹范围用 alpha 的 55% 等值线，比 Otsu 掩码更贴近真实字形边界
    ink_mask = (alpha_u8 >= 140).astype(np.uint8) * 255
    if not np.any(ink_mask):
        ink_mask = text_mask
    ink = _ink_bbox(ink_mask)
    if ink is None:
        return None
    ink_x, ink_y, ink_w, ink_h = ink

    font_size, baseline = estimate_font_size(line.text, float(ink_h), line.chars)
    char_size = estimate_font_size_from_chars(line.text, line.chars)
    if char_size is not None and char_size > 1.0:
        # 两个估计各有各的失效模式：墨迹高度会被相邻元素/圆圈图标撑大，字符宽度
        # 会被标点与空格撑大。取较小者，两种污染都能被另一路压回来。
        font_size = min(font_size, char_size)
        baseline = min(baseline, ink_h * 1.02)

    is_cjk = has_cjk(line.text)
    stroke = estimate_stroke_width(ink_mask, alpha_u8)
    font_weight = classify_weight(stroke, font_size, is_cjk)
    coverage = float(np.count_nonzero(ink_mask)) / float(max(1, ink_w * ink_h))

    style = TextStyle(
        color=(int(fg_bgr[2]), int(fg_bgr[1]), int(fg_bgr[0])),
        bg_color=(int(bg_bgr[2]), int(bg_bgr[1]), int(bg_bgr[0])),
        bg_uniform=bool(np.std(gray[bg_mask > 0]) < 14) if np.any(bg_mask) else True,
        bg_std=float(np.std(gray[bg_mask > 0])) if np.any(bg_mask) else 0.0,
        contrast=round(contrast, 4),
        font_size=round(font_size, 2),
        font_weight=font_weight,
        stroke_width=round(stroke, 2),
        serif=estimate_serif(ink_mask, is_cjk),
        italic_deg=round(estimate_italic(ink_mask), 2),
        letter_spacing=estimate_letter_spacing(line.chars, font_size, line.text),
        baseline_offset=round(baseline, 2),
        ink_bbox=(float(px + ink_x), float(py + ink_y), float(ink_w), float(ink_h)),
        is_cjk=is_cjk,
        coverage=round(coverage, 4),
        quality=round(_clamp(contrast * 3.0, 0.0, 1.0), 3),
    )

    rgba = np.zeros((ph, pw, 4), dtype=np.uint8)
    rgba[:, :, 0] = int(fg_bgr[2])
    rgba[:, :, 1] = int(fg_bgr[1])
    rgba[:, :, 2] = int(fg_bgr[0])
    rgba[:, :, 3] = alpha_u8
    # 低对比场景保留原始像素色，避免整体被压成单色
    if contrast < 0.12:
        rgb = cv2.cvtColor(patch, cv2.COLOR_BGR2RGB)
        rgba[:, :, :3] = rgb

    return RegionAnalysis(style=style, mask=text_mask, alpha=alpha_u8,
                          patch_rect=(px, py, pw, ph), rgba=rgba)


# --------------------------------------------------------------------------- #
# 段落分组与对齐
# --------------------------------------------------------------------------- #

def group_paragraphs(lines: List[TextLine],
                     styles: List[TextStyle]) -> List[List[int]]:
    """把行按「字号接近 + 水平重叠 + 行距合理」聚成段落，返回索引分组。"""
    order = sorted(range(len(lines)), key=lambda i: (lines[i].bbox[1], lines[i].bbox[0]))
    groups: List[List[int]] = []

    for idx in order:
        x, y, w, h = lines[idx].bbox
        size = styles[idx].font_size
        placed = False
        for group in groups:
            last = group[-1]
            lx, ly, lw, lh = lines[last].bbox
            lsize = styles[last].font_size
            if lsize <= 0 or size <= 0:
                continue
            if abs(size - lsize) / max(size, lsize) > 0.22:
                continue
            gap = y - (ly + lh)
            if not (-lh * 0.4 <= gap <= lh * 1.15):
                continue
            overlap = min(x + w, lx + lw) - max(x, lx)
            if overlap < min(w, lw) * 0.35:
                continue
            group.append(idx)
            placed = True
            break
        if not placed:
            groups.append([idx])
    return groups


def infer_alignment(lines: List[TextLine], group: List[int]) -> str:
    if len(group) < 2:
        return "left"
    lefts = [lines[i].bbox[0] for i in group]
    rights = [lines[i].bbox[0] + lines[i].bbox[2] for i in group]
    centers = [lines[i].bbox[0] + lines[i].bbox[2] / 2 for i in group]
    v_left, v_right, v_center = np.std(lefts), np.std(rights), np.std(centers)
    best = min(v_left, v_right, v_center)
    if best == v_center and v_center < v_left * 0.6 and v_center < v_right * 0.6:
        return "center"
    if best == v_right and v_right < v_left * 0.6:
        return "right"
    return "left"
