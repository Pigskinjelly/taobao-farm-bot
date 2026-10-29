"""可选感知后端：uiautomator2（ATX server）。

为什么需要它
------------
Android 8.x 自带的 `uiautomator dump` 在遍历视图树时，如果页面正在动画，
`AccessibilityNodeInfoDumper.dumpNodeRec` 会拿到 null 子节点并抛
NullPointerException，进程被系统杀掉（adb 返回 137，落盘 0 字节）。
真机实测（荣耀 8X / Android 8.1 / 淘宝 10.68.10）在农场页必现。

uiautomator2 的 server 走同一套 UiAutomation，但 dump 实现有 null 保护，
实测同样页面 0.4s 返回、不崩。

已知局限
--------
淘宝 Themis/H5 页面只把**部分**节点暴露给无障碍：农场页能看到
「再施肥52.70%就结出果实啦」之类，但底部「集肥料/施肥」那排和
「做任务赚肥料」面板内容读不到（多次验证，见 DESIGN.md 13.13）。
所以本模块解决的是"崩溃"，不解决"H5 覆盖不全"——后者需要 OCR 兜底。

依赖是可选的：没装 uiautomator2 时本模块 import 失败，调用方自动回退到
adb + uiautomator dump。
"""
from __future__ import annotations

import os
from typing import Optional


class U2Dumper:
    """把 uiautomator2 的 dump 包装成和 `Adb.dump_xml()` 相同的接口。"""

    def __init__(self, serial: Optional[str] = None, max_depth: int = 200):
        # adbutils 默认找 PATH 里的 adb；本项目 adb 放在 tools/platform-tools，
        # 必须在 import 之前告诉它，否则连不上设备。
        if "ADBUTILS_ADB_PATH" not in os.environ:
            from .adb import find_adb
            os.environ["ADBUTILS_ADB_PATH"] = find_adb()

        import uiautomator2 as u2     # 延迟导入：没装也不影响主流程
        self._u2 = u2
        self.device = u2.connect(serial) if serial else u2.connect()
        try:
            self.device.settings["max_depth"] = max_depth
        except Exception:
            pass                          # 老版本没有 settings，忽略
        self.max_depth = max_depth
        self.name = "uiautomator2"

    @property
    def serial(self) -> str:
        try:
            return self.device.serial
        except Exception:
            return ""

    def dump_xml(self) -> str:
        """返回 hierarchy XML；失败抛异常，由调用方决定回退。"""
        xml = self.device.dump_hierarchy(max_depth=self.max_depth)
        if "<hierarchy" not in xml:
            raise RuntimeError("u2 dump 返回内容不含 hierarchy")
        return xml

    def screencap(self) -> bytes:
        """u2 的截图（PNG bytes），用于和 adb 截图做交叉校验。"""
        import io
        buf = io.BytesIO()
        self.device.screenshot().save(buf, format="PNG")
        return buf.getvalue()

    def alive(self) -> bool:
        try:
            self.device.info
            return True
        except Exception:
            return False


def build_device(serial: Optional[str] = None):
    """返回 uiautomator2 的 Device（常驻服务）。

    给视觉通道用作"快速输入/截图后端"：截图约 0.23s、点击约 0.18s，
    而 `adb exec-out screencap` 约 1.0s、`adb shell input tap` 约 1.3s
    （每次 input 都要在设备上新起一个 java 进程）。
    """
    if "ADBUTILS_ADB_PATH" not in os.environ:
        from .adb import find_adb
        os.environ["ADBUTILS_ADB_PATH"] = find_adb()

    import uiautomator2 as u2
    dev = u2.connect(serial) if serial else u2.connect()
    dev.info                      # 探活，连不上会抛异常
    return dev
