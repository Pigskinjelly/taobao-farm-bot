"""界面树模型与元素查找。

uiautomator dump 得到一棵 XML 节点树，每个节点带 text / content-desc /
resource-id / bounds。这里把它拍平成 Node 列表，并提供业务侧需要的查询：
按文案、按正则、按方位、按"靠近某元素"。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass
class Node:
    text: str = ""
    desc: str = ""
    rid: str = ""
    cls: str = ""
    package: str = ""
    clickable: bool = False
    enabled: bool = True
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    index: int = 0
    children: list["Node"] = field(default_factory=list)

    # ---------------------------------------------------------- 几何

    @property
    def left(self) -> int:
        return self.bounds[0]

    @property
    def top(self) -> int:
        return self.bounds[1]

    @property
    def right(self) -> int:
        return self.bounds[2]

    @property
    def bottom(self) -> int:
        return self.bounds[3]

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    @property
    def cy(self) -> int:
        return (self.top + self.bottom) // 2

    @property
    def cx(self) -> int:
        return (self.left + self.right) // 2

    def visible(self) -> bool:
        return self.area > 0

    # ---------------------------------------------------------- 文案

    @property
    def label(self) -> str:
        """用于匹配和展示的合并文案。text 与 desc 常重复，去重后拼接。"""
        parts: list[str] = []
        for p in (self.text.strip(), self.desc.strip()):
            if p and p not in parts:
                parts.append(p)
        return " ".join(parts)

    def has_text(self) -> bool:
        return bool(self.text.strip() or self.desc.strip())

    def __repr__(self) -> str:
        t = (self.text or self.desc or "")[:20].replace("\n", " ")
        return f"<Node {t!r} {self.bounds} clickable={self.clickable}>"


def _parse_bounds(raw: str) -> tuple[int, int, int, int]:
    nums = re.findall(r"-?\d+", raw or "")
    if len(nums) >= 4:
        return int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3])
    return 0, 0, 0, 0


def _build(elem: ET.Element, counter: list[int]) -> Node:
    a = elem.attrib
    node = Node(
        text=a.get("text", ""),
        desc=a.get("content-desc", ""),
        rid=a.get("resource-id", ""),
        cls=a.get("class", ""),
        package=a.get("package", ""),
        clickable=a.get("clickable") == "true",
        enabled=a.get("enabled", "true") == "true",
        bounds=_parse_bounds(a.get("bounds", "")),
        index=counter[0],
    )
    counter[0] += 1
    for child in elem:
        if child.tag == "node":
            node.children.append(_build(child, counter))
    return node


def parse(xml_text: str) -> tuple[Node, list[Node]]:
    """返回 (根节点, 拍平后的节点列表)。"""
    root_elem = ET.fromstring(xml_text)
    counter = [0]
    root = _build(root_elem, counter)
    flat: list[Node] = []
    stack = [root]
    while stack:
        n = stack.pop()
        flat.append(n)
        stack.extend(reversed(n.children))
    flat.sort(key=lambda n: n.index)
    return root, flat


# ---------------------------------------------------------------- 查询


def by_text(nodes: Iterable[Node], needles: Iterable[str], exact: bool = False) -> list[Node]:
    out = []
    for n in nodes:
        label = n.label
        if not label:
            continue
        for needle in needles:
            hit = (label == needle) if exact else (needle in label)
            if hit:
                out.append(n)
                break
    return out


def by_regex(nodes: Iterable[Node], pattern: str) -> list[tuple[Node, re.Match]]:
    rx = re.compile(pattern)
    out = []
    for n in nodes:
        label = n.label
        if not label:
            continue
        m = rx.search(label)
        if m:
            out.append((n, m))
    return out


def clickable(nodes: Iterable[Node]) -> list[Node]:
    return [n for n in nodes if n.clickable and n.enabled and n.visible()]


def nearest(nodes: Iterable[Node], anchor: Node, max_dy: int = 400, max_dx: int = 400) -> list[Node]:
    """按到 anchor 的距离排序，返回竖直方向上邻近的元素（用于"卡片里那个按钮"）。"""
    scored = []
    for n in nodes:
        dy = abs(n.cy - anchor.cy)
        dx = abs(n.cx - anchor.cx)
        if dy <= max_dy and dx <= max_dx and n.index != anchor.index:
            scored.append((dy + dx // 4, n))
    scored.sort(key=lambda p: p[0])
    return [n for _, n in scored]


def topmost_small(nodes: Iterable[Node], limit: int = 6) -> list[Node]:
    """点击类候选按钮通常很小。按面积升序，用于挑"关闭 X"。"""
    pool = [n for n in nodes if n.visible()]
    pool.sort(key=lambda n: n.area)
    return pool[:limit]


def screen_box(nodes: Iterable[Node]) -> tuple[int, int]:
    """由节点边界反推屏幕尺寸。

    根节点 <hierarchy> 本身没有 bounds，直接读 root.width 会得到 0，
    这里取所有节点右下角的最大值。
    """
    w = h = 0
    for n in nodes:
        if n.right > w:
            w = n.right
        if n.bottom > h:
            h = n.bottom
    return w, h


def has_any(nodes: Iterable[Node], needles: Iterable[str]) -> Optional[str]:
    """命中任一关键词则返回该词，否则 None。"""
    for n in nodes:
        label = n.label
        if not label:
            continue
        for needle in needles:
            if needle in label:
                return needle
    return None
