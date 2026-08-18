"""非文字元素分割：色块 / 图标 / 照片区域 / 线条，并判定可编辑形状类型。

流程：
  1. 颜色量化 + 边缘检测 双通道生成候选区域；
  2. 剔除与文字重叠、贴满全图、过小过细的候选；
  3. 合并被颜色量化切碎的渐变条带；
  4. 轮廓拟合判定 rect / rounded-rect / ellipse / line / icon / image；
  5. 生成带软 alpha 的 RGBA 切片，并按包含关系推断层级顺序。

掩码一律按元素自身 bbox 尺寸存储（局部掩码），避免大图上几十个全幅掩码把内存吃满。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

Rect = Tuple[int, int, int, int]


@dataclass
class Element:
    kind: str                                    # rect / rounded-rect / ellipse / line / icon / image
    bbox: Rect
    mask: np.ndarray                             # bbox 尺寸的局部掩码 0/255
    fill: Optional[Tuple[int, int, int]] = None  # RGB，仅纯色形状
    radius: float = 0.0
    solid: bool = False
    color_count: int = 0
    area_ratio: float = 0.0
    depth: int = 0
    meta: Dict = field(default_factory=dict)


PRESETS = {
    "conservative": dict(min_area_ratio=0.0018, max_area_ratio=0.62,
                         color_levels=8, edge_low=70, edge_high=190, max_elements=40),
    "standard": dict(min_area_ratio=0.0006, max_area_ratio=0.80,
                     color_levels=12, edge_low=45, edge_high=140, max_elements=90),
    "aggressive": dict(min_area_ratio=0.00018, max_area_ratio=0.92,
                       color_levels=18, edge_low=25, edge_high=95, max_elements=180),
}

SHAPE_KINDS = ("rect", "rounded-rect", "ellipse", "line")


# --------------------------------------------------------------------------- #
# 颜色量化
# --------------------------------------------------------------------------- #

def quantize_colors(image_bgr: np.ndarray, levels: int) -> Tuple[np.ndarray, np.ndarray]:
    """K-means 颜色量化，返回 (标签图, 调色板 BGR)。"""
    h, w = image_bgr.shape[:2]
    scale = min(1.0, 480.0 / max(h, w))
    small = (cv2.resize(image_bgr, (max(1, int(w * scale)), max(1, int(h * scale))),
                        interpolation=cv2.INTER_AREA) if scale < 1.0 else image_bgr)
    data = small.reshape(-1, 3).astype(np.float32)
    levels = int(max(2, min(levels, 32)))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 12, 1.5)
    _ret, _labels, centers = cv2.kmeans(data, levels, None, criteria, 2,
                                        cv2.KMEANS_PP_CENTERS)
    centers = centers.astype(np.float32)

    full = image_bgr.reshape(-1, 3).astype(np.float32)
    labels = np.empty(full.shape[0], dtype=np.int32)
    step = 200000
    for start in range(0, full.shape[0], step):
        chunk = full[start:start + step]
        dists = ((chunk[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        labels[start:start + step] = np.argmin(dists, axis=1)
    return labels.reshape(h, w), centers


def _dominant_background(labels: np.ndarray) -> int:
    """取图像四边出现最多的标签作为整图背景标签。"""
    ring = np.concatenate([
        labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1],
        labels[1, :], labels[-2, :], labels[:, 1], labels[:, -2],
    ])
    counts = np.bincount(ring, minlength=int(labels.max()) + 1)
    return int(np.argmax(counts))


# --------------------------------------------------------------------------- #
# 候选生成
# --------------------------------------------------------------------------- #

Candidate = Tuple[np.ndarray, Rect, Optional[Tuple[int, int, int]]]


def _candidates_from_quantization(labels: np.ndarray, centers: np.ndarray,
                                  bg_label: int, cfg: dict) -> List[Candidate]:
    h, w = labels.shape
    total = h * w
    min_area = cfg["min_area_ratio"] * total
    max_area = cfg["max_area_ratio"] * total
    out: List[Candidate] = []
    kernel = np.ones((3, 3), np.uint8)

    for label in range(int(labels.max()) + 1):
        if label == bg_label:
            continue
        band = (labels == label).astype(np.uint8) * 255
        if np.count_nonzero(band) < min_area:
            continue
        band = cv2.morphologyEx(band, cv2.MORPH_CLOSE, kernel)
        num, comp, stats, _cent = cv2.connectedComponentsWithStats(band, connectivity=8)
        for i in range(1, num):
            area = stats[i, cv2.CC_STAT_AREA]
            if area < min_area or area > max_area:
                continue
            cw, ch = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
            if cw < 6 or ch < 6:
                continue
            cx, cy = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP])
            local = (comp[cy:cy + ch, cx:cx + cw] == i).astype(np.uint8) * 255
            bgr = centers[label]
            out.append((local, (cx, cy, cw, ch),
                        (int(bgr[2]), int(bgr[1]), int(bgr[0]))))
    return out


def _candidates_from_edges(image_bgr: np.ndarray, cfg: dict) -> List[Candidate]:
    h, w = image_bgr.shape[:2]
    total = h * w
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 7, 45, 45)
    edges = cv2.Canny(gray, cfg["edge_low"], cfg["edge_high"])
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))

    contours, _hier = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = cfg["min_area_ratio"] * total
    max_area = cfg["max_area_ratio"] * total
    out: List[Candidate] = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area or area > max_area:
            continue
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw < 8 or ch < 8:
            continue
        local = np.zeros((ch, cw), np.uint8)
        cv2.drawContours(local, [cnt - np.array([[x, y]])], -1, 255, thickness=cv2.FILLED)
        out.append((local, (x, y, cw, ch), None))
    return out


# --------------------------------------------------------------------------- #
# 形状判定
# --------------------------------------------------------------------------- #

def _corner_radius(mask_local: np.ndarray) -> float:
    """沿四角对角线探测圆弧起点，反推圆角半径。"""
    h, w = mask_local.shape[:2]
    sub = mask_local > 0
    limit = int(min(w, h) * 0.5)
    if limit < 2:
        return 0.0
    dists = []
    corners = [(0, 0, 1, 1), (w - 1, 0, -1, 1), (0, h - 1, 1, -1), (w - 1, h - 1, -1, -1)]
    for cx, cy, dx, dy in corners:
        found = limit
        for step in range(limit):
            px, py = cx + dx * step, cy + dy * step
            if 0 <= py < h and 0 <= px < w and sub[py, px]:
                found = step
                break
        dists.append(found)
    d = float(np.median(dists))
    if d < 1.2:
        return 0.0
    return round(min(d * 2.414, min(w, h) / 2.0), 1)


def classify_element(image_bgr: np.ndarray, mask_local: np.ndarray, bbox: Rect,
                     fill_hint: Optional[Tuple[int, int, int]],
                     text_guard: Optional[np.ndarray] = None) -> Optional[Element]:
    ih, iw = image_bgr.shape[:2]
    x0, y0, bw, bh = bbox
    if bw < 2 or bh < 2:
        return None
    area = float(np.count_nonzero(mask_local))
    if area < 1:
        return None
    rect_fill = area / float(bw * bh)

    sub_img = image_bgr[y0:y0 + bh, x0:x0 + bw]

    # 判定纯色时排除文字像素，否则「红底白字按钮」会因为白字被误判成图像
    eval_mask = mask_local
    if text_guard is not None:
        guard = text_guard[y0:y0 + bh, x0:x0 + bw]
        without_text = cv2.bitwise_and(mask_local, cv2.bitwise_not(guard))
        if np.count_nonzero(without_text) >= 0.25 * area:
            eval_mask = without_text

    pixels = sub_img[eval_mask > 0].astype(np.float32)
    color_std = float(np.max(np.std(pixels, axis=0))) if pixels.size else 0.0
    solid = color_std < 13.0
    median_rgb = None
    if pixels.size:
        med = np.median(pixels, axis=0)
        median_rgb = (int(med[2]), int(med[1]), int(med[0]))
    fill = median_rgb if solid else None
    if solid and fill_hint and median_rgb is None:
        fill = fill_hint

    quantized = (pixels // 24).astype(np.int32) if pixels.size else np.empty((0, 3), np.int32)
    color_count = int(len(np.unique(quantized, axis=0))) if quantized.size else 0

    contours, _ = cv2.findContours(mask_local, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    kind = "image"
    radius = 0.0
    circularity = 0.0
    vertices = 0
    if contours:
        cnt = max(contours, key=cv2.contourArea)
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.022 * peri, True)
        vertices = len(approx)
        cnt_area = cv2.contourArea(cnt)
        circularity = (4 * math.pi * cnt_area / (peri * peri)) if peri > 0 else 0.0
        thin = min(bw, bh) <= 8 and max(bw, bh) >= min(bw, bh) * 5

        # 判定顺序很关键：圆形的矩形填充率恰为 π/4≈0.785，会落进圆角矩形的区间
        if thin and rect_fill > 0.72:
            kind = "line"
        elif solid and circularity > 0.78 and 0.70 <= rect_fill <= 0.86 \
                and 0.7 <= bw / max(1, bh) <= 1.43:
            kind = "ellipse"
        elif solid and rect_fill > 0.93:
            radius = _corner_radius(mask_local)
            kind = "rounded-rect" if radius > 2.0 else "rect"
        elif solid and rect_fill > 0.74 and 4 <= vertices <= 12:
            radius = _corner_radius(mask_local)
            kind = "rounded-rect" if radius > 2.0 else "rect"
        elif color_count <= 24 and max(bw, bh) <= max(ih, iw) * 0.30:
            kind = "icon"
        else:
            kind = "image"

    return Element(kind=kind, bbox=bbox, mask=mask_local, fill=fill, radius=radius,
                   solid=solid, color_count=color_count,
                   area_ratio=area / float(ih * iw),
                   meta={"rectFill": round(rect_fill, 3),
                         "colorStd": round(color_std, 2),
                         "circularity": round(circularity, 3),
                         "vertices": vertices})


# --------------------------------------------------------------------------- #
# 条带合并（修复颜色量化把渐变切碎的问题）
# --------------------------------------------------------------------------- #

def _mean_color(image_bgr: np.ndarray, mask_local: np.ndarray, bbox: Rect) -> np.ndarray:
    x, y, w, h = bbox
    sub = image_bgr[y:y + h, x:x + w]
    sel = sub[mask_local > 0]
    if sel.size == 0:
        return np.zeros(3, np.float32)
    return np.mean(sel.astype(np.float32), axis=0)


def _adjacent_bands(a: Rect, b: Rect, tol: int) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    # 左右边缘对齐 + 上下相接 -> 水平条带
    if abs(ax - bx) <= tol and abs((ax + aw) - (bx + bw)) <= tol:
        gap = max(ay, by) - min(ay + ah, by + bh)
        if gap <= tol:
            return True
    # 上下边缘对齐 + 左右相接 -> 垂直条带
    if abs(ay - by) <= tol and abs((ay + ah) - (by + bh)) <= tol:
        gap = max(ax, bx) - min(ax + aw, bx + bw)
        if gap <= tol:
            return True
    return False


def _union_elements(image_bgr: np.ndarray, group: List[Element]) -> Rect:
    xs = [e.bbox[0] for e in group]
    ys = [e.bbox[1] for e in group]
    xe = [e.bbox[0] + e.bbox[2] for e in group]
    ye = [e.bbox[1] + e.bbox[3] for e in group]
    x, y = min(xs), min(ys)
    return (x, y, max(xe) - x, max(ye) - y)


def merge_gradient_bands(image_bgr: np.ndarray, elements: List[Element],
                         text_guard: Optional[np.ndarray] = None,
                         color_tol: float = 105.0,
                         max_rounds: int = 4) -> List[Element]:
    """把边缘对齐且相接、颜色相近的条带合并成整块，避免渐变区被切成一堆碎片。"""
    current = list(elements)
    for _round in range(max_rounds):
        n = len(current)
        if n < 2:
            break
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        colors = [_mean_color(image_bgr, e.mask, e.bbox) for e in current]
        merged_any = False
        for i in range(n):
            for j in range(i + 1, n):
                ei, ej = current[i], current[j]
                if ei.kind == "line" or ej.kind == "line":
                    continue
                tol = max(3, int(0.02 * max(ei.bbox[2], ei.bbox[3],
                                            ej.bbox[2], ej.bbox[3])))
                if not _adjacent_bands(ei.bbox, ej.bbox, tol):
                    continue
                if float(np.linalg.norm(colors[i] - colors[j])) > color_tol:
                    continue
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
                    merged_any = True
        if not merged_any:
            break

        buckets: Dict[int, List[Element]] = {}
        for i, el in enumerate(current):
            buckets.setdefault(find(i), []).append(el)

        rebuilt: List[Element] = []
        for group in buckets.values():
            if len(group) == 1:
                rebuilt.append(group[0])
                continue
            bbox = _union_elements(image_bgr, group)
            gx, gy, gw, gh = bbox
            local = np.zeros((gh, gw), np.uint8)
            for el in group:
                ex, ey, ew, eh = el.bbox
                sub = local[ey - gy:ey - gy + eh, ex - gx:ex - gx + ew]
                np.maximum(sub, el.mask, out=sub)
            local = cv2.morphologyEx(local, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
            el_new = classify_element(image_bgr, local, bbox, None, text_guard)
            rebuilt.append(el_new if el_new else group[0])
        current = rebuilt
    return current


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def _text_overlap(mask_local: np.ndarray, bbox: Rect,
                  text_guard: np.ndarray) -> float:
    x, y, w, h = bbox
    guard = text_guard[y:y + h, x:x + w]
    own = np.count_nonzero(mask_local)
    if own == 0:
        return 0.0
    inter = np.count_nonzero(cv2.bitwise_and(mask_local, guard))
    return inter / own


def _bbox_iou(a: Rect, b: Rect) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    return inter / (aw * ah + bw * bh - inter)


def _contains(outer: Rect, inner: Rect) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ox <= ix and oy <= iy and ox + ow >= ix + iw and oy + oh >= iy + ih


def _mask_overlap(a: Element, b: Element) -> Tuple[float, int]:
    """两个元素掩码的交集面积 ÷ 较小者面积，同时返回较小者的面积。"""
    ax, ay, aw, ah = a.bbox
    bx, by, bw, bh = b.bbox
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0, 0
    sa = a.mask[y1 - ay:y2 - ay, x1 - ax:x2 - ax]
    sb = b.mask[y1 - by:y2 - by, x1 - bx:x2 - bx]
    inter = int(np.count_nonzero(cv2.bitwise_and(sa, sb)))
    smaller = min(int(np.count_nonzero(a.mask)), int(np.count_nonzero(b.mask)))
    if smaller == 0:
        return 0.0, 0
    return inter / smaller, smaller


def consolidate_fragments(image_bgr: np.ndarray, elements: List[Element],
                          text_guard: Optional[np.ndarray] = None,
                          overlap_thresh: float = 0.34,
                          max_union_ratio: float = 0.58,
                          max_rounds: int = 3) -> List[Element]:
    """把同一个视觉物体的碎片合并成一个「主要素」。

    颜色量化和边缘检测两条通道会对同一个物体各生成一批候选，彼此大面积重叠：一条带光晕的
    图表曲线能碎成十来层稀疏的大 bbox。按 bbox 的 IoU 去重抓不住这种情况——它们的外接框
    错开，IoU 不高，但掩码其实压在一起。碎片化的直接后果是「所见非所得」：拖走一层只带走
    物体的一部分，删掉一层剩下的部分还在，看着就像没删掉。

    判定用掩码交集占较小者的比例，也就是「它们是否真的共用同一批墨迹像素」。
    三条护栏防止过度合并：
      · 干净的可编辑图元（矩形/圆角矩形/椭圆/线）一律不并——并了就没法改填充色和圆角了；
      · 大的实心块包住小元素时不并，否则面板会把里面的图标吞掉；
      · 合并结果超过整图 58% 时不并，否则背景会把前景全吃进去，反而更不好用。
    """
    current = list(elements)
    ih, iw = image_bgr.shape[:2]
    total = float(ih * iw)

    for _round in range(max_rounds):
        n = len(current)
        if n < 2:
            break
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        merged_any = False
        for i in range(n):
            for j in range(i + 1, n):
                ei, ej = current[i], current[j]
                if ei.kind in SHAPE_KINDS or ej.kind in SHAPE_KINDS:
                    continue
                if _bbox_iou(ei.bbox, ej.bbox) <= 0.0:
                    continue

                big, small = (ei, ej) if ei.area_ratio >= ej.area_ratio else (ej, ei)
                if _contains(big.bbox, small.bbox) and big.meta.get("rectFill", 0) > 0.88:
                    continue

                ratio, _ = _mask_overlap(ei, ej)
                if ratio < overlap_thresh:
                    continue

                ux, uy, uw, uh = _union_elements(image_bgr, [ei, ej])
                if uw * uh > max_union_ratio * total:
                    continue

                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
                    merged_any = True
        if not merged_any:
            break

        buckets: Dict[int, List[Element]] = {}
        for i, el in enumerate(current):
            buckets.setdefault(find(i), []).append(el)

        rebuilt: List[Element] = []
        for group in buckets.values():
            if len(group) == 1:
                rebuilt.append(group[0])
                continue
            bbox = _union_elements(image_bgr, group)
            gx, gy, gw, gh = bbox
            local = np.zeros((gh, gw), np.uint8)
            for el in group:
                ex, ey, ew, eh = el.bbox
                sub = local[ey - gy:ey - gy + eh, ex - gx:ex - gx + ew]
                np.maximum(sub, el.mask, out=sub)
            el_new = classify_element(image_bgr, local, bbox, None, text_guard)
            rebuilt.append(el_new if el_new else group[0])
        current = rebuilt
    return current


def drop_slivers(image_bgr: np.ndarray, elements: List[Element],
                 min_side_ratio: float = 0.012, floor: int = 12) -> List[Element]:
    """丢掉细如发丝的条状候选（分隔线、刻度线、卡片描边）。

    这类元素短边只有几个像素，掩码几乎填满整个包围盒，于是切片里 85% 是它周围的背景色。
    一旦有元素挪到它下面，它就会拿这片不属于自己的暗色糊上去——实测把图标移到分隔线下方，
    图标身上会横着一道暗条，看着像是被切断了。

    它们本来也不是友哥说的「主要素」：选不中、挪了看不出、删了没区别。留在底图里最合适。
    """
    ih, iw = image_bgr.shape[:2]
    limit = max(floor, int(round(min_side_ratio * min(ih, iw))))
    out: List[Element] = []
    for el in elements:
        _x, _y, w, h = el.bbox
        if min(w, h) < limit and max(w, h) >= 3 * min(w, h):
            continue
        out.append(el)
    return out


def drop_background_crops(image_bgr: np.ndarray, elements: List[Element],
                          min_bbox_ratio: float = 0.015,
                          max_fill: float = 0.55,
                          smooth_ratio: float = 0.5) -> List[Element]:
    """丢掉「大而稀疏、内部又平滑」的候选——它们不是物体，只是背景渐变里切出来的一块。

    深色渐变背景配光晕的信息图上，颜色量化和边缘检测会在同一片平滑区域里切出七八个
    互相穿插的大矩形。它们的墨迹彼此几乎不重叠（交集只占较小者的 6%~10%），所以合并
    规则抓不到；但每一个单独拿出来都毫无意义——选不中、拖不动、删了也看不出区别，
    只会把图层列表撑长，还让人误以为「删了东西还在」。

    判据是内部梯度：拿它跟整图平均梯度比。实测这些裁片全落在 0.06~0.23 倍（内部一片平滑），
    而真正有内容的图表曲线是 7.9 倍、照片主体是 5.4 倍，两类之间是一个数量级的空隙，
    所以阈值取在中间很安全。曾经试过用剪影的边缘支撑度，结果正好相反——裁片的稀疏掩码
    在膨胀取轮廓带时会大量压到里面的文字和曲线上，反而算得比真实图标还高。

    被丢掉的部分仍完整留在底图里，画面不受影响，只是不能单独编辑；真要编辑可以用「抠取」
    手动框出来。
    """
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mag = cv2.magnitude(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3),
                        cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
    img_mean = float(np.mean(mag)) or 1.0
    ih, iw = image_bgr.shape[:2]
    total = float(ih * iw)
    kernel = np.ones((3, 3), np.uint8)

    out: List[Element] = []
    for el in elements:
        x, y, w, h = el.bbox
        fill = el.meta.get("rectFill", 1.0)
        if el.kind in SHAPE_KINDS or (w * h) / total <= min_bbox_ratio or fill >= max_fill:
            out.append(el)
            continue
        inside = cv2.erode(el.mask, kernel, iterations=2)
        if np.count_nonzero(inside) == 0:
            continue
        interior = float(np.mean(mag[y:y + h, x:x + w][inside > 0]))
        if interior / img_mean < smooth_ratio:
            continue
        el.meta["interiorGrad"] = round(interior / img_mean, 2)
        out.append(el)
    return out


def _dedupe(elements: List[Element], iou_thresh: float = 0.72) -> List[Element]:
    ordered = sorted(elements, key=lambda e: -(e.bbox[2] * e.bbox[3]))
    kept: List[Element] = []
    priority = {"rect": 3, "rounded-rect": 3, "ellipse": 3, "line": 2, "icon": 1, "image": 0}
    for el in ordered:
        dup_index = None
        for i, k in enumerate(kept):
            if _bbox_iou(el.bbox, k.bbox) > iou_thresh:
                dup_index = i
                break
        if dup_index is None:
            kept.append(el)
        elif priority.get(el.kind, 0) > priority.get(kept[dup_index].kind, 0):
            kept[dup_index] = el
    return kept


def segment_elements(image_bgr: np.ndarray,
                     text_mask: Optional[np.ndarray] = None,
                     strength: str = "standard",
                     detect_shapes: bool = True) -> List[Element]:
    cfg = PRESETS.get(strength, PRESETS["standard"])
    h, w = image_bgr.shape[:2]
    if text_mask is None:
        text_mask = np.zeros((h, w), np.uint8)
    text_guard = cv2.dilate(text_mask, np.ones((5, 5), np.uint8), iterations=1)

    labels, centers = quantize_colors(image_bgr, cfg["color_levels"])
    bg_label = _dominant_background(labels)

    raw: List[Candidate] = []
    if detect_shapes:
        raw.extend(_candidates_from_quantization(labels, centers, bg_label, cfg))
    raw.extend(_candidates_from_edges(image_bgr, cfg))

    elements: List[Element] = []
    for mask_local, bbox, fill_hint in raw:
        if _text_overlap(mask_local, bbox, text_guard) > 0.42:
            continue
        el = classify_element(image_bgr, mask_local, bbox, fill_hint, text_guard)
        if el is None:
            continue
        bx, by, bw, bh = el.bbox
        # 贴满整图三边以上的巨大区域视为整体背景，不作为元素
        touches = (bx <= 1) + (by <= 1) + (bx + bw >= w - 1) + (by + bh >= h - 1)
        if touches >= 3 and el.area_ratio > 0.45:
            continue
        elements.append(el)

    elements = merge_gradient_bands(image_bgr, elements, text_guard)
    elements = [e for e in elements
                if not ((e.bbox[0] <= 1) + (e.bbox[1] <= 1)
                        + (e.bbox[0] + e.bbox[2] >= w - 1)
                        + (e.bbox[1] + e.bbox[3] >= h - 1) >= 4
                        and e.area_ratio > 0.55)]
    elements = _dedupe(elements)
    elements = consolidate_fragments(image_bgr, elements, text_guard)
    elements = drop_background_crops(image_bgr, elements)
    elements = drop_slivers(image_bgr, elements)
    elements = _dedupe(elements, iou_thresh=0.55)
    elements.sort(key=lambda e: -(e.bbox[2] * e.bbox[3]))
    elements = elements[: cfg["max_elements"]]

    for i, el in enumerate(elements):
        el.depth = sum(1 for j, other in enumerate(elements)
                       if j != i and _contains(other.bbox, el.bbox))
    elements.sort(key=lambda e: (e.depth, -(e.bbox[2] * e.bbox[3])))
    return elements


# --------------------------------------------------------------------------- #
# 切片生成 + 手动抠图
# --------------------------------------------------------------------------- #

def cutout_rgba(image_bgr: np.ndarray, mask_local: np.ndarray,
                bbox: Rect, feather: float = 0.8) -> np.ndarray:
    x, y, w, h = bbox
    sub = image_bgr[y:y + h, x:x + w]
    alpha = mask_local.astype(np.float32)
    if feather > 0:
        k = int(feather * 2) * 2 + 1
        alpha = cv2.GaussianBlur(alpha, (k, k), 0)
    rgba = np.zeros((h, w, 4), np.uint8)
    rgba[:, :, :3] = cv2.cvtColor(sub, cv2.COLOR_BGR2RGB)
    rgba[:, :, 3] = np.clip(alpha, 0, 255).astype(np.uint8)
    return rgba


def grabcut_cutout(image_bgr: np.ndarray, rect: Rect,
                   iterations: int = 5) -> Tuple[np.ndarray, Rect]:
    """用户框选区域的前景抠图，返回 (局部掩码, 收紧后的 bbox)。"""
    h, w = image_bgr.shape[:2]
    x, y, rw, rh = rect
    x = int(max(0, min(x, w - 2)))
    y = int(max(0, min(y, h - 2)))
    rw = int(max(2, min(rw, w - x)))
    rh = int(max(2, min(rh, h - y)))

    mask = np.zeros((h, w), np.uint8)
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(image_bgr, mask, (x, y, rw, rh), bgd, fgd, iterations,
                    cv2.GC_INIT_WITH_RECT)
        binary = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD),
                          255, 0).astype(np.uint8)
    except cv2.error:
        binary = np.zeros((h, w), np.uint8)
        binary[y:y + rh, x:x + rw] = 255

    if np.count_nonzero(binary) < 0.03 * rw * rh:
        binary = np.zeros((h, w), np.uint8)
        binary[y:y + rh, x:x + rw] = 255

    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    ys, xs = np.nonzero(binary)
    if ys.size == 0:
        return binary[y:y + rh, x:x + rw], (x, y, rw, rh)
    tx, ty = int(xs.min()), int(ys.min())
    tw, th = int(xs.max() - tx + 1), int(ys.max() - ty + 1)
    return binary[ty:ty + th, tx:tx + tw].copy(), (tx, ty, tw, th)
