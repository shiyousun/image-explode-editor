"""字号标定诊断：对照已知真值，输出墨迹高度与字符框尺寸的比例，用于校准系数。"""

from __future__ import annotations

import os
import sys

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

import cv2
import numpy as np

import ocr_engines
import style_infer

# 生成测试图时使用的真实字号
TRUTH = {
    "AI 眼镜行业深度观察": 66,
    "2026 Market Landscape Report": 34,
    "1420": 52, "38.6%": 52, "¥2199": 52,
    "万台出货": 30, "同比增长": 30, "均价下探": 30,
    "消费级 AI 眼镜在 2026 年迎来关键转折点，光波导良率提升": 31,
    "带来整机成本快速下降，头部厂商开始把重心从参数竞赛转向": 31,
    "真实使用场景的打磨，语音助手与拍摄成为最高频的两个入口。": 31,
    "光学显示模组": 32, "端侧大模型": 32, "整机散热结构": 32,
    "Fig.1 供应链结构变化": 26, "查看完整报告": 32,
    "friendsun.ai / 2026": 26,
}


def match_truth(text: str):
    text = text.strip()
    if text in TRUTH:
        return TRUTH[text]
    for key, val in TRUTH.items():
        a = "".join(text.split())
        b = "".join(key.split())
        if a and (a in b or b in a) and abs(len(a) - len(b)) <= 6:
            return val
    return None


def main() -> int:
    sample = os.path.join(PROJECT_DIR, "samples", "sample_infographic.png")
    img = cv2.imread(sample)
    lines, engines = ocr_engines.detect_text(img)
    print(f"引擎 {engines}，检出 {len(lines)} 行\n")

    header = (f"{'文本':26} {'真值':>5} {'inkH':>6} {'chH75':>6} {'chW75':>6} "
              f"{'ink/em':>7} {'chH/em':>7} {'chW/em':>7} {'CJK':>4}")
    print(header)
    print("-" * len(header))

    rows = []
    for line in lines:
        truth = match_truth(line.text)
        res = style_infer.analyze_text_region(img, line)
        if res is None or truth is None:
            continue
        ink_h = res.style.ink_bbox[3]
        cjk = [c for c in line.chars if style_infer.has_cjk(c.text)]
        pool = cjk or [c for c in line.chars if not c.text.isspace()]
        ch_h = float(np.percentile([c.bbox[3] for c in pool], 75)) if pool else 0.0
        ch_w = float(np.percentile([c.bbox[2] for c in pool], 75)) if pool else 0.0
        label = line.text[:24]
        print(f"{label:26} {truth:5d} {ink_h:6.1f} {ch_h:6.1f} {ch_w:6.1f} "
              f"{ink_h / truth:7.3f} {ch_h / truth:7.3f} {ch_w / truth:7.3f} "
              f"{str(bool(cjk)):>4}")
        rows.append((bool(cjk), ink_h / truth, ch_h / truth, ch_w / truth))

    for is_cjk, label in ((True, "CJK"), (False, "拉丁")):
        sub = [r for r in rows if r[0] == is_cjk]
        if not sub:
            continue
        print(f"\n{label} 中位比例  ink/em={np.median([r[1] for r in sub]):.3f} "
              f"chH/em={np.median([r[2] for r in sub]):.3f} "
              f"chW/em={np.median([r[3] for r in sub]):.3f}  (n={len(sub)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
