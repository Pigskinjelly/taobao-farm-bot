"""动作层：所有点击都必须经过 TapGuard。

这是整套脚本里唯一允许"按下屏幕"的地方，安全约束（不下载、不授权、
不支付）在 tap_node 里统一拦截，业务代码没有旁路可以绕过去。
"""
from __future__ import annotations

import time
from typing import Optional

from . import config as C
from . import ui
from .adb import Adb, AdbError
from .logger import Logger
from .perception import Frame, Perceiver


class BlockedAction(RuntimeError):
    """被安全策略拦下的点击。"""


class Actor:
    def __init__(self, adb: Adb, perceiver: Perceiver, log: Logger):
        self.adb = adb
        self.p = perceiver
        self.log = log
        self.blocked_count = 0

    # ------------------------------------------------------------ 安全闸门

    @staticmethod
    def is_forbidden(node: ui.Node) -> Optional[str]:
        label = node.label
        if not label:
            return None
        for word in C.FORBIDDEN_TEXTS:
            if word in label:
                return word
        return None

    def tap_node(self, node: ui.Node, stage: str, action: str,
                 force: bool = False) -> bool:
        """点击元素中心。命中黑名单则拒绝（除非 force，且 force 不用于黑名单）。"""
        if not node.visible():
            self.log.step(stage, f"{action}（元素不可见 {node!r}）", False)
            return False
        bad = self.is_forbidden(node)
        if bad and not force:
            self.blocked_count += 1
            self.log.step(stage, f"{action}（安全拦截：命中禁用词「{bad}」）", False,
                          note="已跳过，绝不点击")
            return False
        x, y = node.center
        try:
            self.adb.tap(x, y)
        except AdbError as e:
            self.log.step(stage, f"{action}（adb 异常 {e}）", False)
            return False
        time.sleep(C.ACTION_SETTLE)
        self.log.step(stage, f"{action} @({x},{y})", True)
        return True

    def tap_point(self, x: int, y: int, stage: str, action: str) -> None:
        self.adb.tap(x, y)
        time.sleep(C.ACTION_SETTLE)
        self.log.step(stage, f"{action} @({x},{y})", True)

    # ------------------------------------------------------------ 通用手势

    def back(self, stage: str = "-", action: str = "返回键") -> None:
        try:
            self.adb.back()
            time.sleep(C.ACTION_SETTLE)
            self.log.step(stage, action, True)
        except AdbError as e:
            self.log.step(stage, f"{action}（{e}）", False)

    def swipe_up(self, stage: str = "-", ratio: float = 0.55) -> None:
        w, h = self.adb.screen_size()
        x = w // 2
        self.adb.swipe(x, int(h * 0.72), x, int(h * (0.72 - ratio)), duration_ms=380)
        time.sleep(0.8)
        self.log.step(stage, "上滑手势", True)

    def swipe_down(self, stage: str = "-", ratio: float = 0.5) -> None:
        w, h = self.adb.screen_size()
        x = w // 2
        self.adb.swipe(x, int(h * 0.30), x, int(h * (0.30 + ratio)), duration_ms=380)
        time.sleep(0.8)
        self.log.step(stage, "下滑手势", True)

    # ------------------------------------------------------------ 关闭弹窗

    @staticmethod
    def is_close_label(label: str) -> bool:
        """判断一个文案是不是"关闭控件"。

        真机教训：农场页右上角是「返回首页」，早期白名单里含「返回」，
        子串匹配直接把它当关闭按钮点掉，结果离开了当前页面。
        所以规则改成：
          1. 命中导航/离开语义（返回首页、更多、设置…）一律不算关闭；
          2. 单个 X/×/✕ 必须完全相等；
          3. 强关闭语义（关闭/跳过/残忍/以后再说…）允许子串匹配；
          4. 其余必须与白名单完全相等。
        """
        t = (label or "").strip()
        if not t:
            return False
        if any(x in t for x in C.CLOSE_EXCLUDE_TEXTS):
            return False
        if t in ("X", "x", "×", "✕"):
            return True
        if any(w in t for w in C.CLOSE_STRONG_TEXTS):
            return True
        return t in C.CLOSE_TEXTS

    def dismiss_popup(self, frame: Frame, stage: str = "-", tries: int = 2) -> bool:
        """尝试关掉当前弹窗。返回是否点到了东西。"""
        closed_any = False
        for _ in range(tries):
            candidates = [n for n in frame.nodes
                          if n.visible() and self.is_close_label(n.label)]
            if not candidates:
                break
            # 打分：面积小（像按钮）+ 靠右上（常见关闭位）优先
            h, w = frame.screen_h, frame.screen_w
            candidates.sort(key=lambda n: (
                0 if (n.cy < h * 0.55 and n.cx > w * 0.55) else 1,
                n.area,
            ))
            target = candidates[0]
            if self.tap_node(target, stage, f"关闭弹窗「{target.label[:12]}」"):
                closed_any = True
                frame = self.p.grab_quiet(tag="after_close")
            else:
                break
        return closed_any

    # ------------------------------------------------------------ 需要人工

    @staticmethod
    def need_human(frame: Frame) -> Optional[str]:
        return frame.has(C.NEED_HUMAN_TEXTS)
