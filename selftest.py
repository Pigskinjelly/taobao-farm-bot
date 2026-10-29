"""离线自检：不需要手机，验证识别与安全策略是否正确。

运行：python selftest.py
全部通过返回 0，任一失败返回 1。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from farmbot import config as C          # noqa: E402
from farmbot import ui                   # noqa: E402
from farmbot.actions import Actor        # noqa: E402
from farmbot.adwatch import AdWatcher    # noqa: E402
from farmbot.farm import FarmNavigator   # noqa: E402
from farmbot.logger import Logger        # noqa: E402
from farmbot.perception import Perceiver  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name} {extra}")


# ---------------------------------------------------------------- 工具

class RecordAdb:
    """只记录动作，不真执行。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def tap(self, x, y):
        self.calls.append(("tap", x, y))

    def swipe(self, *a, **k):
        self.calls.append(("swipe",) + a)

    def back(self):
        self.calls.append(("back",))

    def key(self, c):
        self.calls.append(("key", c))

    def screen_size(self):
        return (1080, 2400)

    def current_focus(self):
        return "com.taobao.taobao/.MainActivity"


class NoopPerceiver(Perceiver):
    def __init__(self):
        pass

    def grab(self, save_shot=False, tag=""):
        return None

    def grab_quiet(self, tag="", retries=1):
        return None


def make_frame(xml_path: Path):
    xml = xml_path.read_text(encoding="utf-8")
    root, nodes = ui.parse(xml)
    sw, sh = ui.screen_box(nodes)
    f = type("F", (), {})()
    f.nodes, f.root = nodes, root
    f.screen_w, f.screen_h = sw or 1080, sh or 2400
    f.find = lambda needles, exact=False: ui.by_text(nodes, needles, exact=exact)
    f.find_regex = lambda pat: ui.by_regex(nodes, pat)
    f.has = lambda needles: ui.has_any(nodes, needles)
    f.ui_sig = f.shot_sig = "x"
    f.screenshot, f.ts, f.shot_path = b"", 0, None
    return f


FIX = ROOT / "fixtures"


# ---------------------------------------------------------------- 用例

def test_fert_parse() -> None:
    print("\n[1] 肥料数值解析")
    cases = {
        "+1200g": 1200, "900g": 900, "+1000": 1000, "1.2kg": 1200, "800g": 800,
        "浏览会场 3/5次": None, "第2天": None, "剩余 15 秒": None, "集肥料": None,
    }
    for text, expect in cases.items():
        got = FarmNavigator._parse_fert(text)
        value = got[0] if got else None
        check(f"{text!r} -> {expect}", value == expect, f"实得 {value}")


def test_scan() -> None:
    print("\n[2] 集肥料页任务卡扫描")
    frame = make_frame(FIX / "farm_page.xml")
    nav = FarmNavigator(None, None, None, Logger(ROOT / "logs"))
    tasks = nav.scan_tasks(frame)
    ferts = [t.fert for t in tasks]
    check("识别出 3 张可执行卡", len(tasks) == 3, f"实得 {len(tasks)}: {ferts}")
    check("按肥料降序", ferts == sorted(ferts, reverse=True), f"{ferts}")
    check("已领取的 900g 卡被排除", 900 not in ferts)
    check("诱饵 3/5次 未误识别", all(t.fert in (1200, 1000, 500) for t in tasks), f"{ferts}")

    picked = nav.pick_target(tasks, 800)
    check("阈值 800 选中 1200g", picked is not None and picked.fert == 1200,
          f"实得 {picked.describe() if picked else None}")

    picked_low = nav.pick_target(tasks, 400)
    check("阈值 400 仍选最大 1200g", picked_low is not None and picked_low.fert == 1200)

    nav.mark_handled(picked)
    nxt = nav.pick_target(tasks, 800)
    check("已处理的 1200g 不再重复选中", nxt is not None and nxt.fert == 1000,
          f"实得 {nxt.fert if nxt else None}")


