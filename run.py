"""命令行入口。

用法：
  python run.py --probe            只截一帧，打印识别到的元素（不点击）
  python run.py --dry-run          完整识别流程，但所有点击被拦截（演练）
  python run.py                    真正执行
  python run.py --max-ads 10      限制观看条数
  python run.py --replay a.xml     离线回放一份界面树，验证识别逻辑
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from farmbot import config as C                     # noqa: E402
from farmbot import ui                              # noqa: E402
from farmbot.actions import Actor                   # noqa: E402
from farmbot.adb import Adb, AdbError, find_adb     # noqa: E402
from farmbot.adwatch import AdWatcher               # noqa: E402
from farmbot.farm import FarmNavigator              # noqa: E402
from farmbot.logger import Logger, setup_stdout     # noqa: E402
from farmbot.perception import Perceiver            # noqa: E402
from farmbot.runner import Runner                   # noqa: E402

SHOT_DIR = ROOT / "shots"
LOG_DIR = ROOT / "logs"


class NoopAdb(Adb):
    """演练模式：所有注入动作被吞掉，只保留感知能力。"""

    def __init__(self, serial=None, adb_path=None):
        super().__init__(serial=serial, adb_path=adb_path)
        self.actions: list[tuple] = []

    def tap(self, x, y):
        self.actions.append(("tap", x, y))
        print(f"    [dry-run] 本应点击 ({x},{y})")

    def swipe(self, x1, y1, x2, y2, duration_ms=320):
        self.actions.append(("swipe", x1, y1, x2, y2))

    def back(self):
        self.actions.append(("back",))

    def key(self, code):
        self.actions.append(("key", code))

    def start_app(self, package):
        self.actions.append(("start", package))


def build_dumper(adb: Adb, use_u2: bool = True):
    """尝试启用 uiautomator2 感知后端；不可用返回 None（回退 adb dump）。"""
    if not use_u2:
        return None
    try:
        from farmbot.u2sense import U2Dumper
        d = U2Dumper(adb.serial)
        print(f"感知后端: uiautomator2（max_depth={d.max_depth}，不依赖 uiautomator dump）")
        return d
    except Exception as e:                      # noqa: BLE001
        print(f"感知后端: adb uiautomator（uiautomator2 不可用：{type(e).__name__}: {e}）")
        return None


def probe(adb: Adb, limit: int = 60, use_u2: bool = True) -> None:
    """打印当前界面的结构化视图，用于人工核对识别效果。"""
    p = Perceiver(adb, SHOT_DIR, dumper=build_dumper(adb, use_u2))
    try:
        frame = p.grab(save_shot=True, tag="probe")
    except AdbError as e:
        print(f"[失败] 界面树抓取失败（后端 {p.backend}）：{e}")
        try:
            shot = SHOT_DIR / f"probe_fail_{time.strftime('%H%M%S')}.png"
            adb.screencap(save_path=shot)
            print(f"       已保存现场截图：{shot}")
        except AdbError:
            pass
        print("       常见原因：页面有持续动画/视频，Android 8.x 的 uiautomator 会在")
        print("       遍历中崩溃（dumpNodeRec NPE）。处理：让页面静止后再试，")
        print("       或用 --replay 对保存下来的 XML 做离线验证。")
        return
    size = adb.screen_size()
    print(f"屏幕: {size[0]}x{size[1]}   截图: {frame.shot_path}")
    print(f"节点总数: {len(frame.nodes)}   可点击: {len(ui.clickable(frame.nodes))}")
    print("-" * 70)
    shown = 0
    for n in frame.nodes:
        if not n.has_text():
            continue
        flag = "*" if n.clickable else " "
        print(f"{flag} {n.bounds}  {n.label[:50]}")
        shown += 1
        if shown >= limit:
            print("...")
            break
    print("-" * 70)
    nav = FarmNavigator(adb, p, Actor(adb, p, Logger(LOG_DIR)), Logger(LOG_DIR))
    tasks = nav.scan_tasks(frame)
    print(f"识别到任务卡 {len(tasks)} 张：")
    for t in tasks:
        print(f"  {t.fert:>6}g  {t.fert_raw:<8} {t.action_text:<8} @{t.button.center}")
    if not tasks:
        print("  （无）—— 若当前确实是集肥料页，说明肥料数值在图片里，需要接入 OCR")


def replay(xml_path: Path, threshold: int = C.FERT_THRESHOLD) -> None:
    """离线回放：不用设备，验证识别与筛选逻辑。"""
    xml = xml_path.read_text(encoding="utf-8")
    root, nodes = ui.parse(xml)
    sw, sh = ui.screen_box(nodes)
    print(f"节点数 {len(nodes)}，推算屏幕 {sw}x{sh}")

    frame = type("F", (), {})()
    frame.nodes, frame.root = nodes, root
    frame.screen_w, frame.screen_h = sw or 1080, sh or 2400
    frame.find = lambda needles, exact=False: ui.by_text(nodes, needles, exact=exact)
    frame.find_regex = lambda pat: ui.by_regex(nodes, pat)
    frame.has = lambda needles: ui.has_any(nodes, needles)
    frame.ui_sig = "replay"
    frame.shot_sig = "replay"
    frame.screenshot = b""
    frame.ts = 0

    log = Logger(LOG_DIR)
    nav = FarmNavigator(None, None, None, log)   # 只用到纯函数部分
    tasks = nav.scan_tasks(frame)
    print(f"\n任务卡 {len(tasks)} 张（按肥料降序）：")
    for t in tasks:
        print(f"  {t.fert:>6}g  {t.fert_raw:<10} {t.action_text:<10} 按钮@{t.button.center}")
    picked = nav.pick_target(tasks, threshold)
    print(f"\n阈值 >{threshold}g 时选中：{picked.describe() if picked else '无'}")
    cd = AdWatcher._countdown(frame)
    print(f"倒计时读数：{cd}")
    closers = AdWatcher._close_candidates(frame)
    print(f"关闭候选：{[n.label for n in closers]}")


def main() -> int:
    setup_stdout()
    ap = argparse.ArgumentParser(description="淘宝芭芭农场 · 自动观看肥料广告")
    ap.add_argument("--serial", help="adb 设备序列号（多设备时指定）")
    ap.add_argument("--threshold", type=int, default=C.FERT_THRESHOLD,
                    help=f"只做肥料值 > 该值的任务，默认 {C.FERT_THRESHOLD}")
    ap.add_argument("--max-ads", type=int, default=C.MAX_ADS,
                    help=f"最多观看广告条数，默认 {C.MAX_ADS}")
    ap.add_argument("--probe", action="store_true", help="只截一帧并打印识别结果")
    ap.add_argument("--dry-run", action="store_true", help="完整流程演练，但不真的点击")
    ap.add_argument("--replay", type=Path, help="离线回放一份 uiautomator XML")
    ap.add_argument("--no-u2", action="store_true",
                    help="禁用 uiautomator2 感知后端，只用 adb + uiautomator dump")
    ap.add_argument("--visual", action="store_true",
                    help="视觉通道：用 OCR 识别 H5 页面并完成浏览任务（无障碍读不到时用）")
    ap.add_argument("--cooldown", type=int, default=300,
                    help="视觉通道遇到限流时，回淘宝首页冷却多少秒（默认 300）")
    ap.add_argument("--cooldown-rounds", type=int, default=6,
                    help="最多冷却重试几轮（默认 6，0 表示不限流恢复就直接停）")
    args = ap.parse_args()

    if args.replay:
        replay(args.replay, args.threshold)
        return 0

    try:
        adb_bin = find_adb()
    except AdbError as e:
        print(f"[错误] {e}")
        return 2
    print(f"adb: {adb_bin}")

    adb: Adb
    if args.dry_run:
        adb = NoopAdb(serial=args.serial, adb_path=adb_bin)
    else:
        adb = Adb(serial=args.serial, adb_path=adb_bin)

    try:
        serial = adb.ensure_device()
    except AdbError as e:
        print(f"[错误] {e}")
        return 2
    print(f"设备: {serial}")

    if args.probe:
        probe(adb, use_u2=not args.no_u2)
        return 0

    if args.visual:
        from farmbot.panel import VisualBot
        vlog = Logger(LOG_DIR)
        u2dev = None
        if not args.no_u2 and not args.dry_run:
            try:
                from farmbot.u2sense import build_device
                u2dev = build_device(args.serial or adb.serial)
                print("输入后端: uiautomator2 常驻服务（截图约 0.23s / 点击约 0.18s）")
            except Exception as e:              # noqa: BLE001
                print(f"输入后端: adb（uiautomator2 不可用：{type(e).__name__}）")
        try:
            bot = VisualBot(adb, vlog, max_tasks=args.max_ads, shot_dir=SHOT_DIR,
                            dry_run=args.dry_run, cooldown_seconds=args.cooldown,
                            cooldown_rounds=args.cooldown_rounds, u2dev=u2dev)
            res = bot.run()
            vlog.raw(f"完成 {len(res['done'])} 条浏览任务；失败 {len(res['failed'])} 条")
            return 0
        except AdbError as e:
            vlog.raw(f"视觉通道异常终止：{e}")
            return 1
        finally:
            vlog.close()

    log = Logger(LOG_DIR)
    perceiver = Perceiver(adb, SHOT_DIR, dumper=build_dumper(adb, not args.no_u2))
    log.raw(f"感知后端：{perceiver.backend}")
    runner = Runner(adb, perceiver, log, threshold=args.threshold, max_ads=args.max_ads)
    try:
        runner.run()
    except KeyboardInterrupt:
        log.raw("收到中断信号，正在退出")
        log.summary("人工中断", runner.stats.failures)
        return 130
    except AdbError as e:
        log.raw(f"adb 异常终止：{e}")
        log.summary(f"adb 异常：{e}", runner.stats.failures)
        return 1
    finally:
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
