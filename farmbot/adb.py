"""adb 驱动层。

所有对设备的原始操作收敛在这里：定位 adb、设备发现、截图、
注入点击/滑动/返回、抓取界面树。上层业务代码不直接拼 adb 命令。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional


class AdbError(RuntimeError):
    """adb 调用失败。"""


# 从 dumpsys 任意一行里捞 "包名/Activity"。
# 兼容这些真实格式：
#   mCurrentFocus=Window{bdcbe5f u0 com.taobao.taobao/com.taobao.tao.welcome.Welcome}
#   mResumedActivity: ActivityRecord{99dacda u0 com.taobao.taobao/com.taobao.tao.welcome.Welcome t336}
#   mFocusedApp=Token{... u0 com.huawei.android.launcher/.unihome.UniHomeLauncher t1}
# 关键：不能按空格取固定下标（会拿到 "u0" 或 "t336}"），必须按 "pkg/Activity" 形状匹配。
_FOCUS_RX = re.compile(r"([A-Za-z][A-Za-z0-9_.]*)/([A-Za-z0-9_.$]+)")
_FOCUS_KEYS = ("mCurrentFocus", "mFocusedApp", "mResumedActivity", "mFocusedActivity")


def _candidate_paths() -> list[Path]:
    root = Path(__file__).resolve().parent.parent          # taobao_farm/
    cands: list[Path] = [
        root / "tools" / "platform-tools" / "adb.exe",
        root / "tools" / "platform-tools" / "adb",
    ]
    env = os.environ.get("ADB_PATH")
    if env:
        cands.append(Path(env))
    found = shutil.which("adb")
    if found:
        cands.append(Path(found))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    home = Path.home()
    cands += [
        home / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
        Path("/c/platform-tools/adb.exe"),
        Path("/d/platform-tools/adb.exe"),
        Path("/c/Android/platform-tools/adb.exe"),
        Path("/d/Android/platform-tools/adb.exe"),
    ]
    return cands


def find_adb() -> str:
    for p in _candidate_paths():
        try:
            if p.is_file():
                return str(p)
        except OSError:
            continue
    raise AdbError(
        "找不到 adb。请任选其一：\n"
        "  1) 把 platform-tools 解压到 taobao_farm/tools/platform-tools/\n"
        "  2) 设置环境变量 ADB_PATH 指向 adb.exe\n"
        "  3) 把 platform-tools 加入系统 PATH"
    )


class Adb:
    def __init__(self, serial: Optional[str] = None, adb_path: Optional[str] = None):
        self.bin = adb_path or find_adb()
        self.serial = serial
        # 注意：screencap 走 `exec-out`，是二进制安全通道，不能做 CRLF 修正。
        # 早期版本对输出做 replace(b"\r\n", b"\n")，会把 PNG 头
        # 89 50 4E 47 0D 0A 1A 0A 改坏成 ...0A 1A 0A，导致所有截图无法打开。
        # （真机验证于 2026-10-29，见 DESIGN.md 第 13 章）
        self._png_norm = False

    # ------------------------------------------------------------ 底层

    def _base(self) -> list[str]:
        cmd = [self.bin]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def _run(self, args: list[str], timeout: float = 30.0, binary: bool = False):
        cmd = self._base() + args
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb 超时（{timeout}s）：{' '.join(args)}") from e
        if binary:
            if proc.returncode != 0:
                raise AdbError(
                    f"adb 失败（{proc.returncode}）：{' '.join(args)} :: "
                    f"{proc.stderr.decode('utf-8', 'replace')[:200]}"
                )
            return proc.stdout
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        if proc.returncode != 0:
            raise AdbError(f"adb 失败（{proc.returncode}）：{' '.join(args)} :: {err[:300]}")
        return out + (("\n" + err) if err.strip() else "")

    def shell(self, *args: str, timeout: float = 30.0) -> str:
        return self._run(["shell", *args], timeout=timeout)

    # ------------------------------------------------------------ 设备

    def list_devices(self) -> list[dict]:
        out = self._run(["devices", "-l"])
        devices = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            devices.append({"serial": parts[0], "state": parts[1], "raw": line})
        return devices

    def ensure_device(self) -> str:
        devices = self.list_devices()
        ready = [d for d in devices if d["state"] == "device"]
        if not ready:
            detail = "、".join(f"{d['serial']}({d['state']})" for d in devices) or "无任何设备"
            raise AdbError(
                f"没有可用设备（{detail}）。请确认：手机已开启「开发者选项 → USB 调试」，"
                "USB 已连接并授权，`adb devices` 能看到 device 状态。"
            )
        if self.serial:
            if not any(d["serial"] == self.serial for d in ready):
                raise AdbError(f"指定设备 {self.serial} 不可用")
            return self.serial
        self.serial = ready[0]["serial"]
        return self.serial

    def screen_size(self) -> tuple[int, int]:
        out = self.shell("wm", "size")
        for token in out.replace("\r", " ").split():
            if "x" in token and token[0].isdigit():
                w, h = token.split("x")[:2]
                try:
                    return int(w), int(h)
                except ValueError:
                    continue
        return 1080, 2400

    @staticmethod
    def _extract_focus(text: str) -> str:
        """从 dumpsys 输出里提取 "包名/Activity"，找不到返回空串。"""
        for line in text.splitlines():
            if not any(k in line for k in _FOCUS_KEYS):
                continue
            m = _FOCUS_RX.search(line)
            if m:
                return f"{m.group(1)}/{m.group(2)}"
        return ""

    def current_focus(self) -> str:
        """返回当前焦点窗口，形如 com.taobao.taobao/.MainActivity

        不同 Android 版本把焦点信息放在不同 dump 里：
          - Android 9+：dumpsys window displays 含 mCurrentFocus
          - Android 8.x（本机测试机）：window displays 里没有，
            只有 dumpsys activity activities 的 mResumedActivity 可用
        所以按"便宜到贵"依次试，任一命中即返回。
        """
        for args in (("window", "displays"),
                     ("activity", "activities"),
                     ("activity", "top"),
                     ("window",)):
            try:
                out = self.shell("dumpsys", *args, timeout=20)
            except AdbError:
                continue
            focus = self._extract_focus(out)
            if focus:
                return focus
        return ""

    # ------------------------------------------------------------ 感知

    def screencap(self, save_path: Optional[Path] = None) -> bytes:
        """截图并校验完整性。

        真机踩过：`exec-out screencap -p` 偶发被截断，返回半张图，
        下游 PIL 会报 "image file is truncated"。这里做 PNG 头尾校验 + 重试。
        """
        last_err = ""
        for attempt in range(3):
            data = self._run(["exec-out", "screencap", "-p"], timeout=30, binary=True)
            if self._png_norm:
                data = data.replace(b"\r\n", b"\n")
            ok = (data.startswith(b"\x89PNG") and len(data) > 2048
                  and b"IEND" in data[-64:])
            if ok:
                if save_path is not None:
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    save_path.write_bytes(data)
                return data
            last_err = f"截图不完整（{len(data)} 字节，第 {attempt + 1}/3 次）"
            time.sleep(0.8)
        raise AdbError(last_err or "截图失败")

    def dump_xml(self, retries: int = 1) -> str:
        """抓取 uiautomator 界面树。失败自动重试。

        真机踩过的坑（Android 8.1 + 淘宝 Themis 页，见 DESIGN.md 第 13 章）：
        页面持续动画时，AOSP 的 AccessibilityNodeInfoDumper.dumpNodeRec 会在
        遍历中拿到 null 子节点并抛 NullPointerException，进程被直接杀掉
        （adb 返回 137、落盘文件 0 字节）。这不是超时，重试仍是唯一可行手段，
        但必须：(1) 重试之间留间隔让页面喘口气；(2) 后几次换 --compressed
        减少遍历节点；(3) 把失败原因说清楚，交上层决定停机而不是继续点。
        """
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            args = ["uiautomator", "dump"]
            if attempt >= 1:            # 第一次完整 dump，之后用压缩模式减少遍历
                args.append("--compressed")
            args.append("/sdcard/farmbot_ui.xml")
            try:
                # 真机上进程通常 12s 左右被 NPE 杀掉，超时给 15s 足够，
                # 给太长只会白白多等
                self.shell(*args, timeout=15)
                xml = self.shell("cat", "/sdcard/farmbot_ui.xml", timeout=20)
                if "<hierarchy" in xml and len(xml) > 200:
                    return xml
                last_err = AdbError("dump 输出为空（页面持续动画导致 dumpNodeRec 崩溃）")
            except AdbError as e:
                last_err = e
            time.sleep(0.8 + attempt * 0.6)
        raise AdbError(f"界面树抓取失败（已重试 {retries + 1} 次）：{last_err}")

    # ------------------------------------------------------------ 操作

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(int(x)), str(int(y)))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 320) -> None:
        self.shell(
            "input", "swipe",
            str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(duration_ms)),
        )

    def long_press(self, x: int, y: int, duration_ms: int = 900) -> None:
        self.shell("input", "swipe", str(int(x)), str(int(y)), str(int(x)), str(int(y)),
                   str(int(duration_ms)))

    def back(self) -> None:
        self.shell("input", "keyevent", "4")

    def key(self, code: int) -> None:
        self.shell("input", "keyevent", str(code))

    def input_ascii(self, text: str) -> None:
        """只能输入 ASCII。中文需要用输入法（见 README）。"""
        safe = text.replace(" ", "%s")
        self.shell("input", "text", safe)

    def start_app(self, package: str) -> None:
        self.shell(
            "monkey", "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1",
            timeout=25,
        )


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()