def test_safety() -> None:
    print("\n[3] 安全闸门（黑名单点击拦截）")
    log = Logger(ROOT / "logs", echo=False)
    actor = Actor(RecordAdb(), NoopPerceiver(), log)

    fake_xml = (FIX / "settlement_page.xml").read_text(encoding="utf-8")
    _, nodes = ui.parse(fake_xml)

    dl = [n for n in nodes if "下载" in n.label]
    check("样本中存在「立即下载App」按钮", len(dl) == 1)
    if dl:
        ok = actor.tap_node(dl[0], "5", "点下载")
        check("下载按钮被拒绝点击", ok is False)
        check("确实没有产生 tap 动作", len(actor.adb.calls) == 0, f"{actor.adb.calls}")

    close = [n for n in nodes if "关闭" in n.label]
    if close:
        ok = actor.tap_node(close[0], "5", "点关闭")
        check("正常关闭按钮可以点击", ok is True)
        check("产生了一次 tap", actor.adb.calls[-1][0] == "tap")

    for word in ("安装", "允许", "授权", "支付", "立即购买", "实名"):
        n = ui.Node(text=f"请{word}以继续", bounds=(0, 0, 100, 100), clickable=True)
        check(f"禁用词「{word}」被拦截", Actor.is_forbidden(n) == word,
              f"实得 {Actor.is_forbidden(n)}")

    # 关闭控件判定：真机上「返回首页」曾被当成关闭按钮点掉
    for label, want in (
        ("关闭广告", True),
        ("×", True),
        ("跳过 5s", True),
        ("以后再说", True),
        ("我知道了", True),
        ("返回首页", False),
        ("똏 返回首页", False),
        ("更多_细 按钮", False),
        ("设置", False),
        ("去逛逛", False),
    ):
        got = Actor.is_close_label(label)
        check(f"关闭判定「{label}」== {want}", got is want, f"实得 {got}")


def test_ad_rules() -> None:
    print("\n[4] 广告页判定规则")
    watcher = AdWatcher(None, None, None, Logger(ROOT / "logs", echo=False))

    ad = make_frame(FIX / "ad_page.xml")
    check("读到倒计时 5 秒", watcher._countdown(ad) == 5, f"实得 {watcher._countdown(ad)}")
    closers = watcher._close_candidates(ad)
    check("识别到「跳过 5s」", any("跳过" in n.label for n in closers))
    check("「跳过 5s」被判为倒计时中、不可点",
          all(watcher._skip_is_counting(n) for n in closers))

    ready = ui.Node(text="跳过", bounds=(0, 0, 100, 100), clickable=True)
    check("「跳过」无数字时可点", not watcher._skip_is_counting(ready))
    done = ui.Node(text="跳过 0s", bounds=(0, 0, 100, 100), clickable=True)
    check("「跳过 0s」可点", not watcher._skip_is_counting(done))
    check("识别到上滑提示", ad.has(C.SWIPE_PROMPTS) == "上滑")

    st = make_frame(FIX / "settlement_page.xml")
    check("识别到结算文案", st.has(C.SETTLEMENT_TEXTS) == "恭喜获得",
          f"实得 {st.has(C.SETTLEMENT_TEXTS)}")
    check("结算页不被误判为任务页", not watcher._looks_like_task_page(st))
    check("任务页判定正确", watcher._looks_like_task_page(make_frame(FIX / "farm_page.xml")))


def test_need_human() -> None:
    print("\n[5] 需人工处理检测")
    nodes = [
        ui.Node(text="请拖动滑块完成验证", bounds=(0, 0, 500, 100)),
        ui.Node(text="安全验证", bounds=(0, 0, 500, 100)),
        ui.Node(text="正常文案", bounds=(0, 0, 500, 100)),
    ]
    check("命中「滑块」", ui.has_any(nodes, C.NEED_HUMAN_TEXTS) == "滑块",
          f"实得 {ui.has_any(nodes, C.NEED_HUMAN_TEXTS)}")
    ok_nodes = [ui.Node(text="集肥料", bounds=(0, 0, 500, 100))]
    check("正常页面不误报", ui.has_any(ok_nodes, C.NEED_HUMAN_TEXTS) is None)


def test_watch_loop() -> None:
    """端到端仿真：广告页 → 广告页 → 结算页 → 回到任务页，应判成功。"""
    print("\n[6] 广告观看循环仿真")
    seq = [
        make_frame(FIX / "ad_page.xml"),
        make_frame(FIX / "ad_page.xml"),
        make_frame(FIX / "settlement_page.xml"),
        make_frame(FIX / "farm_page.xml"),
    ]

    class Scripted(NoopPerceiver):
        def __init__(self):
            self.i = 0

        def grab_quiet(self, tag="", retries=1):
            f = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return f

    old_poll, old_settle = C.POLL_INTERVAL, C.ACTION_SETTLE
    C.POLL_INTERVAL = 0.01
    C.ACTION_SETTLE = 0.01
    try:
        watcher = AdWatcher(RecordAdb(), Scripted(), None, Logger(ROOT / "logs", echo=False))
        watcher.actor = Actor(watcher.adb, Scripted(), Logger(ROOT / "logs", echo=False))
        result = watcher.watch()
    finally:
        C.POLL_INTERVAL, C.ACTION_SETTLE = old_poll, old_settle

    check("循环正常结束且判定成功", result.status == "success", f"实得 {result.status}/{result.detail}")
    check("捕获到奖励文案", result.reward_text == "恭喜获得", f"实得 {result.reward_text!r}")
    check("结算后执行了关闭动作", result.close_rounds >= 1, f"实得 {result.close_rounds}")


