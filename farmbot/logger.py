"""日志：控制台 + 文件双写，格式严格对齐产物要求。

[时间] 阶段X | 动作：xxx | 结果：成功/失败 | 累计观看：N 次 | 累计肥料：约 XXXg
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


class Logger:
    def __init__(self, log_dir: Path, echo: bool = True):
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"{time.strftime('%Y-%m-%d_%H%M%S')}.log"
        self.echo = echo
        self._fh = self.path.open("w", encoding="utf-8")
        self.ads_watched = 0
        self.fert_total = 0

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def raw(self, msg: str) -> None:
        """不带格式的自由文本（调试用）。"""
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        if self.echo:
            print(line, flush=True)
        self._fh.write(line + "\n")
        self._fh.flush()

    def step(self, stage: str, action: str, ok: bool, note: str = "") -> None:
        result = "成功" if ok else "失败"
        line = (
            f"[{time.strftime('%H:%M:%S')}] 阶段{stage} | 动作：{action} | 结果：{result}"
            f" | 累计观看：{self.ads_watched} 次 | 累计肥料：约 {self.fert_total}g"
        )
        if note:
            line += f" | {note}"
        if self.echo:
            print(line, flush=True)
        self._fh.write(line + "\n")
        self._fh.flush()

    def add_ad(self, fert: int = 0) -> None:
        self.ads_watched += 1
        if fert > 0:
            self.fert_total += fert

    def summary(self, stop_reason: str, failures: int) -> str:
        lines = [
            "",
            "=" * 56,
            "任务结束",
            f"  完成广告数量：{self.ads_watched}",
            f"  失败次数：{failures}",
            f"  累计肥料：约 {self.fert_total}g",
            f"  停止原因：{stop_reason}",
            f"  日志文件：{self.path}",
            "=" * 56,
        ]
        text = "\n".join(lines)
        if self.echo:
            print(text, flush=True)
        self._fh.write(text + "\n")
        self._fh.flush()
        return text


def setup_stdout() -> None:
    """Windows 控制台默认 GBK，统一成 UTF-8 避免中文报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
