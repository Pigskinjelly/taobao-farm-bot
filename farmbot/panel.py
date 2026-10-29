"""视觉通道：用 OCR 驱动「做任务赚肥料」面板，逐条完成浏览类任务。

适用场景
--------
淘宝农场是 Themis/H5，无障碍读不到底栏和任务面板（DESIGN.md 13.13），
只能靠截图 OCR 拿到文字坐标再点击。

一次任务流程
------------
    读面板 → 选一条「浏览」任务 → 点「去完成」
      → 进入浏览页：需要滑动才计时的一直滑；遇到「搜一搜」就点历史搜索词
      → 读倒计时/结算 → 返回面板 → 复核进度 → 下一条（可重复任务做满次数）

安全边界（硬编码，不可配置放开）
--------------------------------
- 只点按钮文案在白名单里的控件：去完成 / 去观看 / 去浏览；
- 按钮所在行必须出现「浏览」，且不得出现黑名单词
  （安装/下载/下单/支付/支付宝/答题/前往…），否则跳过；
- 从不按坐标盲点：每个点击坐标都来自 OCR 识别到的文字框；
- 出现验证码/滑块/账号异常 → 立即停机。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

from . import ocr
from .adb import Adb, AdbError
from .logger import Logger

# 可以作为"进入任务"的按钮文案（必须完全相等，避免"去开启"之类误命中）
ACTION_WHITELIST = ("去完成", "去观看", "去浏览", "立即领取")

# 行内出现这些词 = 不是浏览任务 / 有风险 → 跳过
# 真机见过的高危行：「安装桌面访问小组件 +30000」「百亿补贴双11大额肥料 页面下单赚5万+肥料 +57000」
# 「逛逛支付宝芭芭农场 +1400」，奖励都比浏览任务高得多，必须靠名单挡住。
ROW_BLOCKLIST = (
    # 安装 / 交易类
    "安装", "下载", "开启", "下单", "购买", "付款", "结算", "充值", "开卡",
    "免单", "领取红包", "预约", "授权", "开通",
    # 平台互动类（会以本账号名义产生动作）
    "答题", "签到", "邀请", "助力", "分享", "关注", "评论", "点赞",
    "登录", "注册", "评价", "发布", "投稿", "上传",
    # 跨 App 跳转（真机见过：去快手app领福利 / 去头条刷热点 / 去微博看热搜 / 前往支付宝）
    "app", "快手", "头条", "微博", "抖音", "小红书", "拼多多", "京东",
    "美团", "饿了么", "支付宝", "微信", "前往",
    "支付",
)

# 只允许这些"纯浏览"语义进入候选（行内出现即可）
ROW_BROWSE_HINTS = ("浏览", "逛逛")

# 面板 / 浏览页特征
PANEL_TEXTS = ("做任务赚肥料", "任务赚肥料")
# 浏览页收尾标志：出现这些才可以返回。
# 注意别放「已完成」「浏览完成」这类泛词——商品卡/进度条上到处是「已完成」，
# 会让脚本进页面 8 秒就误判完成（真机踩过）。
DONE_TEXTS = ("恭喜", "已获得", "领取成功", "奖励已发放", "太棒了",
              "任务完成", "浏览任务完成")
CLOSE_TEXTS = ("关闭", "跳过", "我知道了", "以后再说", "残忍")

# 浏览页右上角的「滑动浏览得肥料」浮标：出现它就必须一直滑，滑满倒计时才算完成
SCROLL_WIDGET_HINTS = ("滑动浏览", "浏览得肥料", "滑动得肥料")

# 「搜一搜」类：页面出现这些说明要搜索；出现"历史搜索"就点里面的词
SEARCH_PAGE_HINTS = ("历史搜索", "大家都在搜", "搜索发现", "猜你想搜", "搜索历史")
SEARCH_CTRL_WORDS = {"搜索", "取消", "删除", "清空", "换一换", "全部", "综合",
                     "销量", "价格", "筛选", "返回", "更多", "历史搜索", "猜你想搜",
                     "大家都在搜", "搜索发现", "搜索历史"}

# 面板加载失败时的重试按钮（真机见过："开小差了，请点击重试"）
RETRY_TEXTS = ("点击重试", "重新加载", "刷新试试", "重试")

COUNTDOWN_RX = r"(\d{1,3})\s*(?:s|S|秒)"
REWARD_RX = r"[+＋]\s*(\d{2,6})"
PROGRESS_RX = r"[（(]\s*(\d+)\s*/\s*(\d+)\s*[)）]"

# 浏览页里最多滑动几次（需要滑动才计时的任务靠它推进度条）
MAX_SCROLLS = 6


@dataclass
class PanelTask:
    title: str
    row_text: str
    reward: int
    seconds: Optional[int]
    button: ocr.Box

    @property
    def base_name(self) -> str:
        return re.sub(PROGRESS_RX, "", self.title).strip()

    @property
    def progress(self) -> tuple[int, int]:
        m = re.search(PROGRESS_RX, self.title)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    @property
    def type_key(self) -> str:
        """任务类型：名字 + 总数。

        同一条任务会显示 (0/26)→(1/26)→…，靠总数把它们认成同一类；
        而「看严选推荐商品(0/5)」和「看严选推荐商品(3/26)」是两条不同任务，
        靠"/总数"区分开。
        """
        total = self.progress[1]
        return f"{self.base_name}/{total}" if total else self.base_name

    @property
    def quota(self) -> int:
        """这类任务最多做几次：有分母就做满，没有就做 1 次。"""
        total = self.progress[1]
        return total if total > 1 else 1

    def describe(self) -> str:
        sec = f"{self.seconds}s" if self.seconds else "滑动"
        return f"{self.title}（浏览{sec}，+{self.reward}）"


def _row_of(boxes: list[ocr.Box], action: ocr.Box, band: int = 30) -> list[ocr.Box]:
    """取与按钮同一行、且在按钮左侧的文字（标题/说明/奖励）。

    实测：标题中心比按钮中心高 20~25px，相邻行间距约 130px，
    band=30 既能覆盖标题，又不会把邻行说明吸进来。
    """
    return [b for b in boxes
            if b is not action
            and abs(b.cy - action.cy) <= band
            and b.right <= action.left + 6]


def _pick_title(row: list[ocr.Box]) -> str:
    """行里挑真正的任务标题（带进度括号的最可靠）。"""
    with_prog = [b for b in row if re.search(PROGRESS_RX, b.text)]
    if with_prog:
        return max(with_prog, key=lambda b: b.width).text
    cands = [b for b in row if len(b.text) >= 4 and not b.text.startswith(("+", "＋"))]
    if cands:
        return max(cands, key=lambda b: len(b.text)).text
    return row[0].text if row else ""


def scan_panel(boxes: list[ocr.Box]) -> list[PanelTask]:
    """从面板 OCR 结果里提取所有可执行的浏览任务（按奖励降序）。"""
    tasks: list[PanelTask] = []
    seen: set[str] = set()
    for b in boxes:
        if b.text.strip() not in ACTION_WHITELIST:
            continue
        row = _row_of(boxes, b)
        if not row:
            continue
        row_text = " ".join(x.text for x in row)
        if "浏览" not in row_text:
            continue
        if any(w in row_text for w in ROW_BLOCKLIST):
            continue
        title = _pick_title(row)
        m_sec = re.search(COUNTDOWN_RX, row_text)
        m_reward = re.search(REWARD_RX, row_text)
        task = PanelTask(
            title=title,
            row_text=row_text,
            reward=int(m_reward.group(1)) if m_reward else 0,
            seconds=int(m_sec.group(1)) if m_sec else None,
            button=b,
        )
        if task.type_key in seen:
            continue
        seen.add(task.type_key)
        tasks.append(task)
    tasks.sort(key=lambda t: -t.reward)
    return tasks


def is_panel(boxes: list[ocr.Box]) -> bool:
    return bool(ocr.find(boxes, PANEL_TEXTS))


def _is_search_page(boxes: list[ocr.Box]) -> bool:
    """是否是"待搜索"页（需要点历史词），而不是搜索结果页。

    真机教训：搜索结果页顶部仍保留「Q搜索宝贝」占位符，
    早期用"有搜索框就算搜索页"会把结果页误判成搜索页，
    于是反复点历史词、从不滑动，进度永远 0/3。
    正确判据：**有历史/发现区 且 没有「滑动浏览得肥料」浮标**。
    """
    if ocr.find(boxes, SCROLL_WIDGET_HINTS):
        return False
    return bool(ocr.find(boxes, SEARCH_PAGE_HINTS))


def _pick_history_keyword(boxes: list[ocr.Box]) -> Optional[ocr.Box]:
    """在搜索页里挑一个历史搜索词点进去。

    规则：优先取「历史搜索」标签下方的短词；没有标签就取页面上部、
    长度 2~10、不是控件词、不含奖励数字的文字框。
    """
    label = ocr.find(boxes, ("历史搜索", "搜索历史", "猜你想搜", "大家都在搜", "搜索发现"))
    floor = label[0].bottom if label else 0
    cands: list[ocr.Box] = []
    for b in boxes:
        t = b.text.strip()
        if not (2 <= len(t) <= 10):
            continue
        if t in SEARCH_CTRL_WORDS:
            continue
        if re.search(r"[+＋]\d", t):
            continue
        if b.top < floor:
            continue
        if b.top > (floor + 420):
            continue
        cands.append(b)
    if not cands:
        return None
    cands.sort(key=lambda b: (b.top // 40, b.left))
    return cands[0]


def _is_home_page(boxes: list[ocr.Box]) -> bool:
    """淘宝首页特征（底部导航 + 顶部标签）。"""
    return bool(ocr.find(boxes, ("关注", "推荐", "购物车", "我的淘宝")))


def _find_farm_entry(boxes: list[ocr.Box]) -> Optional[ocr.Box]:
    """在首页找「芭芭农场」入口。

    真机：金刚区图标文字常被 OCR 拆成「芭芭」+「农场」两行，
    所以不能只匹配完整四字；用"含芭芭/农场 + 位于图标区"来兜。
    """
    full = ocr.find(boxes, ("芭芭农场",))
    if full:
        return full[0]
    cands = [b for b in boxes
             if ("芭芭" in b.text or "农场" in b.text)
             and 150 < b.top < 700
             and len(b.text) <= 8]
    if not cands:
        return None
    # 同一行里「芭芭」「农场」两块的左边那个就是图标文字
    cands.sort(key=lambda b: (b.top // 30, -b.left))
    return cands[0]


class VisualBot:
    """用截图 + OCR 完成浏览任务。所有点击都来自识别到的文字框。"""

    def __init__(self, adb: Adb, log: Logger, max_tasks: int = 10,
                 shot_dir=None, dry_run: bool = False, max_scrolls: int = MAX_SCROLLS,
                 cooldown_seconds: int = 300, cooldown_rounds: int = 6, u2dev=None):
        self.adb = adb
        self.log = log
        self.max_tasks = max_tasks
        self.shot_dir = shot_dir
        self.dry_run = dry_run
        self.max_scrolls = max_scrolls
        # 限流后的冷却策略：回淘宝首页 → 等 N 秒 → 重进农场 → 继续
        self.cooldown_seconds = cooldown_seconds
        self.cooldown_rounds = cooldown_rounds
        self.cooldowns_used = 0
        self.restarts_used = 0
        self.max_restarts = 2
        # 可选：uiautomator2 常驻服务，截图 0.23s / 点击 0.18s（adb 是 1.0s / 1.3s）
        self.u2 = u2dev
        self.done: list[str] = []
        self.failed: list[str] = []
        self.skipped: list[str] = []
        self._shot_n = 0
        self._err_retries = 0
        self._stop_reason = ""

    # ------------------------------------------------------------ 基础动作

    def shot(self) -> bytes:
        """截图：优先 u2 常驻服务（~0.23s），失败回退 adb（~1.0s）。"""
        if self.u2 is not None:
            try:
                import io
                buf = io.BytesIO()
                self.u2.screenshot().save(buf, format="PNG")
                data = buf.getvalue()
                if data.startswith(b"\x89PNG") and b"IEND" in data[-64:]:
                    return data
            except Exception:
                self.u2 = None        # 服务挂了就不再重试，直接用 adb
        return self.adb.screencap()

    def look(self, tag: str = "", roi=None, scale: float = 1.0) -> list[ocr.Box]:
        last: Optional[Exception] = None
        for _ in range(2):
            png = self.shot()
            if self.shot_dir and tag:
                self._shot_n += 1
                p = self.shot_dir / f"{time.strftime('%H%M%S')}_{self._shot_n:03d}_{tag}.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(png)
            try:
                return ocr.read(png, roi=roi, scale=scale)
            except Exception as e:          # noqa: BLE001 - 截断/解码失败都重试
                last = e
                time.sleep(0.8)
        raise AdbError(f"OCR 识别失败：{last}")

    def look_fast(self, tag: str = "") -> list[ocr.Box]:
        """只看顶部区域（倒计时/「滑动浏览得肥料」浮标/奖励文案），比全屏快 3~4 倍。"""
        return self.look(tag=tag, roi=(0.0, 0.0, 1.0, 0.22), scale=0.75)

    def tap_box(self, box: ocr.Box, why: str) -> bool:
        if self.dry_run:
            self.log.step("V", f"[dry-run] 本应点击「{box.text}」@({box.cx},{box.cy})", True)
            return True
        try:
            if self.u2 is not None:
                try:
                    self.u2.click(box.cx, box.cy)
                except Exception:
                    self.u2 = None
                    self.adb.tap(box.cx, box.cy)
            else:
                self.adb.tap(box.cx, box.cy)
        except AdbError as e:
            self.log.step("V", f"点击「{box.text}」失败：{e}", False)
            return False
        self.log.step("V", f"点击「{box.text}」@({box.cx},{box.cy})｜{why}", True)
        time.sleep(1.2)
        return True

    def scroll(self, why: str = "") -> None:
        """自然、必要的一次上滑：需要滑动才计时的任务靠它推进度条。"""
        w, h = self.adb.screen_size()
        try:
            if not self.dry_run:
                # 一次"慢滑"覆盖约 2.2 秒连续滚动：adb input 每次往返固定 ~1.3s，
                # 短滑 30 次要 40s，长滑 8 次只要 ~22s，浏览时长还更足
                self.adb.swipe(w // 2, int(h * 0.82), w // 2, int(h * 0.30), duration_ms=2200)
        except AdbError as e:
            self.log.step("V", f"滑动失败：{e}", False)
        time.sleep(0.55)

    # ------------------------------------------------------------ 面板

    def recover_if_error(self, boxes: list[ocr.Box]) -> bool:
        """面板报「开小差了/人数较多」时点重试，最多 2 次。

        真机上这两种是平台侧的拥塞提示，连点没用，点多了还会更糟，
        所以有次数上限；调用方据此决定是否停机等待。
        """
        retry = ocr.find(boxes, RETRY_TEXTS)
        if not retry:
            self._err_retries = 0
            return False
        if self._err_retries >= 2:
            return False
        self._err_retries += 1
        if self.tap_box(retry[0], f"面板加载失败，点重试（{self._err_retries}/2）"):
            time.sleep(4.0)
            return True
        return False

    @staticmethod
    def panel_error(boxes: list[ocr.Box]) -> Optional[str]:
        hit = ocr.find(boxes, ("开小差", "稍后再试", "人数较多", "晚点再来", "系统繁忙"))
        return hit[0].text if hit else None

    def restart_taobao(self) -> bool:
        """重启淘宝（force-stop → 重新拉起）。

        用在"首页找不到农场入口"的情况：新账号的首页金刚区会被个性化，
        入口可能不在第一屏、甚至不出现；重启后一般会回到默认布局。
        """
        from . import config as C

        self.restarts_used += 1
        self.log.raw(f"↻ 未找到农场入口，重启淘宝（第 {self.restarts_used} 次）")
        try:
            self.adb.shell("am", "force-stop", C.APP_PACKAGE)
        except AdbError as e:
            self.log.raw(f"  force-stop 失败：{e}")
        time.sleep(2.0)
        try:
            self.adb.start_app(C.APP_PACKAGE)
        except AdbError as e:
            self.log.raw(f"  启动失败：{e}")
            return False

        deadline = time.time() + C.APP_START_TIMEOUT + 10
        while time.time() < deadline:
            time.sleep(2.0)
            try:
                if C.APP_PACKAGE in self.adb.current_focus():
                    time.sleep(C.PAGE_LOAD_WAIT)
                    self.log.raw("  淘宝已重启完成")
                    return True
            except AdbError:
                break
        return False

    def open_panel(self) -> bool:
        """从任意页面回到「做任务赚肥料」面板。

        分三种情况处理：
          1. 已在面板 → 直接返回；
          2. 在农场页但底栏还没渲染（真机重启后常见「正在打开」）→ 等它出来再点「集肥料」；
          3. 在淘宝首页 → 点「芭芭农场」入口；找不到入口就重启淘宝；
        农场页的标题也叫「芭芭农场」，所以入口识别只在首页才启用。
        """
        from . import config as C

        boxes = self.look("farm")
        for attempt in range(10):
            if is_panel(boxes):
                if self.recover_if_error(boxes):
                    boxes = self.look("panel_retry")
                    continue
                return True

            hit = ocr.find(boxes, ("集肥料",))
            if hit:
                if not self.tap_box(hit[0], "进入集肥料面板"):
                    return False
                time.sleep(2.5)
                boxes = self.look("panel")
                continue

            # 农场页但底栏还在加载：等一下再看
            farm_like = ocr.find(boxes, ("施肥", "一起种", "领红包", "我的收获", "阳光农场"))
            if farm_like and attempt < 4:
                self.log.raw("  农场页底栏还在加载（未出现「集肥料」），稍等再试")
                time.sleep(2.5)
                boxes = self.look("farm_wait")
                continue

            # 只有首页的「芭芭农场」才是入口（农场页的同名文字是标题）
            if _is_home_page(boxes):
                entry = _find_farm_entry(boxes)
                if entry is not None:
                    if not self.tap_box(entry, "从首页进入芭芭农场"):
                        return False
                    time.sleep(4.0)
                    boxes = self.look("farm_after_entry")
                    continue
                self.log.step("V", f"在淘宝首页但没找到农场入口（第 {attempt + 1}/10 次）", False)
                if self.restarts_used < self.max_restarts:
                    if self.restart_taobao():
                        boxes = self.look("after_restart")
                        continue
                time.sleep(2.0)
                boxes = self.look("home_retry")
                continue

            popup = ocr.find(boxes, ("明日7点后记得来领", "提醒我明天领", "今天已领取"))
            if popup:
                self.log.step("V", f"被浮层遮挡：「{popup[0].text}」，需人工关闭", False)
                return False

            self.log.step("V", f"未看到面板（第 {attempt + 1}/10 次），返回上一层", False)
            self.adb.back()
            time.sleep(1.5)
            boxes = self.look("farm_retry")
        return is_panel(boxes)

    def _back_to_panel(self, max_back: int = 3) -> bool:
        for _ in range(max_back):
            if is_panel(self.look("back_check")):
                return True
            self.adb.back()
            time.sleep(1.3)
        return is_panel(self.look("back_final"))

    # ------------------------------------------------------------ 限流冷却

    def cooldown_and_reenter(self) -> bool:
        """被限流后：回淘宝首页 → 等 cooldown_seconds → 重进农场 → 回到面板。

        这是"休息一会儿再来"，不是绕过限制：
        不换账号、不换网络、不改设备信息，只是等平台自己恢复。
        """
        from . import config as C

        self.cooldowns_used += 1
        self.log.raw("")
        self.log.raw(f"⚠ 平台限流：回淘宝首页冷却 {self.cooldown_seconds}s"
                     f"（第 {self.cooldowns_used}/{self.cooldown_rounds} 次）")
        try:
            self.adb.key(3)                       # HOME：先离开淘宝
            time.sleep(2.0)
            self.adb.start_app(C.APP_PACKAGE)     # 重新拉起 → 淘宝首页
        except AdbError as e:
            self.log.raw(f"  回淘宝首页失败：{e}")
            return False
        time.sleep(6.0)                           # 等开屏/首页渲染

        left = self.cooldown_seconds
        while left > 0:
            step = min(60, left)
            time.sleep(step)
            left -= step
            if left > 0:
                self.log.raw(f"  冷却中…剩余 {left}s")

        for attempt in range(3):
            boxes = self.look("after_cooldown")
            entry = ocr.find(boxes, ("芭芭农场",))
            if entry is None:
                self.log.raw(f"  首页没看到入口（第 {attempt + 1}/3 次），下拉找一找")
                try:
                    self.adb.swipe(360, 1200, 360, 700, 400)
                except AdbError:
                    pass
                time.sleep(2.5)
                continue
            self.tap_box(entry[0], "冷却结束，重进农场")
            time.sleep(4.0)
            if self.open_panel():
                self._err_retries = 0
                self.log.raw("✓ 已重新进入「做任务赚肥料」面板，继续执行剩余任务")
                return True
            self.log.raw(f"  重进面板未成功（第 {attempt + 1}/3 次），稍候再试")
            time.sleep(4.0)
        return False

    # ------------------------------------------------------------ 浏览页

    def _browse(self, task: PanelTask) -> str:
        """在浏览页完成任务。返回 success / blocked / need_human / failed。

        真机经验（2026-10-29）：
        1. 看完**不会自动返回**，必须主动按返回；
        2. 浏览页右上角常有「滑动浏览得肥料」浮标 + 15 秒倒计时——
           **必须持续滑动**把 15 秒读完，光等不动是不会计时的；
        3. 读不到倒计时时按滑动次数兜底；
        4. 「搜一搜」类要先点一个历史搜索词，页面才会出现可浏览内容。
        """
        budget = (task.seconds or 20) + 120
        deadline = time.time() + budget
        scrolls = 0
        scroll_cap = self.max_scrolls
        saw_widget = False
        searched = False
        full = True          # 交替全屏/顶部局部识别，省掉一半 OCR 开销

        while time.time() < deadline:
            boxes = self.look("browse") if full else self.look_fast("browse_fast")
            full = not full

            human = ocr.find(boxes, ("验证码", "滑块", "安全验证", "账号异常", "环境异常"))
            if human:
                self.log.step("V", f"出现人工验证页「{human[0].text}」，立即停止", False)
                return "need_human"

            if is_panel(boxes):
                return "success"

            done = ocr.find(boxes, DONE_TEXTS)
            if done:
                self.log.step("V", f"识别到收尾文案「{done[0].text}」，返回面板", True)
                break

            # 「搜一搜」：点一个历史搜索词，再去浏览结果页（只点一次，避免打转）
            if _is_search_page(boxes):
                if searched:
                    self.log.step("V", "点了历史词仍停在搜索页，跳过该任务", False)
                    self._back_to_panel()
                    return "blocked"
                kw = _pick_history_keyword(boxes)
                if kw is None:
                    self.log.step("V", "搜索页没有可点的历史搜索词，跳过", False)
                    self._back_to_panel()
                    return "blocked"
                self.tap_box(kw, "点击历史搜索词（代替手动输入）")
                searched = True
                time.sleep(2.2)
                continue

            # 「滑动浏览得肥料」浮标：出现就说明必须靠滑动计时，加大滑动预算
            widget = ocr.find(boxes, SCROLL_WIDGET_HINTS)
            if widget:
                if not saw_widget:
                    self.log.step("V", f"发现「{widget[0].text}」浮标，持续滑动计入浏览时长", True)
                    saw_widget = True
                scroll_cap = max(scroll_cap, 8)

            # 读倒计时（浮标旁的数字／「跳过 5s」／「剩余12秒」）
            left = None
            for _b, m in ocr.find_regex(boxes, COUNTDOWN_RX):
                v = int(m.group(1))
                if 0 <= v <= 300:
                    left = v if left is None else min(left, v)
            if left is not None:
                self.log.raw(f"    浏览页倒计时剩余 {left}s（已滑 {scrolls} 次）")
                if left <= 1:
                    self.log.step("V", "倒计时归零", True)
                    break
                if saw_widget and scrolls < scroll_cap:
                    # 「滑动浏览得肥料」只认滑动时长，别干等着
                    for _i in range(2):
                        if scrolls >= scroll_cap:
                            break
                        self.scroll()
                        scrolls += 1
                    continue

            # 有浮标 → 边滑边等（滑动才是有效浏览）；没浮标 → 滑几批再等
            if scrolls < scroll_cap:
                batch = 2 if saw_widget else 2
                for _i in range(batch):
                    if scrolls >= scroll_cap:
                        break
                    self.scroll()
                    scrolls += 1
                self.log.raw(f"    滑动浏览 {scrolls}/{scroll_cap}")
                continue

            if left is None:
                break

            # 滑满了：不再死等（浮标数字常常不随滑动递减，等下去只会耗满 deadline），
            # 直接回面板，由 _verify 用进度是否前进来判断成败
            self.log.raw("    滑动次数已用满，返回面板复核")
            break

        if self._back_to_panel():
            return "success"
        return "failed"

    def _dismiss(self, boxes: list[ocr.Box]) -> None:
        for _ in range(2):
            label = ocr.find(boxes, CLOSE_TEXTS)
            if not label:
                return
            self.tap_box(label[0], "关闭奖励弹窗")
            time.sleep(1.2)

    # ------------------------------------------------------------ 单条任务

    def _verify(self, task: PanelTask) -> bool:
        """回到面板后复核：进度前进或该任务消失才算成功。

        不能只看 scan_panel 结果——面板没读全时它是空列表，会被当成"任务消失"
        误判成功（真机踩过：8 秒就报完成）。所以这里按任务名直接找文字行。
        """
        boxes = self.look("verify")
        if not is_panel(boxes):
            if not self._back_to_panel():
                return False
            boxes = self.look("verify2")
            if not is_panel(boxes):
                return False

        key = task.base_name[:6]
        total = task.progress[1]
        for b in boxes:
            if not key or key not in b.text:
                continue
            m = re.search(PROGRESS_RX, b.text)
            if not m:
                continue
            # 必须是同一类任务：名字相同 **且** 总数相同
            # （「发现精选好物(1/5)」和「发现精选好物(16/26)」是两条任务）
            if total and int(m.group(2)) != total:
                continue
            cur = int(m.group(1))
            self.log.step("V", f"复核进度：{cur}/{m.group(2)}（原 {task.progress[0]}/{total}）",
                          cur > task.progress[0])
            return cur > task.progress[0]
        return True                   # 整行都找不到 → 任务已完成并移除

    def run_task(self, task: PanelTask) -> str:
        t0 = time.time()
        self.log.step("V", f"开始任务：{task.describe()}", True)
        res = "failed"
        if not self.tap_box(task.button, f"进入「{task.title}」"):
            res = "failed"
        else:
            t1 = time.time()
            status = self._browse(task)
            browse_cost = time.time() - t1
            if status in ("need_human", "blocked"):
                res = status
            elif not self._back_to_panel():
                self.log.step("V", "无法回到任务面板", False)
                res = "failed"
            else:
                res = "success" if self._verify(task) else "failed"
            self.log.raw(f"  ⏱ 浏览页 {browse_cost:.1f}s")
        self.log.raw(f"  ⏱ 本条总计 {time.time() - t0:.1f}s → {res}")
        return res

    # ------------------------------------------------------------ 主循环

    def ensure_taobao(self) -> bool:
        """确保淘宝在前台；不在就拉起来（手机停桌面/锁屏后常见）。"""
        from . import config as C

        focus = ""
        try:
            focus = self.adb.current_focus()
        except AdbError:
            pass
        if C.APP_PACKAGE in focus:
            return True
        self.log.raw(f"淘宝不在前台（{focus or '未知'}），正在启动…")
        try:
            self.adb.start_app(C.APP_PACKAGE)
        except AdbError as e:
            self.log.step("V", f"启动淘宝失败：{e}", False)
            return False
        deadline = time.time() + C.APP_START_TIMEOUT
        while time.time() < deadline:
            time.sleep(2.0)
            try:
                if C.APP_PACKAGE in self.adb.current_focus():
                    self.log.raw("淘宝已在前台")
                    time.sleep(C.PAGE_LOAD_WAIT)
                    return True
            except AdbError:
                break
        return False

    def run(self) -> dict:
        self.log.raw("视觉通道：OCR 驱动，逐条完成浏览任务")
        if not self.ensure_taobao():
            return self._summary("无法启动/切到淘宝")
        if not self.open_panel():
            return self._summary("无法进入任务面板")

        rounds: dict[str, int] = {}       # 每类任务已完成次数
        attempts: dict[str, int] = {}     # 每类任务当前连续失败次数
        need_human = False

        while len(self.done) < self.max_tasks:
            boxes = self.look("scan")
            err = self.panel_error(boxes)
            if err and not self.recover_if_error(boxes):
                # 限流：回首页冷却，再回来接着干（不绕过、不换号）
                if (not self.dry_run and self.cooldowns_used < self.cooldown_rounds
                        and self.cooldown_and_reenter()):
                    continue
                self._stop_reason = f"面板提示「{err}」，冷却 {self.cooldowns_used} 次仍未恢复"
                self.log.step("V", self._stop_reason, False)
                break
            if is_panel(boxes) and self.recover_if_error(boxes):
                boxes = self.look("scan_recover")
            if not is_panel(boxes):
                if not self._back_to_panel():
                    break
                boxes = self.look("scan2")

            # 记录本屏所有"不可执行"的按钮，便于汇报
            for b in boxes:
                if b.text.strip() in ("去开启", "去答题", "前往", "去签到"):
                    if b.text.strip() not in self.skipped:
                        self.skipped.append(b.text.strip())

            pending = [t for t in scan_panel(boxes)
                       if rounds.get(t.type_key, 0) < t.quota
                       and attempts.get(t.type_key, 0) < 2]
            if not pending:
                # OCR 偶发漏行：再看一次确认，避免把"没读全"当成"没任务"
                time.sleep(1.2)
                boxes = self.look("scan_retry")
                pending = [t for t in scan_panel(boxes)
                           if rounds.get(t.type_key, 0) < t.quota
                           and attempts.get(t.type_key, 0) < 2]
            if not pending:
                self.log.step("V", "面板上已没有可执行的浏览任务", True)
                self._stop_reason = "全部浏览任务已完成（面板无剩余可执行项）"
                break

            task = pending[0]
            task_no = rounds.get(task.type_key, 0) + 1
            self.log.raw(f"  ▸ 第 {task_no}/{task.quota} 次：{task.type_key}")

            status = self.run_task(task)

            if status == "need_human":
                need_human = True
                self.log.step("V", "命中风控验证，停止", False)
                break
            if status == "blocked":
                attempts[task.type_key] = 2
                if task.type_key not in self.skipped:
                    self.skipped.append(task.type_key)
                self.log.step("V", f"跳过（需人工操作）：{task.type_key}", True)
                continue
            if status == "success":
                rounds[task.type_key] = task_no
                attempts[task.type_key] = 0
                self.done.append(f"{task.base_name}({task_no}/{task.quota})")
                self.log.step("V", f"任务完成（累计 {len(self.done)} 次）："
                                   f"{task.base_name} {task_no}/{task.quota}", True)
                # 轮间隔：连续高频操作会被服务端限流（真机见过"开小差了"）
                time.sleep(3.0)
                continue

            attempts[task.type_key] = attempts.get(task.type_key, 0) + 1
            if attempts[task.type_key] >= 2:
                if task.type_key not in self.failed:
                    self.failed.append(task.type_key)
                self.log.step("V", f"连续 2 次失败，跳过：{task.type_key}", False)
            else:
                self.log.step("V", f"未确认成功，重试：{task.type_key}", False)

        reason = ("命中人工验证" if need_human
                  else (self._stop_reason or "浏览任务已处理完（无剩余可执行项）"))
        return self._summary(reason)

    def _summary(self, reason: str) -> dict:
        self.log.raw("")
        self.log.raw("=" * 56)
        self.log.raw("视觉通道执行摘要")
        self.log.raw(f"  完成浏览次数：{len(self.done)}")
        for t in self.done:
            self.log.raw(f"    ✓ {t}")
        if self.failed:
            self.log.raw(f"  失败：{len(self.failed)} → {'、'.join(self.failed)}")
        self.log.raw(f"  跳过（非浏览/需人工）：{'、'.join(self.skipped) or '无'}")
        self.log.raw(f"  限流冷却次数：{self.cooldowns_used}")
        self.log.raw(f"  结束原因：{reason}")
        self.log.raw("=" * 56)
        return {"done": self.done, "failed": self.failed,
                "skipped": self.skipped, "reason": reason}
