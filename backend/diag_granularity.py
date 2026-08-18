"""分割颗粒度诊断：把当前 segment_elements 的输出画成标注图并列统计。

用法：python backend/diag_granularity.py samples/real_infographic.png [out.png]
"""

from __future__ import annotations

import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import exploder                      # noqa: E402
import ocr_engines                   # noqa: E402
import segmenter                     # noqa: E402


def text_mask_of(analyze: np.ndarray) -> np.ndarray:
    h, w = analyze.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    try:
        lines, _used = ocr_engines.detect_text(analyze, engine="auto")
    except Exception as exc:                                  # noqa: BLE001
        print(f"  (OCR 不可用: {exc})")
        return mask
    for ln in lines:
        x, y, bw, bh = (int(v) for v in ln.bbox)
        mask[max(0, y):y + bh, max(0, x):x + bw] = 255
    return mask


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "samples/real_infographic.png"
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/diag_granularity.png"
    strength = sys.argv[3] if len(sys.argv) > 3 else "standard"

    analyze, _bgra, _scale = exploder.load_image(path)
    h, w = analyze.shape[:2]
    print(f"图片 {path}  分析尺寸 {w}×{h}  strength={strength}")

    tmask = text_mask_of(analyze)
    t0 = time.time()
    els = segmenter.segment_elements(analyze, text_mask=tmask, strength=strength)
    print(f"元素数 {len(els)}   耗时 {time.time() - t0:.2f}s")

    canvas = analyze.copy()
    buckets = {}
    for i, el in enumerate(els):
        x, y, bw, bh = el.bbox
        buckets[el.kind] = buckets.get(el.kind, 0) + 1
        color = {"icon": (0, 220, 0), "image": (0, 150, 255), "rect": (255, 80, 80),
                 "rounded-rect": (255, 140, 0), "ellipse": (255, 0, 255),
                 "line": (120, 120, 120)}.get(el.kind, (255, 255, 255))
        cv2.rectangle(canvas, (x, y), (x + bw, y + bh), color, 2)
        cv2.putText(canvas, str(i), (x + 2, y + 16), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, color, 2)
        print(f"  #{i:<3} {el.kind:<13} {bw:>4}×{bh:<4} "
              f"area={el.area_ratio * 100:5.2f}%  fill={el.meta.get('rectFill'):<5} "
              f"grad={el.meta.get('interiorGrad', '-'):<5} "
              f"contrast={segmenter.surround_contrast(analyze, el):6.1f} "
              f"edge={segmenter.boundary_support(analyze, el):5.2f} "
              f"frame={segmenter.frame_ratio(el):.2f} colors={el.color_count} flat={el.meta.get('flatRatio')}")
    print("按类型:", buckets)
    sizes = sorted(min(e.bbox[2], e.bbox[3]) for e in els)
    print("短边分布:", sizes)
    cv2.imwrite(out, canvas)
    print("标注图 ->", out)


if __name__ == "__main__":
    main()
