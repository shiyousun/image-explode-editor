"""OCR 引擎抽象层。

提供统一的文字检测 + 识别接口，按可用性自动降级：
  1. macOS Vision (pyobjc)  —— 原生、免模型下载、中英文精度高、支持逐字符包围盒
  2. RapidOCR (onnxruntime) —— 跨平台兜底
  3. Tesseract (CLI)        —— 最后兜底

统一输出结构 TextLine：
  text  识别文本
  quad  四点多边形 [(x,y) * 4]，顺序为 左上/右上/右下/左下，像素坐标
  bbox  轴对齐外接矩形 (x, y, w, h)
  conf  置信度 0~1
  chars 逐字符包围盒列表（可能为空）
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class CharBox:
    text: str
    bbox: Tuple[float, float, float, float]  # x, y, w, h


@dataclass
class TextLine:
    text: str
    quad: List[Tuple[float, float]]
    conf: float
    chars: List[CharBox] = field(default_factory=list)
    source: str = ""

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        xs = [p[0] for p in self.quad]
        ys = [p[1] for p in self.quad]
        return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    @property
    def angle_deg(self) -> float:
        """文本基线相对水平方向的角度（顺时针为正）。"""
        (x0, y0), (x1, y1) = self.quad[0], self.quad[1]
        return math.degrees(math.atan2(y1 - y0, x1 - x0))


# --------------------------------------------------------------------------- #
# 引擎实现
# --------------------------------------------------------------------------- #

class BaseOCR:
    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def detect(self, image_bgr: np.ndarray, scale: float = 1.0) -> List[TextLine]:
        raise NotImplementedError


class MacVisionOCR(BaseOCR):
    """macOS Vision framework。精度高且能给出逐字符包围盒。"""

    name = "macos-vision"

    def __init__(self, languages: Sequence[str] = ("zh-Hans", "en-US")):
        self.languages = list(languages)
        self._mod = None

    def available(self) -> bool:
        if self._mod is not None:
            return True
        try:
            import Vision  # noqa: F401
            import Quartz  # noqa: F401
            from Foundation import NSURL  # noqa: F401

            self._mod = Vision
            return True
        except Exception:
            return False

    def detect(self, image_bgr: np.ndarray, scale: float = 1.0) -> List[TextLine]:
        if not self.available():
            return []

        import Vision
        from Foundation import NSURL

        h, w = image_bgr.shape[:2]
        work = image_bgr
        if scale != 1.0:
            work = cv2.resize(
                image_bgr, (int(round(w * scale)), int(round(h * scale))),
                interpolation=cv2.INTER_CUBIC,
            )

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            cv2.imwrite(tmp_path, work)

            url = NSURL.fileURLWithPath_(tmp_path)
            handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
            request = Vision.VNRecognizeTextRequest.alloc().init()
            # 0 = Accurate, 1 = Fast
            request.setRecognitionLevel_(0)
            request.setUsesLanguageCorrection_(True)
            try:
                request.setRecognitionLanguages_(self.languages)
            except Exception:
                pass
            try:
                request.setMinimumTextHeight_(0.004)
            except Exception:
                pass

            ok, _err = handler.performRequests_error_([request], None)
            if not ok:
                return []
            observations = request.results() or []
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        lines: List[TextLine] = []
        for obs in observations:
            candidates = obs.topCandidates_(1)
            if not candidates or len(candidates) == 0:
                continue
            cand = candidates[0]
            text = str(cand.string() or "")
            if not text.strip():
                continue
            conf = float(cand.confidence())

            quad = self._quad_from_observation(obs, w, h)
            chars = self._char_boxes(cand, text, w, h)
            lines.append(TextLine(text=text, quad=quad, conf=conf,
                                  chars=chars, source=self.name))
        return lines

    # Vision 的归一化坐标原点在左下角，需翻转 y
    @staticmethod
    def _norm_to_px(pt, w: int, h: int) -> Tuple[float, float]:
        return (float(pt.x) * w, (1.0 - float(pt.y)) * h)

    def _quad_from_observation(self, obs, w: int, h: int) -> List[Tuple[float, float]]:
        try:
            tl = self._norm_to_px(obs.topLeft(), w, h)
            tr = self._norm_to_px(obs.topRight(), w, h)
            br = self._norm_to_px(obs.bottomRight(), w, h)
            bl = self._norm_to_px(obs.bottomLeft(), w, h)
            return [tl, tr, br, bl]
        except Exception:
            bb = obs.boundingBox()
            x = float(bb.origin.x) * w
            bw = float(bb.size.width) * w
            bh = float(bb.size.height) * h
            y = (1.0 - float(bb.origin.y) - float(bb.size.height)) * h
            return [(x, y), (x + bw, y), (x + bw, y + bh), (x, y + bh)]

    def _char_boxes(self, cand, text: str, w: int, h: int) -> List[CharBox]:
        """逐字符包围盒。中文按字、英文按字符，用于精确估算字号与字距。"""
        boxes: List[CharBox] = []
        try:
            from Foundation import NSRange
        except Exception:
            return boxes

        for idx, ch in enumerate(text):
            if ch.isspace():
                continue
            try:
                rect_obs, _err = cand.boundingBoxForRange_error_(NSRange(idx, 1), None)
                if rect_obs is None:
                    continue
                tl = self._norm_to_px(rect_obs.topLeft(), w, h)
                tr = self._norm_to_px(rect_obs.topRight(), w, h)
                br = self._norm_to_px(rect_obs.bottomRight(), w, h)
                bl = self._norm_to_px(rect_obs.bottomLeft(), w, h)
                xs = [tl[0], tr[0], br[0], bl[0]]
                ys = [tl[1], tr[1], br[1], bl[1]]
                boxes.append(CharBox(text=ch, bbox=(min(xs), min(ys),
                                                    max(xs) - min(xs),
                                                    max(ys) - min(ys))))
            except Exception:
                continue
        return boxes


class RapidOCREngine(BaseOCR):
    """RapidOCR（PaddleOCR 的 onnxruntime 移植），跨平台兜底。"""

    name = "rapidocr"

    def __init__(self):
        self._engine = None
        self._failed = False

    def available(self) -> bool:
        if self._engine is not None:
            return True
        if self._failed:
            return False
        try:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except Exception:
                from rapidocr import RapidOCR  # 新版包名
            self._engine = RapidOCR()
            return True
        except Exception:
            self._failed = True
            return False

    def detect(self, image_bgr: np.ndarray, scale: float = 1.0) -> List[TextLine]:
        if not self.available():
            return []
        work = image_bgr
        if scale != 1.0:
            h, w = image_bgr.shape[:2]
            work = cv2.resize(image_bgr, (int(round(w * scale)), int(round(h * scale))),
                              interpolation=cv2.INTER_CUBIC)
        try:
            result, _elapse = self._engine(work)
        except Exception:
            return []
        if not result:
            return []

        lines: List[TextLine] = []
        for item in result:
            try:
                pts, text, conf = item[0], item[1], item[2]
            except Exception:
                continue
            if not str(text).strip():
                continue
            quad = [(float(p[0]) / scale, float(p[1]) / scale) for p in pts]
            if len(quad) != 4:
                continue
            lines.append(TextLine(text=str(text), quad=quad,
                                  conf=float(conf), source=self.name))
        return lines


class TesseractOCR(BaseOCR):
    name = "tesseract"

    def available(self) -> bool:
        return shutil.which("tesseract") is not None

    def detect(self, image_bgr: np.ndarray, scale: float = 1.0) -> List[TextLine]:
        if not self.available():
            return []
        tmp_dir = tempfile.mkdtemp()
        img_path = os.path.join(tmp_dir, "in.png")
        cv2.imwrite(img_path, image_bgr)
        try:
            out = subprocess.run(
                ["tesseract", img_path, "stdout", "-l", "chi_sim+eng", "tsv"],
                capture_output=True, text=True, timeout=120,
            )
            lines: List[TextLine] = []
            rows = out.stdout.splitlines()
            if not rows:
                return []
            header = rows[0].split("\t")
            try:
                i_left, i_top = header.index("left"), header.index("top")
                i_w, i_h = header.index("width"), header.index("height")
                i_conf, i_text = header.index("conf"), header.index("text")
            except ValueError:
                return []
            for row in rows[1:]:
                cols = row.split("\t")
                if len(cols) <= i_text:
                    continue
                text = cols[i_text]
                if not text.strip():
                    continue
                try:
                    x, y = float(cols[i_left]), float(cols[i_top])
                    w, h = float(cols[i_w]), float(cols[i_h])
                    conf = max(0.0, float(cols[i_conf])) / 100.0
                except ValueError:
                    continue
                lines.append(TextLine(
                    text=text, conf=conf, source=self.name,
                    quad=[(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
                ))
            return lines
        except Exception:
            return []
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 调度：多引擎 + 多尺度融合
# --------------------------------------------------------------------------- #

_ENGINE_CACHE: dict = {}


def get_engines() -> List[BaseOCR]:
    if "list" not in _ENGINE_CACHE:
        _ENGINE_CACHE["list"] = [MacVisionOCR(), RapidOCREngine(), TesseractOCR()]
    return _ENGINE_CACHE["list"]


def available_engine_names() -> List[str]:
    return [e.name for e in get_engines() if e.available()]


def _iou(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def dedupe_lines(lines: List[TextLine], iou_thresh: float = 0.45) -> List[TextLine]:
    """同一区域的重复检测按置信度择优保留。"""
    ordered = sorted(lines, key=lambda l: (-l.conf, -(l.bbox[2] * l.bbox[3])))
    kept: List[TextLine] = []
    for line in ordered:
        if any(_iou(line.bbox, k.bbox) > iou_thresh for k in kept):
            continue
        kept.append(line)
    # 按阅读顺序（先上后左）返回
    kept.sort(key=lambda l: (round(l.bbox[1] / 8), l.bbox[0]))
    return kept


def detect_text(image_bgr: np.ndarray,
                engine: str = "auto",
                multi_scale: bool = True,
                min_conf: float = 0.25) -> Tuple[List[TextLine], List[str]]:
    """检测图中所有文字行。

    engine: "auto" 用首个可用引擎；也可指定 "macos-vision" / "rapidocr" / "tesseract"
    multi_scale: 对小图额外做一次放大检测，提升小字召回
    返回 (文字行列表, 实际使用的引擎名列表)
    """
    engines = get_engines()
    if engine != "auto":
        engines = [e for e in engines if e.name == engine]

    h, w = image_bgr.shape[:2]
    scales = [1.0]
    if multi_scale:
        long_side = max(h, w)
        if long_side < 1600:
            scales.append(min(2.0, 1800.0 / max(1, long_side)))

    collected: List[TextLine] = []
    used: List[str] = []
    for eng in engines:
        if not eng.available():
            continue
        for sc in scales:
            found = eng.detect(image_bgr, scale=sc)
            if sc != 1.0 and eng.name == MacVisionOCR.name:
                # Vision 在放大图上返回的是归一化坐标，已自动映射回原尺寸
                pass
            collected.extend(f for f in found if f.conf >= min_conf)
        if collected:
            used.append(eng.name)
            break  # 首个有结果的引擎即可，避免风格混杂
    return dedupe_lines(collected), used
