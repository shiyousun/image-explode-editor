"""端到端冒烟测试：跑一遍炸开流程并输出诊断信息与可视化预览。"""

from __future__ import annotations

import json
import os
import sys
import time

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

import cv2
import numpy as np

import exploder
import ocr_engines


def draw_preview(layout: dict, job_dir: str, out_path: str) -> None:
    base = cv2.imread(os.path.join(job_dir, "base.png"), cv2.IMREAD_COLOR)
    canvas = base.copy()
    palette = {"text": (60, 220, 60), "shape": (255, 140, 40), "image": (60, 120, 255)}
    for layer in layout["layers"]:
        x, y, w, h = (int(layer["x"]), int(layer["y"]), int(layer["w"]), int(layer["h"]))
        color = palette.get(layer["type"], (200, 200, 200))
        cv2.rectangle(canvas, (x, y), (x + w, y + h), color, 2)
    clean_text = cv2.imread(os.path.join(job_dir, "clean_text.png"), cv2.IMREAD_COLOR)
    clean_all = cv2.imread(os.path.join(job_dir, "clean_all.png"), cv2.IMREAD_COLOR)
    combo = np.hstack([canvas, clean_text, clean_all])
    cv2.imwrite(out_path, combo)


def main() -> int:
    sample = os.path.join(PROJECT_DIR, "samples", "sample_infographic.png")
    if not os.path.exists(sample):
        print("缺少测试图，先运行 make_sample.py")
        return 1

    print("可用 OCR 引擎:", ocr_engines.available_engine_names())
    started = time.time()
    layout = exploder.explode(sample, os.path.join(PROJECT_DIR, "workspace"),
                              job_id="a" * 12, strength="standard")
    print(f"耗时 {time.time() - started:.2f}s")
    print("统计:", json.dumps(layout["stats"], ensure_ascii=False))
    print(f"画布 {layout['width']}x{layout['height']}  图层数 {len(layout['layers'])}")

    print("\n--- 文字图层 ---")
    for layer in layout["layers"]:
        if layer["type"] != "text":
            continue
        print(f"  [{layer['id']}] {layer['text'][:26]!r:30} "
              f"size={layer['fontSize']:.1f} w={layer['fontWeight']} "
              f"color={layer['color']} bg={layer['bgColor']} "
              f"ls={layer['letterSpacing']} align={layer['align']} "
              f"conf={layer['confidence']:.2f} cjk={layer['isCJK']}")

    print("\n--- 非文字图层 ---")
    for layer in layout["layers"]:
        if layer["type"] == "text":
            continue
        print(f"  [{layer['id']}] {layer['name']:8} kind={layer['kind']:13} "
              f"box=({layer['x']:.0f},{layer['y']:.0f},{layer['w']:.0f},{layer['h']:.0f}) "
              f"fill={layer.get('fill')} r={layer.get('radius')} meta={layer.get('meta')}")

    job_dir = os.path.join(PROJECT_DIR, "workspace", "a" * 12)
    preview = os.path.join(PROJECT_DIR, "workspace", "smoke_preview.png")
    draw_preview(layout, job_dir, preview)
    print(f"\n预览图（左：图层框 / 右：干净背景）: {preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
