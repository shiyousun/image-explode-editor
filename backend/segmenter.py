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


# min_area_px 是最小面积的绝对上限，用来兜住「大图上小图标被按比例的门槛筛掉」：
# 比例门槛在 3000px 宽的图上会涨到 55×55，一个 32px 的图标根本进不了候选池。
# 两者取小，等于「比例说了算，但再严也不能严过这个像素数」。
PRESETS = {
    "conservative": dict(min_area_ratio=0.0018, max_area_ratio=0.62, min_area_px=900,
                         min_side=10, color_levels=8, edge_low=70, edge_high=190,
                         max_elements=40),
    "standard": dict(min_area_ratio=0.0006, max_area_ratio=0.80, min_area_px=420,
                     min_side=8, color_levels=12, edge_low=45, edge_high=140,
                     max_elements=90),
    "fine": dict(min_area_ratio=0.00035, max_area_ratio=0.86, min_area_px=260,
                 min_side=7, color_levels=15, edge_low=35, edge_high=115,
                 max_elements=140),
    "aggressive": dict(min_area_ratio=0.00018, max_area_ratio=0.92, min_area_px=160,
                       min_side=6, color_levels=18, edge_low=25, edge_high=95,
                       max_elements=180),
}


def _min_area(cfg: dict, total: int) -> float:
    return min(cfg["min_area_ratio"] * total, float(cfg.get("min_area_px", 1e9)))

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
    min_area = _min_area(cfg, total)
    max_area = cfg["max_area_ratio"] * total
    min_side = int(cfg.get("min_side", 6))
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
            if cw < min_side or ch < min_side:
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

    # 边缘通道用 RETR_CCOMP 取到「外轮廓 + 它内部的洞」两层，再把洞里的内容单独送进候选池：
    # 卡片/面板的外轮廓会把里面的图标整个圈进去，只取 EXTERNAL 的话，一张卡片上的三个图标
    # 永远只能得到一个候选，也就是友哥说的「颗粒度太大，单个图标改不了」。
    contours, hier = cv2.findContours(edges, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    min_area = _min_area(cfg, total)
    max_area = cfg["max_area_ratio"] * total
    min_side = int(cfg.get("min_side", 6))
    out: List[Candidate] = []
    tree = hier[0] if hier is not None and len(hier) else []
    for idx, cnt in enumerate(contours):
        # CCOMP 的第二层是「洞的边界」，也就是外轮廓自己的内沿，跟外轮廓几乎重合，取了纯属重复；
        # 真正嵌在洞里的物体（卡片里的图标）被 CCOMP 提回第一层，所以只留 parent == -1 的。
        if len(tree) > idx and tree[idx][3] != -1:
            continue
        area = cv2.contourArea(cnt)
        if area < min_area or area > max_area:
            continue
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw < min_side or ch < min_side:
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

    # 平坦像素占比：区分「照片」和「图形」。照片处处是纹理，界面图形则大片纯色，
    # 只在物体边界处有跳变。合并逻辑要靠它判断一个容器里的小块是独立物件还是照片碎屑。
    gray_sub = cv2.cvtColor(sub_img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    grad_sub = cv2.magnitude(cv2.Sobel(gray_sub, cv2.CV_32F, 1, 0, ksize=3),
                             cv2.Sobel(gray_sub, cv2.CV_32F, 0, 1, ksize=3)) / 4.0
    sel_grad = grad_sub[mask_local > 0]
    flat_ratio = float(np.mean(sel_grad < 8.0)) if sel_grad.size else 1.0

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
                         "flatRatio": round(flat_ratio, 3),
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


def _nested_child(big: Element, small: Element, max_ratio: float = 0.55,
                  min_fill: float = 0.34, max_aspect: float = 6.0) -> bool:
    """small 是不是「嵌在 big 里的独立小物体」，而不是 big 被切碎后的一块碎片。

    三条判据缺一不可：

    · 明显小一圈。碎片是把一个物体劈成尺寸相当的几块；子物体只占容器的百分之几。
      之前这里用的是「big 填充率 > 0.88 才算容器」，可实测右侧那块面板量出来是 0.70
      （描边和圆角把填充率拉了下来），于是三个图标全被并进面板，变成一个 248×531 的
      巨块——正是友哥说的「太大了，单个图标改不了」。尺寸比不吃描边和圆角的亏。

    · 子物体自己得像个物件：饱满（掩码填满自己的包围盒）、不细长。这一条同时挡住了
      照片被拆碎的问题——照片里的一切都「嵌在照片里」，只按尺寸判的话一张风景照会被
      切成八十多块天空和树叶，而它本该是一个整体。实测照片里那些量化碎片的填充率
      几乎全在 0.01~0.19（颜色带在照片里是丝絮状的），而 UI 单元是 0.44~0.83
      （图标卡、定位气泡都饱满地占满自己的框），中间是一道干净的空隙。

      （试过用「轮廓是否压在真实的边上」来判，结果正好相反：照片里处处是纹理，
      任何切法的轮廓都有 0.7~0.95 的边吻合度，反而比深色面板上的图标卡（0.09）更高，
      那个指标测的其实是「周围有没有纹理」，不是「这是不是一个物件」。）
    """
    ba = big.bbox[2] * big.bbox[3]
    sa = small.bbox[2] * small.bbox[3]
    if ba <= 0 or sa / ba > max_ratio:
        return False
    sw, sh = small.bbox[2], small.bbox[3]
    if max(sw, sh) / max(1, min(sw, sh)) > max_aspect:
        return False
    return small.meta.get("rectFill", 0.0) >= min_fill


def _protected_nesting(elements: List[Element], max_children: int = 7,
                       size_cv: float = 0.28) -> set:
    """挑出「容器 → 独立子物件」这类不该合并的配对，同时防住照片被拆碎。

    单看一对元素，「面板里的图标」和「镜片上的高光斑」长得一样：都比容器小得多、
    都饱满、都嵌在里面。实测那张眼镜产品图里，镜片反光被切出十几块 fill 0.34~0.53 的
    斑块，逐对判断时和图标卡没有区别（试过颜色数、精确同色占比、轮廓边吻合度、内部
    平坦度，四个指标在两类之间都有重叠，分不开）。

    真正的区别在「一个容器下有几个这样的子块」：界面结构是可数的——面板里三个图标卡、
    卡片里一个图标，实测图形类最多 6 个；而纹理是切不完的，同一张照片里同时冒出
    20、10、10、8 个。所以按数量判：少数几个就是结构，一大把就是纹理。

    例外是图标阵列——十二宫格确实能有十二个子块，但它们尺寸高度一致；照片碎块的尺寸
    从 24×40 到 685×651 参差不齐。所以数量超标时再看尺寸离散度，齐整的照旧保护。
    """
    kids: Dict[int, List[int]] = {}
    for i, big in enumerate(elements):
        for j, small in enumerate(elements):
            if i == j or not _contains(big.bbox, small.bbox):
                continue
            if _nested_child(big, small):
                kids.setdefault(i, []).append(j)

    protected = set()
    for i, lst in kids.items():
        if len(lst) > max_children:
            sides = [math.sqrt(elements[j].bbox[2] * elements[j].bbox[3]) for j in lst]
            mean = sum(sides) / len(sides)
            var = sum((s - mean) ** 2 for s in sides) / len(sides)
            if mean <= 0 or math.sqrt(var) / mean > size_cv:
                continue
        for j in lst:
            protected.add((i, j))
            protected.add((j, i))
    return protected


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
      · 大块包住明显小一圈的元素时不并（见 _nested_child），否则面板会把里面的图标吞掉；
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

        protected = _protected_nesting(current)
        merged_any = False
        for i in range(n):
            for j in range(i + 1, n):
                ei, ej = current[i], current[j]
                if ei.kind in SHAPE_KINDS or ej.kind in SHAPE_KINDS:
                    continue
                if _bbox_iou(ei.bbox, ej.bbox) <= 0.0:
                    continue
                if (i, j) in protected:
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
                 min_side_ratio: float = 0.012, floor: int = 12,
                 long_aspect: float = 8.0) -> List[Element]:
    """丢掉细如发丝的条状候选（分隔线、刻度线、卡片描边）。

    这类元素短边只有几个像素，掩码几乎填满整个包围盒，于是切片里 85% 是它周围的背景色。
    一旦有元素挪到它下面，它就会拿这片不属于自己的暗色糊上去——实测把图标移到分隔线下方，
    图标身上会横着一道暗条，看着像是被切断了。

    它们本来也不是友哥说的「主要素」：选不中、挪了看不出、删了没区别。留在底图里最合适。

    长宽比越夸张，越容不下「其实是个小物件」的可能：20:1 的东西只能是线，不会是图标。
    所以极端细长的（默认 8:1 以上）把厚度门槛放宽一倍，把分隔线、坐标轴、进度条底槽
    这类一并归到底图里去。
    """
    ih, iw = image_bgr.shape[:2]
    limit = max(floor, int(round(min_side_ratio * min(ih, iw))))
    out: List[Element] = []
    for el in elements:
        _x, _y, w, h = el.bbox
        short, long_ = min(w, h), max(w, h)
        aspect = long_ / max(1, short)
        if short < limit and aspect >= 3:
            continue
        if short < limit * 2 and aspect >= long_aspect:
            continue
        out.append(el)
    return out


def surround_contrast(image_bgr: np.ndarray, el: Element, band: int = 6) -> float:
    """元素自身颜色与紧邻外圈颜色的距离，衡量它在画面上「看不看得出来」。

    取掩码内的中位色与外扩一圈的中位色比。用中位数而不是均值，是为了不被元素内部的
    文字或高光带偏。
    """
    ih, iw = image_bgr.shape[:2]
    x, y, w, h = el.bbox
    x0, y0 = max(0, x - band), max(0, y - band)
    x1, y1 = min(iw, x + w + band), min(ih, y + h + band)
    sub = image_bgr[y0:y1, x0:x1]
    full = np.zeros(sub.shape[:2], np.uint8)
    full[y - y0:y - y0 + h, x - x0:x - x0 + w] = el.mask
    outer = cv2.dilate(full, np.ones((band * 2 + 1, band * 2 + 1), np.uint8))
    ring = cv2.bitwise_and(outer, cv2.bitwise_not(full))
    inside = sub[full > 0]
    around = sub[ring > 0]
    if inside.size == 0 or around.size == 0:
        return 999.0
    return float(np.linalg.norm(np.median(inside.astype(np.float32), axis=0)
                                - np.median(around.astype(np.float32), axis=0)))


def boundary_support(image_bgr: np.ndarray, el: Element, grad: Optional[np.ndarray] = None,
                     band: int = 2) -> float:
    """元素轮廓上的平均梯度 ÷ 全图平均梯度，衡量它的边界是不是画面里真实存在的边。

    看得见的物体，边界处必有一道明显的亮度跳变；而颜色量化在平滑渐变里切出来的块，
    边界只是等值线，那里的梯度和周围一样平——这是区分「一块卡片」和「一片背景」的
    最直接证据。
    """
    if grad is None:
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        grad = cv2.magnitude(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3),
                             cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
    ih, iw = image_bgr.shape[:2]
    x, y, w, h = el.bbox
    pad = band + 1
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(iw, x + w + pad), min(ih, y + h + pad)
    full = np.zeros((y1 - y0, x1 - x0), np.uint8)
    full[y - y0:y - y0 + h, x - x0:x - x0 + w] = el.mask
    k = np.ones((band * 2 + 1, band * 2 + 1), np.uint8)
    edge = cv2.bitwise_xor(cv2.dilate(full, k), cv2.erode(full, k))
    sel = grad[y0:y1, x0:x1][edge > 0]
    if sel.size == 0:
        return 0.0
    img_mean = float(np.mean(grad)) or 1.0
    return float(np.mean(sel)) / img_mean


def frame_ratio(el: Element) -> float:
    """墨迹落在包围盒边框附近的比例。接近 1 说明它只是个空心方框。"""
    _x, _y, w, h = el.bbox
    ink = int(np.count_nonzero(el.mask))
    if ink == 0:
        return 0.0
    band = max(3, int(round(0.03 * min(w, h))))
    if w <= band * 2 or h <= band * 2:
        return 1.0
    inner = el.mask[band:h - band, band:w - band]
    return 1.0 - int(np.count_nonzero(inner)) / ink


def drop_invisible_blocks(image_bgr: np.ndarray, elements: List[Element],
                          min_contrast: float = 12.0) -> List[Element]:
    """丢掉与周围颜色几乎一样、根本看不出边界的候选。

    深色渐变背景上，颜色量化会切出一堆规整的矩形，边缘检测也会沿着渐变等值线圈出空框。
    它们在画面上完全隐形：挪走看不出、删掉看不出，只会占着图层列表，还挡住底下真正
    想选的东西。按定义，一个与周围色差可忽略的块，删了不影响画面，所以丢它是安全的。

    真实物体的色差是这个量级之上的：图表曲线 60+，图标卡片 30+，而幽灵矩形只有 2~8。
    """
    out: List[Element] = []
    for el in elements:
        c = surround_contrast(image_bgr, el)
        if c < min_contrast:
            continue
        el.meta["contrast"] = round(c, 1)
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
    elements = drop_invisible_blocks(image_bgr, elements)
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
