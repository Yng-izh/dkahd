"""Detect Tianzheng (天正) custom objects that a DXF reader cannot interpret.

Tianzheng Architecture/Structure stores walls, doors, axes, symbols and its own
text objects as custom (``TCH_*``) classes.  ``ACAD_PROXY_ENTITY`` records keep
only the cached display geometry and the class hint — never the text payload —
so after a DWG→DXF conversion those objects can no longer be read.  This module
detects the situation and tells the user how to produce a readable file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dxf import Drawing

#: DXF class names used by Tianzheng products.
TIANZHENG_CLASS_PREFIXES = ("TCH_",)

#: Application names that may appear in the CLASSES section.
TIANZHENG_APP_NAMES = ("TCH", "TANGENT", "TARCH", "TSSD", "TWT", "TK")

#: Keywords found in the proxy notification Tianzheng embeds in its classes.
TIANZHENG_HINTS = ("TCH_KERNAL", "天正", "tangent.com.cn")

#: Objects whose payload the user typically cares about.
IMPORTANT_CLASSES = {
    "TCH_TEXT": "天正文字",
    "TCH_MTEXT": "天正多行文字",
    "TCH_DIMENSION": "天正尺寸标注",
    "TCH_DIMENSION2": "天正尺寸标注",
    "TCH_AXIS_LABEL": "轴号",
    "TCH_ARROW": "箭头/符号",
    "TCH_DOOR": "门",
    "TCH_WINDOW": "窗",
    "TCH_CORNER_WINDOW": "转角窗",
    "TCH_COLUMN": "柱",
    "TCH_BALCONY": "阳台",
    "TCH_STAIR": "楼梯",
    "TCH_AUTOSTAIR": "自动楼梯",
    "TCH_DRAWINGNAME": "图名",
    "TCH_ELEVATION": "标高",
    "TCH_COORD": "坐标",
    "TCH_LEADER": "引出标注",
    "TCH_MULTILEADER": "多重引线",
    "TCH_ROOM": "房间",
    "TCH_TABLE": "天正表格",
}

SEVERITY_ORDER = {"none": 0, "info": 1, "warning": 2, "severe": 3}


@dataclass
class TianzhengReport:
    """Result of Tianzheng detection for one drawing."""

    detected: bool = False
    severity: str = "none"
    class_count: int = 0
    class_samples: list[str] = field(default_factory=list)
    important_classes: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    proxy_entities: int = 0
    proxy_objects: int = 0
    text_entities: int = 0
    insert_entities: int = 0
    summary: str = ""
    advice: list[str] = field(default_factory=list)

    @property
    def proxy_total(self) -> int:
        return self.proxy_entities + self.proxy_objects

    def to_dict(self) -> dict:
        return {
            "detected": self.detected,
            "severity": self.severity,
            "class_count": self.class_count,
            "class_samples": self.class_samples,
            "important_classes": self.important_classes,
            "apps": self.apps,
            "proxy_entities": self.proxy_entities,
            "proxy_objects": self.proxy_objects,
            "proxy_total": self.proxy_total,
            "text_entities": self.text_entities,
            "insert_entities": self.insert_entities,
            "summary": self.summary,
            "advice": self.advice,
        }


def _is_tianzheng_class(dxf_name: str, cpp_name: str, app: str) -> bool:
    if dxf_name.upper().startswith(TIANZHENG_CLASS_PREFIXES):
        return True
    if any(hint in app for hint in TIANZHENG_HINTS):
        return True
    # Tianzheng keeps a C++ class name such as "TDbText" next to "TCH_TEXT".
    return cpp_name.upper().startswith("TDB") and dxf_name.upper().startswith("TCH")


def detect_tianzheng(drawing: Drawing) -> TianzhengReport:
    """Inspect a parsed drawing for Tianzheng custom objects."""
    tch_classes = [
        dxf_name or cpp_name
        for dxf_name, cpp_name, app in drawing.classes
        if _is_tianzheng_class(dxf_name, cpp_name, app)
    ]
    apps = sorted(
        {
            app.strip('"').split("|", 1)[0].strip()
            for _dxf_name, _cpp_name, app in drawing.classes
            if any(hint in app for hint in TIANZHENG_HINTS)
        }
        - {""}
    )
    proxy_entities = drawing.proxy_entities
    proxy_objects = drawing.proxy_objects
    text_entities = drawing.counts.get("TEXT", 0) + drawing.counts.get("MTEXT", 0)
    insert_entities = drawing.counts.get("INSERT", 0)

    detected = bool(tch_classes or apps or proxy_entities or proxy_objects)
    report = TianzhengReport(
        detected=detected,
        class_count=len(tch_classes),
        class_samples=sorted(set(tch_classes))[:12],
        important_classes=list(
            dict.fromkeys(
                IMPORTANT_CLASSES[name]
                for name in sorted(set(tch_classes))
                if name in IMPORTANT_CLASSES
            )
        ),
        apps=apps,
        proxy_entities=proxy_entities,
        proxy_objects=proxy_objects,
        text_entities=text_entities,
        insert_entities=insert_entities,
    )
    if not detected:
        report.summary = "未检测到天正自定义对象。"
        return report

    proxy_total = report.proxy_total
    ratio = proxy_total / max(1, proxy_total + text_entities)
    if proxy_total == 0:
        report.severity = "info"
        report.summary = "检测到天正类定义，但图纸内容已全部转换为基本实体，文字可正常读取。"
    elif proxy_total >= 200 or ratio >= 0.5:
        report.severity = "severe"
        report.summary = (
            f"天正对象未展开：{proxy_total} 个代理实体/对象无法解析"
            f"（占图形对象的 {ratio:.0%}），天正文字、轴号、门窗编号、尺寸等可能大量缺失。"
        )
    elif proxy_total >= 20:
        report.severity = "warning"
        report.summary = (
            f"存在 {proxy_total} 个天正代理实体（占 {ratio:.0%}），"
            "部分天正对象无法解析，文字可能不完整。"
        )
    else:
        report.severity = "info"
        report.summary = f"存在少量天正代理实体（{proxy_total} 个），影响有限。"

    if proxy_total == 0:
        report.advice = [
            "无需处理：当前 DXF 已包含可读的基本实体。",
        ]
    else:
        report.advice = [
            (
                "在天正软件中打开原 DWG，使用「文件布图 → 图形导出」并选择 AutoCAD 格式"
                "（T3 导出会把天正对象炸开为 TEXT/LINE 等基本实体）。"
            ),
            "或在 CAD/天正中执行 EXPLODE（分解）后再另存为 DXF。",
            "也可用天正自带的「批量导出」功能，将图纸导为低版本 DWG 后再用本工具解析。",
        ]
        if text_entities:
            report.advice.append(
                f"本次仍提取到 {text_entities} 条基本文字实体，但天正专属文字（TCH_TEXT）无法读取。"
            )
    return report


def tianzheng_markdown(report: TianzhengReport) -> str:
    """Render the Tianzheng section of the report."""
    if not report.detected:
        return "未检测到天正自定义对象。"
    lines = [
        f"**结论**: {report.summary}",
        "",
        f"- 天正类数量: {report.class_count}",
        f"- 代理实体: {report.proxy_entities}，代理对象: {report.proxy_objects}",
        f"- 可读文字实体: {report.text_entities}，块参照: {report.insert_entities}",
    ]
    if report.apps:
        lines.append(f"- 应用标识: {', '.join(report.apps)}")
    if report.important_classes:
        lines.append(f"- 受影响对象: {', '.join(report.important_classes)}")
    if report.class_samples:
        lines.append(f"- 类名示例: {', '.join(report.class_samples)}")
    if report.advice:
        lines.append("")
        for advice in report.advice:
            lines.append(f"- {advice}")
    return "\n".join(lines)