def test_focus_parse() -> None:
    """真机 dumpsys 格式回归（Android 8.1 与 9+ 两种排版）。"""
    print("\n[7] 前台包名解析")
    from farmbot.adb import Adb

    android8 = (
        "  Display: mDisplayId=0\n"
        "    mFocusedApp=Token{48cc0de token=Token{1798819 ActivityRecord{96a4b60 u0 "
        "com.huawei.android.launcher/.unihome.UniHomeLauncher t1}}}\n")
    check("Android 8.x mFocusedApp",
          Adb._extract_focus(android8) == "com.huawei.android.launcher/.unihome.UniHomeLauncher",
          f"实得 {Adb._extract_focus(android8)!r}")

    act = ("    mResumedActivity: ActivityRecord{99dacda u0 "
           "com.taobao.taobao/com.taobao.tao.welcome.Welcome t336}\n")
    check("Android 8.x mResumedActivity",
          Adb._extract_focus(act) == "com.taobao.taobao/com.taobao.tao.welcome.Welcome",
          f"实得 {Adb._extract_focus(act)!r}")

    android9 = ("  mCurrentFocus=Window{bdcbe5f u0 com.taobao.taobao/"
                "com.taobao.tao.welcome.Welcome}\n")
    check("Android 9+ mCurrentFocus",
          Adb._extract_focus(android9) == "com.taobao.taobao/com.taobao.tao.welcome.Welcome",
          f"实得 {Adb._extract_focus(android9)!r}")

    check("空输出不误报", Adb._extract_focus("WINDOW MANAGER DISPLAY CONTENTS\n") == "")
    check("不含包名的行不误报",
          Adb._extract_focus("  mCurrentFocus=null\n") == "")


def test_visual_panel() -> None:
    """视觉通道：任务行筛选（用真机面板的 OCR 结构造样本）。"""
    print("\n[8] 视觉通道任务筛选")
    from farmbot import ocr
    from farmbot.panel import scan_panel

    def row(y: int, title: str, sub: str, reward: str, btn: str) -> list[ocr.Box]:
        return [
            ocr.Box(title, 0.9, 162, y, 464, y + 30),
            ocr.Box(sub, 0.8, 162, y + 42, 300, y + 68),
            ocr.Box(reward, 0.8, 320, y + 40, 420, y + 68),
            ocr.Box(btn, 0.8, 572, y + 20, 656, y + 50),
        ]

    boxes: list[ocr.Box] = []
    boxes += row(550, "安装桌面访问小组件(0/1)", "最高3万肥料", "+30000", "去开启")
    boxes += row(670, "看严选推荐商品(3/26)", "浏览15秒得", "+1000", "去完成")
    boxes += row(790, "农场百科问答(0/1)", "每天答题领肥料", "+150~500", "去答题")
    boxes += row(910, "百亿补贴双11大额肥料(0/1)", "页面下单赚5万+肥料", "+57000", "去完成")
    boxes += row(1030, "看严选推荐商品(0/5)", "浏览得奖励", "+400", "去完成")
    boxes += row(1150, "逛逛支付宝芭芭农场(0/1)", "还可领", "+1400", "去完成")

    tasks = scan_panel(boxes)
    keys = [t.type_key for t in tasks]
    check("只识别出 2 条浏览任务", len(tasks) == 2, f"实得 {keys}")
    check("两条同名任务不被合并", "看严选推荐商品/26" in keys and "看严选推荐商品/5" in keys,
          f"实得 {keys}")
    check("安装任务被拒绝", not any("安装" in k for k in keys))
    check("下单任务被拒绝（+57000 诱惑）", not any("百亿补贴" in k for k in keys))
    check("答题任务被拒绝", not any("问答" in k for k in keys))
    check("支付宝跳转被拒绝", not any("支付宝" in k for k in keys))
    check("按奖励降序", tasks[0].reward >= tasks[-1].reward)


def main() -> int:
    Logger(ROOT / "logs", echo=False)
    test_fert_parse()
    test_scan()
    test_safety()
    test_ad_rules()
    test_need_human()
    test_watch_loop()
    test_focus_parse()
    test_visual_panel()
    print("\n" + "=" * 50)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  失败：{f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
