"""背景修补：把元素（文字/图形）从画面中"擦掉"，得到干净底图。

对每个待擦除区域按背景复杂度分级处理，从最保真到最通用：
  1. 环形邻域近似纯色      -> 直接填充中位数色（零瑕疵）
  2. 环形邻域近似线性渐变  -> 每通道最小二乘拟合平面后填充（渐变背景不留痕）
  3. 环形邻域平滑无纹理    -> 调和插值（∇²u=0，边界精确匹配，不留暗环与方块接缝）
  4. 其余                  -> 局部 cv2.inpaint（TELEA / NS，能编造纹理）

输入区域统一为 (局部掩码, bbox) 形式，避免为每个元素分配全幅掩码。
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

Rect = Tuple[int, int, int, int]
Region = Tuple[np.ndarray, Rect]


def fill_large_holes(mask: np.ndarray, min_ratio: float = 0.18) -> np.ndarray:
    """填充掩码内部的大孔洞。

    OCR 常把「彩色圆圈 + 里面的数字」当成一个文字行，此时二值化只圈出圆环，擦除
    后会把中间的数字留在画面上。把明显偏大的内部孔洞补上即可整块擦净；「口」「回」
    这类字的小孔洞低于比例阈值，不会被误填。
    """
    total = int(np.count_nonzero(mask))
    if total == 0:
        return mask
    h, w = mask.shape[:2]
    inv = cv2.bitwise_not(mask)
    num, labels, stats, _cent = cv2.connectedComponentsWithStats(inv, connectivity=4)
    out = mask.copy()
    for i in range(1, num):
        x, y, cw, ch = (int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP]),
                        int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT]))
        if x == 0 or y == 0 or x + cw >= w or y + ch >= h:
            continue  # 接触边界，属于外部背景而非孔洞
        if int(stats[i, cv2.CC_STAT_AREA]) >= min_ratio * total:
            out[labels == i] = 255
    return out


def _prepare_region(mask_local: np.ndarray, bbox: Rect,
                    shape: Tuple[int, int], dilate: int) -> Optional[Region]:
    """把局部掩码按需外扩并膨胀，返回新的 (掩码, bbox)。"""
    ih, iw = shape
    x, y, w, h = (int(v) for v in bbox)
    if w <= 0 or h <= 0 or x >= iw or y >= ih:
        return None
    w = min(w, iw - x)
    h = min(h, ih - y)
    if w <= 0 or h <= 0:
        return None
    mask_local = mask_local[:h, :w]

    pad = dilate + 1 if dilate > 0 else 0
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(iw, x + w + pad), min(ih, y + h + pad)
    padded = np.zeros((y1 - y0, x1 - x0), np.uint8)
    padded[y - y0:y - y0 + h, x - x0:x - x0 + w] = (mask_local > 0).astype(np.uint8) * 255
    if dilate > 0:
        padded = cv2.dilate(padded, np.ones((3, 3), np.uint8), iterations=dilate)
    if not np.any(padded):
        return None
    return padded, (x0, y0, x1 - x0, y1 - y0)


def _ring_samples(image: np.ndarray, union: np.ndarray, rect: Rect,
                  pad: int) -> Tuple[np.ndarray, np.ndarray]:
    """区域外扩环带内、且不属于任何待擦除掩码的像素坐标与颜色。"""
    h, w = image.shape[:2]
    x, y, rw, rh = rect
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(w, x + rw + pad), min(h, y + rh + pad)

    sub_img = image[y0:y1, x0:x1]
    sub_union = union[y0:y1, x0:x1]
    ys, xs = np.nonzero(sub_union == 0)
    if ys.size == 0:
        return np.empty((0, 2), np.float32), np.empty((0, 3), np.float32)
    if ys.size > 40000:  # 大区域抽样，避免最小二乘过慢
        idx = np.random.default_rng(7).choice(ys.size, 40000, replace=False)
        ys, xs = ys[idx], xs[idx]
    coords = np.stack([xs + x0, ys + y0], axis=1).astype(np.float32)
    colors = sub_img[ys, xs].astype(np.float32)
    return coords, colors


def _fit_plane(coords: np.ndarray, colors: np.ndarray) -> Tuple[np.ndarray, float]:
    """每通道拟合 c = a*x + b*y + d，返回系数 (3,3) 与最大残差标准差。"""
    n = coords.shape[0]
    design = np.concatenate([coords, np.ones((n, 1), np.float32)], axis=1)
    coeffs, _res, _rank, _sv = np.linalg.lstsq(design, colors, rcond=None)
    resid = colors - design @ coeffs
    return coeffs, float(np.max(np.std(resid, axis=0)))


def _apply_plane(coeffs: np.ndarray, rect: Rect) -> np.ndarray:
    x, y, rw, rh = rect
    gx, gy = np.meshgrid(np.arange(x, x + rw, dtype=np.float32),
                         np.arange(y, y + rh, dtype=np.float32))
    flat = np.stack([gx.ravel(), gy.ravel(), np.ones(gx.size, np.float32)], axis=1)
    vals = flat @ coeffs
    return np.clip(vals.reshape(rh, rw, 3), 0, 255).astype(np.uint8)


def _blend_fill(region: np.ndarray, mask_local: np.ndarray,
                values: np.ndarray, feather: int = 3) -> None:
    """按羽化权重把填充值写进区域，让抗锯齿外圈也一并被盖掉。

    直接 `region[sel] = fill` 只覆盖硬掩码内部，字形外围那圈 alpha 不到一半的抗锯齿像素
    会原样留下。深底亮字的场合这圈恰恰是亮的，于是擦完还能清清楚楚读出原来的字——
    实测「CAGR 22-33%」擦除后就留着一层字形亮影。羽化几像素写入即可根治，
    代价只是把紧邻的两三像素也拉向背景色，而那本来就是同一片背景。

    羽化半径取 3 是实测的性价比拐点：把残影的高频结构强度从 0.98 压到 0.19，
    提亮五倍也读不出字形；再加到 6 只降到 0.08，收益有限而外溢风险变大。
    """
    k = feather * 2 + 1
    grown = cv2.dilate(mask_local, np.ones((3, 3), np.uint8), iterations=feather)
    soft = (cv2.GaussianBlur(grown, (k, k), 0).astype(np.float32) / 255.0)[..., None]
    region[:] = (region.astype(np.float32) * (1.0 - soft)
                 + values.astype(np.float32) * soft).astype(np.uint8)


def build_clean_background(image_bgr: np.ndarray,
                           regions: Sequence[Region],
                           dilate: int = 2,
                           uniform_std: float = 11.0,
                           gradient_std: float = 13.0,
                           texture_std: float = 7.0) -> np.ndarray:
    """擦除所有区域覆盖的内容，返回干净背景（BGR）。

    按面积从小到大处理，让小元素优先享受高质量的纯色/渐变填充。
    """
    h, w = image_bgr.shape[:2]
    prepared: List[Region] = []
    for mask_local, bbox in regions:
        item = _prepare_region(mask_local, bbox, (h, w), dilate)
        if item is not None:
            prepared.append(item)
    if not prepared:
        return image_bgr.copy()

    union = np.zeros((h, w), np.uint8)
    for mask_local, (x, y, rw, rh) in prepared:
        sub = union[y:y + rh, x:x + rw]
        np.maximum(sub, mask_local, out=sub)

    result = image_bgr.copy()
    prepared.sort(key=lambda item: int(np.count_nonzero(item[0])))

    fallback: List[Region] = []
    for mask_local, rect in prepared:
        x, y, rw, rh = rect
        pad = int(max(4, min(28, round(2 * max(rw, rh) ** 0.5))))
        coords, colors = _ring_samples(result, union, rect, pad)
        if coords.shape[0] < 30:
            fallback.append((mask_local, rect))
            continue

        sel = mask_local > 0
        std = float(np.max(np.std(colors, axis=0)))
        if std <= uniform_std:
            fill = np.median(colors, axis=0).astype(np.uint8)
            region = result[y:y + rh, x:x + rw]
            _blend_fill(region, mask_local, np.broadcast_to(fill, region.shape))
            continue

        try:
            coeffs, resid = _fit_plane(coords, colors)
        except np.linalg.LinAlgError:
            fallback.append((mask_local, rect))
            continue

        if resid <= gradient_std:
            plane = _apply_plane(coeffs, rect)
            region = result[y:y + rh, x:x + rw]
            _blend_fill(region, mask_local, plane)
            continue

        # 背景不是平面但依然平滑（光晕、圆角卡片、径向渐变）：调和插值比 inpaint 干净
        if _ring_texture(result, union, rect, pad) <= texture_std:
            span = int(max(8, min(60, 0.4 * max(rw, rh))))
            rx0, ry0 = max(0, x - span), max(0, y - span)
            rx1, ry1 = min(w, x + rw + span), min(h, y + rh + span)
            roi = result[ry0:ry1, rx0:rx1]
            own = np.zeros(roi.shape[:2], bool)
            own[y - ry0:y - ry0 + rh, x - rx0:x - rx0 + rw] = sel
            # ROI 里其它待擦区域也算未知，别拿它们的像素当边界条件
            unknown = own | (union[ry0:ry1, rx0:rx1] > 0)
            filled = _harmonic_fill(roi, unknown)
            sub = roi[y - ry0:y - ry0 + rh, x - rx0:x - rx0 + rw]
            _blend_fill(sub, mask_local,
                        filled[y - ry0:y - ry0 + rh, x - rx0:x - rx0 + rw])
            continue

        fallback.append((mask_local, rect))

    if fallback:
        result = _inpaint_regions(result, fallback)
    return result


def _ring_texture(image_bgr: np.ndarray, union: np.ndarray, rect: Rect, pad: int) -> float:
    """区域四周的高频强度。数值小说明背景平滑（纯色、渐变、光晕），可以用调和插值补。"""
    h, w = image_bgr.shape[:2]
    x, y, rw, rh = rect
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(w, x + rw + pad), min(h, y + rh + pad)
    sub = image_bgr[y0:y1, x0:x1]
    if sub.size == 0:
        return 1e9
    ring = union[y0:y1, x0:x1] == 0
    if np.count_nonzero(ring) < 30:
        return 1e9
    high = cv2.absdiff(sub, cv2.blur(sub, (5, 5)))
    return float(np.mean(high[ring]))


def _harmonic_fill(roi_bgr: np.ndarray, unknown: np.ndarray, max_levels: int = 5) -> np.ndarray:
    """调和插值填补：内部满足 ∇²u=0，边界严格等于四周的真实像素。

    cv2.inpaint 是从边界往里推进的，两股推进锋面在中间相遇会留下一道痕——擦掉一个圆形
    图标后常见的那圈暗环就是它。调和插值没有锋面，边界值又是精确匹配的，所以补完既不留
    环也不留方块接缝，代价是无法编造纹理，只适合平滑背景。

    解法是「模糊一次、把已知像素按原值写回」的迭代（Jacobi），并用金字塔从粗到细加速，
    否则大区域要迭代上千次才能把边界信息传到中心。
    """
    known = ~unknown
    if not np.any(known):
        return roi_bgr.copy()

    imgs = [roi_bgr.astype(np.float32)]
    knowns = [known]
    while len(imgs) < max_levels and min(imgs[-1].shape[:2]) > 12:
        k = knowns[-1].astype(np.float32)
        num = cv2.pyrDown(imgs[-1] * k[..., None])
        den = cv2.pyrDown(k)
        coarse = num / np.maximum(den, 1e-6)[..., None]
        imgs.append(coarse)
        knowns.append(den > 0.35)

    cur = None
    for lvl in range(len(imgs) - 1, -1, -1):
        target, k = imgs[lvl], knowns[lvl]
        if cur is None:
            seed = target[k].mean(axis=0) if np.any(k) else np.zeros(3, np.float32)
            cur = np.broadcast_to(seed, target.shape).astype(np.float32).copy()
        else:
            cur = cv2.resize(cur, (target.shape[1], target.shape[0]), interpolation=cv2.INTER_LINEAR)
        if np.any(k):
            cur[k] = target[k]
            for _ in range(70 if lvl else 40):
                cur = cv2.blur(cur, (3, 3))
                cur[k] = target[k]
    return np.clip(cur, 0, 255).astype(np.uint8)


def _inpaint_regions(image_bgr: np.ndarray, regions: Sequence[Region]) -> np.ndarray:
    """对复杂背景区域做局部 inpaint，只在 ROI 上计算以保证速度。"""
    h, w = image_bgr.shape[:2]
    result = image_bgr.copy()
    for mask_local, (x, y, rw, rh) in regions:
        pad = int(max(8, min(60, 0.35 * max(rw, rh))))
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(w, x + rw + pad), min(h, y + rh + pad)
        roi = result[y0:y1, x0:x1]
        roi_mask = np.zeros(roi.shape[:2], np.uint8)
        roi_mask[y - y0:y - y0 + rh, x - x0:x - x0 + rw] = mask_local
        if not np.any(roi_mask):
            continue
        radius = 3 if max(rw, rh) < 120 else 7
        flags = cv2.INPAINT_TELEA if max(rw, rh) < 240 else cv2.INPAINT_NS
        try:
            patched = cv2.inpaint(roi, roi_mask, radius, flags)
        except cv2.error:
            continue
        keep = roi_mask > 0
        roi[keep] = patched[keep]
    return result
