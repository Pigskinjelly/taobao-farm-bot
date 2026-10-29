"""阶段 1~3：进入芭芭农场 → 打开集肥料 → 扫描并挑选任务。

任务卡识别策略：
  1. 在一屏 UI 树里用"严格肥料正则"找候选数字（必须带 +、g、kg 等单位标记，
     避免把"3/5次""第2天"这类计数误认成肥料值）。
  2. 以该数字节点为锚点，在竖直 ±CARD_DY 范围内找可点击的"去完成/去观看"按钮，
     两者构成一张任务卡。
  3. 按钮文案命中"已领取/已完成"则整卡丢弃。
  4. 剩余的按肥料值降序，取第一个 > FERT_THRESHOLD 的。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from . import config as C
from . import ui
from .adb import Adb, AdbError
from .actions import Actor
from .logger import Logger
from .perception import Frame, Perceiver

# 卡片内数字与按钮的竖直容差（按屏幕高度比例）
# 注意：任务卡是整行布局——肥料值靠左、按钮靠右，横向距离可以很大，
# 真正区分卡片的是竖直方向，所以竖直容差收紧、横向放宽。
CARD_DY_RATIO = 0.07
CARD_DX_RATIO = 0.92


@dataclass
class FarmTask:
    fert: int
    fert_raw: str
    action_text: str
    anchor: ui.Node
    button: ui.Node
    key: str

    def describe(self) -> str:
        return f"{self.fert_raw} / {self.action_text}"


class FarmNavigator:
    def __init__(self, adb: Adb, perceiver: Perceiver, actor: Actor, log: Logger):
        self.adb = adb
        self.p = perceiver
        self.actor = actor
        self.log = log
        self._handled: set[str] = set()

    # ------------------------------------------------------------ 阶段 0

    def ensure_foreground(self) -> bool:
        focus = self.adb.current_focus()
        if C.APP_PACKAGE in focus:
            self.log.step("0", f"淘宝已在前台（{focus}）", True)
            return True
        self.log.step("0", f"当前前台不是淘宝（{focus or '未知'}），启动淘宝", True)
        try:
            self.adb.start_app(C.APP_PACKAGE)
        except AdbError as e:
            self.log.step("0", f"启动淘宝失败：{e}", False)
            return False
        # 老机型冷启动要 10s+，固定等 6s 会误判失败，改成轮询到超时
        deadline = time.time() + C.APP_START_TIMEOUT
        focus = ""
        while time.time() < deadline:
            time.sleep(2.0)
            focus = self.adb.current_focus()
            if C.APP_PACKAGE in focus:
                self.log.step("0", f"启动后前台={focus}", True)
                return True
        self.log.step("0", f"启动后前台={focus or '未知'}（等待 {C.APP_START_TIMEOUT:.0f}s 仍未就绪）",
                      False)
        return False

    # ------------------------------------------------------------ 阶段 1

    def _scan_for_entry(self, frame: Frame, max_scrolls: int = 4) -> Optional[ui.Node]:
        """在首页找「芭芭农场」入口。

        只接受"小面积 + 可点击"的节点：农场页自己的根节点 content-desc
        也叫「芭芭农场」，但它是全屏容器，面积占比极大，点它中心等于在全屏
        随机位置盲点一次，必须排除。
        """
        def _pick(hits: list[ui.Node]) -> Optional[ui.Node]:
            limit = frame.screen_w * frame.screen_h * C.ENTRY_MAX_AREA_RATIO
            pool = [n for n in hits if n.area <= limit]
            pool = [n for n in pool if n.clickable] or pool
            return min(pool, key=lambda n: n.area) if pool else None

        picked = _pick(frame.find(C.FARM_ENTRY_TEXTS))
        if picked:
            return picked
        for i in range(max_scrolls):
            self.actor.swipe_up("1")
            frame = self.p.grab_quiet(tag="find_farm")
            picked = _pick(frame.find(C.FARM_ENTRY_TEXTS))
            if picked:
                return picked
        return None

    def on_farm_page(self, frame: Frame) -> bool:
        """判断当前是否已经在农场页。

        两条路：
          1. 能看到「集肥料/施肥」——最可靠；
          2. 看不到但**前台 Activity 是 Themis 容器**且页面标题是「芭芭农场」——
             真机上 H5 节点经常只暴露一部分（见 DESIGN.md 13.13），
             此时不能因为找不到「集肥料」就跑去首页找入口，否则会在农场页里
             连续上滑，把页面滑到别处。
        """
        if frame.has(("集肥料", "施肥")):
            return True
        focus = ""
        try:
            if self.adb is not None:
                focus = self.adb.current_focus()
        except AdbError:
            focus = ""
        if any(h in focus for h in C.FARM_ACTIVITY_HINTS):
            return bool(frame.has(C.FARM_ENTRY_TEXTS))
        return False

    def enter_farm(self) -> Optional[Frame]:
        frame = self.p.grab_quiet(tag="stage1")
        # 已经在农场页（手动进过、或脚本上一轮已进入）就别再找入口了，
        # 否则会在农场页里点「芭芭农场」这个全屏容器。
        if self.on_farm_page(frame):
            self.log.step("1", "已在芭芭农场页，跳过入口查找", True)
            return frame
        entry = self._scan_for_entry(frame)
        if entry is None:
            self.log.step("1", "当前屏未找到「芭芭农场」入口", False,
                          note="可尝试在淘宝内手动进入一次，脚本会从集肥料页接管")
            return None
        if not self.actor.tap_node(entry, "1", f"点击入口「{entry.label[:12]}」"):
            return None
        frame = self.p.wait_ui_change(frame.ui_sig, timeout=C.FARM_LOAD_TIMEOUT)
        # 等 Farms 页把「集肥料/施肥」渲染出来（真机实测要 10~15s）
        deadline = time.time() + C.FARM_LOAD_TIMEOUT
        while time.time() < deadline and not self.on_farm_page(frame):
            time.sleep(1.0)
            frame = self.p.grab_quiet(tag="farm_wait")
        # 引导/新人弹窗
        for _ in range(3):
            if self.actor.dismiss_popup(frame, "1"):
                frame = self.p.grab_quiet(tag="farm_ready")
            else:
                break
        # 判断是否进到了农场
        ready = self.on_farm_page(frame)
        self.log.step("1", "进入芭芭农场", bool(ready),
                      note="" if ready else "未识别到农田特征元素（集肥料/施肥）")
        return frame

    # ------------------------------------------------------------ 阶段 2

    def open_collect(self, frame: Optional[Frame] = None) -> Optional[Frame]:
        frame = frame or self.p.grab_quiet(tag="stage2")
        hits = frame.find(C.COLLECT_TEXTS)
        if not hits:
            self.actor.dismiss_popup(frame, "2")
            frame = self.p.grab_quiet(tag="stage2b")
            hits = frame.find(C.COLLECT_TEXTS)
        if not hits:
            self.log.step("2", "未找到「集肥料」按钮", False)
            return None
        target = min(hits, key=lambda n: n.area)
        # 集肥料按钮本身可能不可点击，点它的父级区域中心更稳
        if not self.actor.tap_node(target, "2", "点击「集肥料」"):
            return None
        frame = self.p.wait_ui_change(frame.ui_sig, timeout=C.PAGE_LOAD_WAIT * 2)
        self.log.step("2", "进入集肥料页", True)
        return frame

    # ------------------------------------------------------------ 阶段 3

    @staticmethod
    def _parse_fert(label: str) -> Optional[tuple[int, str]]:
        """从一段文案里提取肥料数值。只认带 + 号或带单位的数字。"""
        if not label:
            return None
        m = re.search(C.FERT_STRICT_PATTERN, label)
        if not m:
            return None
        num = m.group(1) or m.group(2)
        unit = (m.group(0) or "").lower()
        try:
            value = float(num)
        except (TypeError, ValueError):
            return None
        if "kg" in unit or "千克" in unit:
            value *= 1000
        return int(round(value)), m.group(0).strip()

    def scan_tasks(self, frame: Frame) -> list[FarmTask]:
        max_dy = int(frame.screen_h * CARD_DY_RATIO)
        max_dx = int(frame.screen_w * CARD_DX_RATIO)
        buttons = [n for n in frame.nodes if n.clickable and n.has_text()]
        if not buttons:
            buttons = [n for n in frame.nodes if n.has_text()]

        tasks: list[FarmTask] = []
        for node in frame.nodes:
            parsed = self._parse_fert(node.label)
            if not parsed:
                continue
            fert, raw = parsed
            # 在该数字附近找"决定卡片状态"的那个按钮：
            # 竖直方向最近者胜出；同距离下优先可执行按钮，其次面积小的。
            cands: list[tuple[float, int, int, ui.Node, bool]] = []
            for cand in buttons:
                dy = abs(cand.cy - node.cy)
                if dy > max_dy:
                    continue
                if abs(cand.cx - node.cx) > max_dx:
                    continue
                label = cand.label
                is_done = any(w in label for w in C.TASK_DONE_TEXTS)
                is_action = any(w in label for w in C.TASK_ACTION_TEXTS)
                if not (is_done or is_action):
                    continue
                cands.append((dy, 0 if is_action else 1, cand.area, cand, is_done))
            if not cands:
                continue
            cands.sort(key=lambda t: (t[0], t[1], t[2]))
            _, _, _, button, is_done = cands[0]
            if is_done:
                # 最近的那个按钮是「已领取/已完成」，整卡跳过
                continue
            key = f"{raw}|{button.label}|{node.top // 50}|{button.top // 50}"
            tasks.append(FarmTask(fert=fert, fert_raw=raw, action_text=button.label,
                                  anchor=node, button=button, key=key))
        # 去重
        uniq: dict[str, FarmTask] = {}
        for t in tasks:
            if t.key not in uniq or t.button.area < uniq[t.key].button.area:
                uniq[t.key] = t
        result = sorted(uniq.values(), key=lambda t: t.fert, reverse=True)
        return result

    def pick_target(self, tasks: list[FarmTask], threshold: int) -> Optional[FarmTask]:
        for t in tasks:
            if t.fert <= threshold:
                continue
            if t.key in self._handled:
                continue
            return t
        return None

    def refresh_list(self) -> Frame:
        """下拉刷新集肥料列表。"""
        self.actor.swipe_down("3", ratio=0.4)
        time.sleep(1.2)
        return self.p.grab_quiet(tag="refreshed")

    def mark_handled(self, task: FarmTask) -> None:
        self._handled.add(task.key)

    # ------------------------------------------------------------ 阶段 4

    def enter_ad(self, task: FarmTask) -> Optional[Frame]:
        frame = self.p.grab_quiet(tag="stage4")
        # 用最新一帧重新定位按钮（页面可能刚刷新过，坐标已变）
        candidates = frame.find((task.action_text,))
        target = None
        if candidates:
            near = min(candidates, key=lambda n: abs(n.cy - task.button.cy))
            if abs(near.cy - task.button.cy) < frame.screen_h * 0.25:
                target = near
        if target is None:
            # 兜底：按原坐标再点一次
            target = task.button
        if not self.actor.tap_node(target, "4", f"进入任务「{task.describe()}」"):
            return None
        frame = self.p.wait_ui_change(frame.ui_sig, timeout=10)
        return frame
