"""阶段 5：激励广告观看循环。

设计要点（相比"读不到倒计时就死等 30 秒"的做法）：
  - 每轮只做一次感知，按优先级分类：需人工 / 危险弹窗 / 结算 / 倒计时 / 交互提示 / 可关闭
  - 「跳过 5」这类带数字的跳过按钮视为还在倒计时，不点；只有数字归零或无数字时才点
  - 用整屏像素指纹判断"画面是否真的卡死"，而不是无条件干等
  - 结算后走 _finalize：广告页常见"结算页 → 再关闭 → 二次弹窗"的多层关闭，
    单次关闭会把流程卡在中间页
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

from . import config as C
from . import ui
from .adb import Adb, AdbError
from .actions import Actor
from .logger import Logger
from .perception import Frame, Perceiver


@dataclass
class AdResult:
    status: str          # success / failed / need_human / timeout
    detail: str
    seconds: float
    reward_text: str = ""
    close_rounds: int = 0


class AdWatcher:
    def __init__(self, adb: Adb, perceiver: Perceiver, actor: Actor, log: Logger):
        self.adb = adb
        self.p = perceiver
        self.actor = actor
        self.log = log
        self._fired: set[str] = set()   # 已执行过的交互提示，避免重复点击

    # ------------------------------------------------------------ 分类

    @staticmethod
    def _countdown(frame: Frame) -> Optional[int]:
        """返回剩余秒数；识别不到返回 None。"""
        best: Optional[int] = None
        for node, m in frame.find_regex(C.COUNTDOWN_PATTERN):
            try:
                val = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 0 <= val <= 300:
                best = val if best is None else min(best, val)
        return best

    @staticmethod
    def _close_candidates(frame: Frame) -> list[ui.Node]:
        out = []
        for n in frame.nodes:
            if not n.visible():
                continue
            label = n.label.strip()
            if not label:
                continue
            if label in ("X", "x", "×", "✕"):
                out.append(n)
                continue
            for word in ("关闭", "跳过", "残忍"):
                if word in label:
                    out.append(n)
                    break
        # 面积小的更像按钮；同一位置去重
        out.sort(key=lambda n: n.area)
        return out

    @staticmethod
    def _skip_is_counting(node: ui.Node) -> bool:
        """「跳过 5s」这种带正数的是倒计时中，不能点。"""
        m = re.search(r"(\d{1,3})\s*(?:s|S|秒)?", node.label)
        if not m:
            return False
        try:
            return int(m.group(1)) > 0
        except ValueError:
            return False

    def _dangerous_popup(self, frame: Frame) -> Optional[ui.Node]:
        """检测下载/安装/授权类弹窗：不点它，但要知道它挡住了。"""
        for n in frame.nodes:
            if not n.visible() or not n.clickable:
                continue
            for word in ("下载", "安装", "允许", "授权"):
                if word in n.label:
                    return n
        return None

    # ------------------------------------------------------------ 主循环

    def watch(self, tag: str = "") -> AdResult:
        start = time.time()
        last_shot_sig = ""
        idle = 0
        fired_swipe = 0
        fired_tapthrough = 0
        poll = 0
        reward = ""
        last_countdown: Optional[int] = None

        while True:
            elapsed = time.time() - start
            if elapsed > C.AD_MAX_SECONDS:
                return AdResult("timeout", f"单条广告跟踪超过 {C.AD_MAX_SECONDS}s", elapsed, reward)

            try:
                frame = self.p.grab_quiet(tag=tag)
            except AdbError as e:
                self.log.step("5", f"感知失败（{e}）", False)
                time.sleep(1.5)
                continue
            poll += 1

            # 1) 需要人工 → 立即停
            human = self.actor.need_human(frame)
            if human:
                return AdResult("need_human", f"出现人工处理页面：{human}", elapsed, reward)

            # 2) 结算文案
            settle = frame.has(C.SETTLEMENT_TEXTS)
            if settle:
                reward = settle
                self.log.step("5", f"识别到结算文案「{settle}」", True)
                rounds = self._finalize(frame, "5")
                ok = rounds > 0
                return AdResult("success" if ok else "failed",
                                "结算后已返回任务页" if ok else "结算后未能返回任务页",
                                elapsed, reward, rounds)

            # 3) 危险弹窗遮挡 → 只找关闭
            danger = self._dangerous_popup(frame)
            if danger is not None:
                self.log.step("5", f"发现下载/授权类弹窗「{danger.label[:14]}」，只寻找关闭入口", True)
                if self.actor.dismiss_popup(frame, "5", tries=2):
                    time.sleep(0.8)
                    continue

            # 4) 交互提示
            swipe_prompt = frame.has(C.SWIPE_PROMPTS)
            if swipe_prompt and fired_swipe < 3:
                sig = f"swipe|{swipe_prompt}"
                if sig not in self._fired or True:
                    fired_swipe += 1
                    self.actor.swipe_up("5")
                    self._fired.add(sig)
                    continue

            tap_prompt = frame.has(C.TAP_THROUGH_PROMPTS)
            if tap_prompt and fired_tapthrough < 2:
                hits = frame.find(C.TAP_THROUGH_PROMPTS)
                if hits:
                    fired_tapthrough += 1
                    if self.actor.tap_node(hits[0], "5", f"点击「{tap_prompt}」"):
                        time.sleep(2.5)
                        self.actor.back("5", "从详情页返回广告")
                        continue

            # 5) 倒计时
            cd = self._countdown(frame)
            if cd is not None:
                last_countdown = cd
                if cd == 0:
                    self.log.step("5", "倒计时归零", True)
                    rounds = self._finalize(frame, "5")
                    return AdResult("success" if rounds else "failed",
                                    "倒计时结束并已返回任务页" if rounds else "倒计时结束但未能返回",
                                    elapsed, reward, rounds)
                self.log.raw(f"    倒计时剩余 {cd}s（已过 {elapsed:.0f}s）")

            # 6) 可关闭判定
            closers = self._close_candidates(frame)
            ready = [n for n in closers if not self._skip_is_counting(n)]
            if ready and elapsed > 2.0:
                target = ready[0]
                self.log.step("5", f"点击关闭「{target.label[:12]}」", True)
                try:
                    self.adb.tap(*target.center)
                except AdbError:
                    pass
                time.sleep(C.ACTION_SETTLE)
                rounds = self._finalize(self.p.grab_quiet(tag="after_close"), "5")
                if rounds > 0:
                    return AdResult("success", "点击关闭后已返回任务页", elapsed, reward, rounds)
                # 关不掉，继续循环，最多再试一次
                if elapsed > 20:
                    return AdResult("failed", "多次点击关闭仍无法退出广告", elapsed, reward)

            # 7) 静止判断：UI 与画面都不动
            if frame.shot_sig == last_shot_sig:
                idle += 1
            else:
                idle = 0
            last_shot_sig = frame.shot_sig

            if idle >= C.UI_IDLE_FRAMES and elapsed > 12:
                self.log.step("5", f"画面连续 {idle} 帧无变化，尝试返回", True)
                self.actor.back("5", "返回键退出静止广告")
                frame2 = self.p.grab_quiet(tag="after_back")
                if self._looks_like_task_page(frame2):
                    return AdResult("failed", "广告画面静止，已返回任务页（可能未计奖）", elapsed, reward)
                idle = 0

            # 8) 每 4 轮查一次前台，防止被拉起外部 App
            if poll % 4 == 0:
                focus = self.adb.current_focus()
                if focus and C.APP_PACKAGE not in focus:
                    self.log.step("5", f"前台已切到 {focus}，尝试返回淘宝", True)
                    self.actor.back("5", "从外部 App 返回")
                    time.sleep(1.5)

            # 9) 节奏控制
            if last_countdown is not None and last_countdown <= 3:
                time.sleep(C.POLL_INTERVAL_FAST)
            elif last_countdown is not None:
                time.sleep(C.POLL_INTERVAL)
            elif elapsed < 3:
                time.sleep(1.0)
            else:
                time.sleep(C.POLL_INTERVAL)

    # ------------------------------------------------------------ 收尾

    @staticmethod
    def _looks_like_task_page(frame: Frame) -> bool:
        return bool(frame.has(("集肥料", "肥料任务", "我的肥料", "施肥")))

    def _finalize(self, frame: Frame, stage: str, max_rounds: int = 4) -> int:
        """多层关闭：结算页 → 关闭 → 可能还有二次弹窗。返回成功退出的轮数。"""
        rounds = 0
        for i in range(max_rounds):
            if self._looks_like_task_page(frame):
                return rounds if rounds else 1
            acted = False
            closers = self._close_candidates(frame)
            ready = [n for n in closers if not self._skip_is_counting(n)]
            if ready:
                target = min(ready, key=lambda n: n.area)
                try:
                    self.adb.tap(*target.center)
                    acted = True
                    rounds += 1
                    time.sleep(C.ACTION_SETTLE)
                except AdbError:
                    break
            if not acted:
                # 没有关闭按钮就退回
                try:
                    self.adb.back()
                    time.sleep(C.ACTION_SETTLE)
                    rounds += 0.5
                except AdbError:
                    break
            frame = self.p.grab_quiet(tag=f"finalize{i}")
        if self._looks_like_task_page(frame):
            return max(1, int(rounds))
        return 0
