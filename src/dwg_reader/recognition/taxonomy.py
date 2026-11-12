"""人工维护的语义闭集与别名规则：先规则命中，命不中的才交模型。"""
from __future__ import annotations

import re

SEMANTICS = (
    "wall", "column", "door", "window", "room_name", "axis",
    "stair", "step", "apron", "north", "frame", "furniture", "ignore",
)

# (正则, 语义元组)；按顺序匹配，前面的优先
#: 吊轨/轨道/灯带/窗帘盒这类**线性配件**不是墙：它们同样是"长平行线对"，
#: 但一旦混进墙集合就会改变墙线分组，把弧线与墙的配对打断（实测：一张图上
#: 一个吊轨层就能让门候选从 290 掉到 268、采纳门从 84 掉到 73）。
#: 这条规则排在 wall 之前，命中即 ignore。
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"轨道|吊轨|track|天轨|窗帘盒|灯带|lamplight|lightstrip", ("ignore",)),
    (r"curtwall|curtain|幕墙|玻璃|glass", ("wall", "window")),
    (r"\bwall\b|墙体|墙身|剪力墙|轻钢龙骨|a-wall|i-wall|q\s*墙", ("wall",)),
    (r"\bcolumn\b|\bcol\b|框架柱|结构柱|柱", ("column",)),
    (r"\bdoor\b|门联窗|门$|_door|door_|防火门", ("door",)),
    (r"\bwindow\b|窗|window_text", ("window",)),
    (r"ff-text|房间名|space|ad-name|a-name-room|room_name|pub_text|名称", ("room_name",)),
    (r"\baxis\b|轴线|dote|a-grid|grid", ("axis",)),
    (r"\bstair\b|楼梯|梯段|ff-stair", ("stair",)),
    (r"台阶|踏步|\bstep\b", ("step",)),
    (r"散水|apron", ("apron",)),
    (r"指北针|north", ("north",)),
    (r"图框|title|pub_title|\btk\b|图签", ("frame",)),
    (r"家具|jiaju|洁具|ff-fixed|活动成品|fixed", ("furniture",)),
    (r"填充|hatch|天花|吊顶|标注|dim|说明|图例|legend|索引|详图", ("ignore",)),
)


#: **结构性**排除：这些图层永远不承载构件语义，与图纸无关，也轮不到模型判断。
#: 它们出现在 `layer 0` / Defpoints 上是因为 CAD 的默认落层或构造辅助线，
#: 一旦被赋了语义（尤其是被模型判成"图框层"），会连带把区域裁剪、墙网切碎。
NEVER_ROLE = frozenset({"0", "0 ", "defpoints", "defpoint", "_defpoints_"})
#: 纯数字层名同理：`1`、`2`、`20180112` 这类是落层残留或日期戳，不是有人刻意
#: 建的语义层；模型看到它们有双线就会判成墙，等于凭空加墙。
_NUMERIC_NAME = re.compile(r"^\d+$")


def is_never_role(name: str) -> bool:
    low = str(name or "").strip().lower()
    return low in NEVER_ROLE or bool(_NUMERIC_NAME.match(low))


def match_layer(name: str) -> tuple[str, ...] | None:
    """命中返回语义元组，未命中返回 None（交给模型）。"""
    low = name.lower()
    for pattern, sems in RULES:
        if re.search(pattern, low):
            return sems
    return None
