"""主控：把阶段 0~6 串起来，维护计数与停止条件。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from . import config as C
from .adb import Adb, AdbError
from .actions import Actor
from .adwatch import AdWatcher
from .farm import FarmNavigator, FarmTask
from .logger import Logger
from .perception import Frame, Perceiver


@dataclass
class RunStats:
    ads_done: int = 0
    failures: int = 0
    fail_streak: int = 0
    blocked_taps: int = 0

    def ok(self) -> None:
        self.fail_streak = 0

    def fail(self) -> None:
        self.failures += 1
        self.fail_streak += 1


class Runner:
    def __init__(self, adb: Adb, perceiver: Perceiver, log: Logger, threshold: int, max_ads: int):
        self.adb = adb
        self.p = perceiver
        self.log = log
        self.actor = Actor(adb, perceiver, log)
        self.nav = FarmNavigator(adb, perceiver, self.actor, log)
        self.watcher = AdWatcher(adb, perceiver, self.actor, log)
        self.stats = RunStats()
        self.threshold = threshold
        self.max_ads = max_ads
        self.stop_reason = ""
        self.empty_scans = 0
        self.dump_fails = 0

    # ------------------------------------------------------------ 停止条件

    def _should_stop(self) -> Optional[str]:
        if self.stats.ads_done >= self.max_ads:
            return f"已达到累计观看上限 {self.max_ads} 次"
        if self.stats.fail_streak >= C.MAX_FAIL_STREAK:
            return f"连续 {self.stats.fail_streak} 次广告未正常完成"
        return None

    # ------------------------------------------------------------ 主流程

    def run(self) -> str:
        self.log.raw("启动：芭芭农场自动观看肥料广告")
        self.log.raw(f"阈值：肥料 > {self.threshold}g；上限 {self.max_ads} 次；"
                     f"失败熔断 {C.MAX_FAIL_STREAK} 次")

        # ===== 阶段 0
        if not self.nav.ensure_foreground():
            self.log.summary("无法将淘宝切到前台", 0)
            return "no_foreground"

        try:
            frame = self.p.grab_quiet(tag="stage0")
        except AdbError as e:
            self.log.summary(f"阶段0 界面树抓取失败：{e}", 0)
            return "dump_fail"
        human = self.actor.need_human(frame)
        if human:
            self.log.summary(f"启动即遇到需人工处理页面：{human}", 0)
            return "need_human"

        # ===== 阶段 1 / 2：只在开始时做一次
        try:
            frame = self.nav.enter_farm()
        except AdbError as e:
            self.log.summary(f"阶段1 界面树抓取失败：{e}", 0)
            return "dump_fail"
        if frame is None:
            self.log.summary("未能进入芭芭农场", 0)
            return "no_farm"

        try:
            frame = self.nav.open_collect(frame)
        except AdbError as e:
            self.log.summary(
                f"阶段2 界面树抓取失败：{e}（页面持续动画会让 Android 8.x 的 "
                "uiautomator dump 崩溃，需人工关掉遮挡浮层或改用 OCR/uiautomator2）", 0)
            return "dump_fail"
        if frame is None:
            self.log.summary("未能进入集肥料页", 0)
            return "no_collect"

        # ===== 主循环（阶段 3~6）
        while True:
            stop = self._should_stop()
            if stop:
                self.stop_reason = stop
                break

            try:
                frame = self.p.grab_quiet(tag="loop")
                self.dump_fails = 0
            except AdbError as e:
                self.dump_fails += 1
                self.log.step("3", f"抓取失败（第 {self.dump_fails}/3 次）：{e}", False)
                self.stats.fail()
                if self.dump_fails >= 3:
                    # 页面持续动画会让 Android 8.x 的 dumpNodeRec 崩溃，
                    # 重试也拿不到界面树。此时继续跑等于盲操作，必须停机。
                    self.stop_reason = (
                        "界面树连续 3 次抓取失败（页面持续动画导致 uiautomator 崩溃），"
                        "已停机，需人工处理或改用 OCR/uiautomator2"
                    )
                    break
                time.sleep(2)
                continue

            human = self.actor.need_human(frame)
            if human:
                self.stop_reason = f"出现需人工处理页面：{human}"
                self.log.step("3", f"检测到人工处理页面「{human}」，停机", False)
                break

            # 不在集肥料页 → 先回退
            if not AdWatcher._looks_like_task_page(frame):
                self.log.step("6", "当前不在集肥料页，尝试回退", False)
                if not self._recover(frame):
                    self.stop_reason = "多次回退仍无法回到集肥料页"
                    break
                continue

            tasks = self.nav.scan_tasks(frame)
            self.log.raw(f"    本屏识别到 {len(tasks)} 张任务卡："
                         + "、".join(t.describe() for t in tasks[:6])
                         + (" ..." if len(tasks) > 6 else ""))

            target = self.nav.pick_target(tasks, self.threshold)
            if target is None:
                # 可能是需要下拉刷新，或本屏不含更多任务 → 滑一屏再找
                if self.empty_scans < 3:
                    self.empty_scans += 1
                    self.log.step("6", f"本屏无 > {self.threshold}g 任务，下滑寻找（第 {self.empty_scans}/3 次）", True)
                    self.actor.swipe_up("6", ratio=0.45)
                    time.sleep(1.0)
                    continue

                # 真没有了 → 尝试刷新一次，仍无则停
                self.log.step("6", "刷新任务列表后复核", True)
                frame = self.nav.refresh_list()
                tasks = self.nav.scan_tasks(frame)
                target = self.nav.pick_target(tasks, self.threshold)
                if target is None:
                    self.stop_reason = f"集肥料页已不存在 > {self.threshold}g 的可执行任务"
                    break
                self.empty_scans = 0

            self.empty_scans = 0
            self.log.step("3", f"选中任务「{target.describe()}」（肥料 {target.fert}g）", True)

            # ===== 阶段 4
            ad_frame = self.nav.enter_ad(target)
            if ad_frame is None:
                self.stats.fail()
                self.nav.mark_handled(target)
                self.log.step("4", "进入任务失败", False)
                self._recover(self.p.grab_quiet(tag="recover"))
                continue

            # ===== 阶段 5
            result = self.watcher.watch(tag="ad")
            self.log.step("5", f"广告观看结束（{result.status}）：{result.detail}", 
                          result.status == "success",
                          note=f"耗时 {result.seconds:.0f}s" + (f"，奖励文案「{result.reward_text}」" if result.reward_text else ""))

            if result.status == "need_human":
                self.stop_reason = result.detail
                break
            if result.status == "success":
                self.stats.ads_done += 1
                self.stats.ok()
                self.log.add_ad(target.fert)
                self.nav.mark_handled(target)
            else:
                self.stats.fail()
                self.nav.mark_handled(target)   # 避免在同一个坏任务上反复纠缠
                self.log.add_ad(0)

            # ===== 阶段 6：回到集肥料页
            back = self.p.grab_quiet(tag="back_to_list")
            if not AdWatcher._looks_like_task_page(back):
                self._recover(back)
            self.log.step("6", f"完成第 {self.stats.ads_done} 次观看，继续下一轮", True)

        self.log.summary(self.stop_reason or "正常结束", self.stats.failures)
        return self.stop_reason

    # ------------------------------------------------------------ 恢复

    def _recover(self, frame: Frame) -> bool:
        """逐级回退直到集肥料页。"""
        for i in range(C.MAX_BACK_STEPS):
            if AdWatcher._looks_like_task_page(frame):
                return True
            human = self.actor.need_human(frame)
            if human:
                self.log.step("6", f"回退途中遇到人工页面「{human}」", False)
                return False
            if self.actor.dismiss_popup(frame, "6"):
                frame = self.p.grab_quiet(tag="recover_popup")
                if AdWatcher._looks_like_task_page(frame):
                    return True
            self.actor.back("6", f"回退第 {i + 1} 步")
            frame = self.p.grab_quiet(tag="recover_back")
        return AdWatcher._looks_like_task_page(frame)
