"""感知层：把一次"看屏幕"封装成 Frame。

同时拿两样东西：
  - UI 树（文本 + 精确边界）→ 用来定位元素、读倒计时、读肥料值
  - 截图（PNG 字节）        → 用来判断画面是否还在动、留证排查

两个指纹分别对应两种"静止"：
  ui_sig  UI 结构没变     （视频在播但界面不动时会不变，属正常）
  shot_sig 整屏像素没变   （连视频都卡住了 → 异常）
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import ui
from .adb import Adb, AdbError, md5


@dataclass
class Frame:
    nodes: list[ui.Node]
    root: ui.Node
    xml: str
    screenshot: bytes
    ts: float
    ui_sig: str
    shot_sig: str
    shot_path: Optional[Path] = None

    def find(self, needles, exact: bool = False) -> list[ui.Node]:
        return ui.by_text(self.nodes, needles, exact=exact)

    def find_regex(self, pattern: str):
        return ui.by_regex(self.nodes, pattern)

    def has(self, needles) -> Optional[str]:
        return ui.has_any(self.nodes, needles)

    @property
    def screen_w(self) -> int:
        return ui.screen_box(self.nodes)[0] or 1080

    @property
    def screen_h(self) -> int:
        return ui.screen_box(self.nodes)[1] or 2400


class Perceiver:
    # dump 失败熔断窗口（秒）：某些页面会持续动画，导致 dump 必然崩溃，
    # 这不是偶发抖动，反复重试只会白等。命中后短时间内直接快速失败。
    DUMP_DOWN_SECONDS = 20.0

    def __init__(self, adb: Adb, shot_dir: Optional[Path] = None, dumper=None):
        self.adb = adb
        self.shot_dir = shot_dir
        self.shot_count = 0
        self._dump_down_until = 0.0
        self._dump_err = ""
        # dumper: 可选的外部感知后端（目前是 u2sense.U2Dumper）。
        # 有就用它抓树（不会因 WebView 动画崩进程），失败再回退 adb。
        self.dumper = dumper
        self.backend = getattr(dumper, "name", "adb-uiautomator") if dumper else "adb-uiautomator"

    def _dump(self) -> str:
        if self.dumper is not None:
            try:
                return self.dumper.dump_xml()
            except Exception as e:      # noqa: BLE001 - 任何后端异常都要能回退
                self._dump_err = f"u2 后端抓取失败：{e}"
                # 不要静默失败，回退到 adb 再试一次
        return self.adb.dump_xml()

    def grab(self, save_shot: bool = False, tag: str = "") -> Frame:
        # 先截图再 dump：dump 会阻塞 UI 约 0.5~1s，截图放前面更接近"当前"画面
        shot = self.adb.screencap()
        if time.time() < self._dump_down_until:
            # 熔断期内直接失败，但把上一次的真实原因带出去，方便排查
            raise AdbError(self._dump_err or "界面树暂不可用（dump 熔断中）")
        try:
            xml = self._dump()
        except AdbError as e:
            self._dump_down_until = time.time() + self.DUMP_DOWN_SECONDS
            self._dump_err = str(e)
            raise
        root, nodes = ui.parse(xml)
        path = None
        if save_shot and self.shot_dir is not None:
            self.shot_count += 1
            stamp = time.strftime("%H%M%S")
            name = f"{stamp}_{self.shot_count:04d}{('_' + tag) if tag else ''}.png"
            path = self.shot_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(shot)
        return Frame(
            nodes=nodes,
            root=root,
            xml=xml,
            screenshot=shot,
            ts=time.time(),
            ui_sig=md5(xml.encode("utf-8")),
            shot_sig=md5(shot),
            shot_path=path,
        )

    def grab_quiet(self, tag: str = "", retries: int = 2) -> Frame:
        """带重试的抓取，界面树偶尔会 dump 失败。"""
        last: Optional[Exception] = None
        for i in range(retries):
            try:
                return self.grab(save_shot=False, tag=tag)
            except AdbError as e:
                last = e
                if i + 1 < retries and time.time() >= self._dump_down_until:
                    time.sleep(0.8)
                    continue
                break
        raise AdbError(f"抓取失败：{last}")

    def wait_ui_change(self, prev_sig: str, timeout: float = 6.0, interval: float = 0.5) -> Frame:
        """等界面发生变化，返回变化后的帧；超时返回最后一帧。"""
        deadline = time.time() + timeout
        frame = self.grab_quiet()
        while time.time() < deadline:
            if frame.ui_sig != prev_sig:
                return frame
            time.sleep(interval)
            frame = self.grab_quiet()
        return frame
