"""OCR 感知：把截图变成带坐标的文字框。

为什么需要它
------------
淘宝芭芭农场是 Themis/H5 页面，**关键控件不暴露给无障碍服务**（详见 DESIGN.md 13.13）：
- `uiautomator dump` 会崩（dumpNodeRec NPE）；
- uiautomator2 不崩，但读不到底栏「集肥料」和「做任务赚肥料」面板里的任务。
所以这两处只能靠识别截图像素上的文字，再换算成点击坐标。

识别引擎用 RapidOCR（onnxruntime，离线、离线模型随包、中文开箱可用），
单帧 720x1520 实测约 2.5~3 秒。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass
class Box:
    text: str
    score: float
    left: int
    top: int
    right: int
    bottom: int

    @property
    def cx(self) -> int:
        return (self.left + self.right) // 2

    @property
    def cy(self) -> int:
        return (self.top + self.bottom) // 2

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def __repr__(self) -> str:
        return f"<Box {self.text!r} {self.score:.2f} @({self.cx},{self.cy})>"


_engine = None


def engine():
    """惰性加载识别引擎（首次约 1~2 秒，之后复用）。"""
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def read(png: bytes, roi: Optional[tuple[float, float, float, float]] = None,
         scale: float = 1.0, min_score: float = 0.0) -> list[Box]:
    """对一张 PNG 截图做 OCR，返回文字框列表（按 y 再按 x 排序）。

    roi:   (x0, y0, x1, y1) 归一化裁剪区域（0~1），只识别这块。
           只关心"顶部倒计时"或"面板区域"时用它，能把 1.3s 压到 0.2~0.5s。
    scale: 识别前缩小比例（坐标会自动换算回全屏像素）。
    """
    import io

    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    W, H = img.size
    ox, oy = 0, 0
    if roi:
        x0, y0, x1, y1 = roi
        box = (int(W * x0), int(H * y0), int(W * x1), int(H * y1))
        img = img.crop(box)
        ox, oy = box[0], box[1]
    if scale != 1.0:
        img = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))), Image.BILINEAR)

    result, _elapse = engine()(np.array(img))
    boxes: list[Box] = []
    inv = 1.0 / scale
    for item in result or []:
        pts, text, score = item[0], item[1], float(item[2])
        text = (text or "").strip()
        if not text or score < min_score:
            continue
        xs = [p[0] * inv + ox for p in pts]
        ys = [p[1] * inv + oy for p in pts]
        boxes.append(Box(text, score, int(min(xs)), int(min(ys)),
                         int(max(xs)), int(max(ys))))
    boxes.sort(key=lambda b: (b.top, b.left))
    return boxes


def find(boxes: Iterable[Box], needles: Iterable[str],
         min_score: float = 0.55) -> list[Box]:
    """按"包含"匹配文案，返回命中的框（高分的排前面）。"""
    out = []
    for b in boxes:
        if b.score < min_score:
            continue
        for n in needles:
            if n in b.text:
                out.append(b)
                break
    out.sort(key=lambda b: -b.score)
    return out


def find_regex(boxes: Iterable[Box], pattern: str,
               min_score: float = 0.55) -> list[tuple[Box, re.Match]]:
    rx = re.compile(pattern)
    out = []
    for b in boxes:
        if b.score < min_score:
            continue
        m = rx.search(b.text)
        if m:
            out.append((b, m))
    return out


def has_text(boxes: Iterable[Box], needles: Iterable[str], min_score: float = 0.55) -> Optional[str]:
    hits = find(boxes, needles, min_score=min_score)
    return hits[0].text if hits else None


def dump(boxes: Iterable[Box], limit: int = 40) -> str:
    """调试用：把识别结果排成可读文本。"""
    lines = []
    for b in list(boxes)[:limit]:
        lines.append(f"  ({b.left:4d},{b.top:4d},{b.right:4d},{b.bottom:4d}) "
                     f"{b.score:.2f} {b.text}")
    return "\n".join(lines)
