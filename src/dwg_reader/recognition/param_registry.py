"""运行时参数注册表：能被"实验"改动的东西只有这张表里的项，而且必须真被算子读。

为什么单独一层
--------------
识别器里原来有一批"藏在函数体里"的容差（重叠率 0.6、平行角容差 1.5°、线束至
少 3 条…）。AI 增强如果要调这些值，就不能靠改模块级常量——那是进程全局的，
一次并发跑两个区域就互相污染；也不能让模型直接写数值，模型只能给**区间**。

所以边界定成这样：

* 程序登记 :class:`ParameterSpec`（名字、类型、**绝对授权区间**、单位、归一化
  基准、真正读它的算子、影响哪些节点）；
* 模型只能提交"想调哪个参数 + 建议区间"，区间必须与绝对授权区间求交，交不上
  就报错回滚；
* 执行时把参数值装进 :class:`RecognitionConfig`（不可变、带 digest）显式传给
  算子；算子必须 ``cfg.take(name)`` 才拿得到值——**没被读过的参数直接判失败**，
  不允许"编译了但没人用"的幽灵参数。

单位口径
--------
:data:`ParameterSpec.unit` 只允许 ``ratio`` / ``degree`` / ``drawing_unit`` /
``enum``。前三种是绝对量，``drawing_unit`` 表示"这张图的毫米数"，必须经
:func:`normalize` 用图内基准量（墙厚 / 门宽 / 自噪声）换算后才能用——这是
"换一张图仍然成立"的唯一办法。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType

SCHEMA = "parameter-registry/1.0"

#: 参数值类型闭集
VALUE_TYPES = ("float", "int", "enum", "bool")
#: 作用域：整张图 / 单个区域 / 一类形态（同族多实例共享）
SCOPES = ("drawing", "region", "pattern_family")
#: 单位口径闭集
UNITS = ("ratio", "degree", "drawing_unit", "enum")

#: 归一化基准（图内实测量的名字，见 scale.measure）
BASES = ("thickness", "door_w", "text_h", "noise", "col", "bay")

#: 图层角色闭集（与 taxonomy.SEMANTICS 同源；参数里出现闭集外的角色即拒收）
ROLES = ("wall", "column", "door", "window", "room_name", "axis", "stair",
         "step", "apron", "north", "frame", "furniture", "ignore")

#: 数值比较的容差（不是几何阈值，只是"两个 float 是否同值"）
_EPS = 1.0e-9

ERROR_CODES = ("UNKNOWN_PARAM", "TYPE_MISMATCH", "OUT_OF_ALLOWED",
               "UNIT_UNRESOLVED", "BASIS_MISSING", "CONSTRAINT_VIOLATED",
               "RANGE_EMPTY", "NOT_CONSUMED")


class ParameterError(ValueError):
    """参数校验失败：带**闭集错误码**，供编译器/实验器直接落进审计。"""

    def __init__(self, code: str, **detail):
        self.code = code if code in ERROR_CODES else "UNKNOWN_PARAM"
        self.detail = dict(detail)
        message = code
        if detail:
            message += ":" + json.dumps(detail, ensure_ascii=False, sort_keys=True,
                                        default=str)
        super().__init__(message)

    def as_dict(self) -> dict:
        return {"code": self.code, **self.detail}


@dataclass(frozen=True)
class ParameterSpec:
    """一个可调参数的**完整授权**声明。

    ``allowed`` 对数值型是 ``(lo, hi)`` 闭区间，对枚举是取值元组——这就是
    "绝对授权区间"，模型给的区间只能落在它里面（求交后非空）。
    """

    name: str
    value_type: str
    default: object
    allowed: tuple
    scope: str
    unit: str
    consumer: str
    affects: tuple = ()
    normalized_by: str | None = None
    description: str = ""

    # ---------------------------------------------------------------- 校验
    def bounds(self) -> tuple | None:
        if self.value_type in ("float", "int"):
            return (float(self.allowed[0]), float(self.allowed[1]))
        return None

    def check(self, value):
        """类型 + 授权范围 + 枚举闭集。返回**归一化后**的值（int 截断）。"""
        t = self.value_type
        if t == "bool":
            if not isinstance(value, bool):
                raise ParameterError("TYPE_MISMATCH", name=self.name, expect="bool")
            return value
        if t == "enum":
            if not isinstance(value, str):
                raise ParameterError("TYPE_MISMATCH", name=self.name, expect="enum")
            if value not in self.allowed:
                raise ParameterError("OUT_OF_ALLOWED", name=self.name, value=value,
                                     allowed=list(self.allowed))
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParameterError("TYPE_MISMATCH", name=self.name, expect=t)
        num = float(value)
        lo, hi = self.bounds()
        if num < lo - _EPS or num > hi + _EPS:
            raise ParameterError("OUT_OF_ALLOWED", name=self.name, value=num,
                                 allowed=[lo, hi])
        return int(round(num)) if t == "int" else round(num, 6)

    def within(self, lo, hi) -> tuple:
        """把模型给的区间与绝对授权区间求交；空集报 ``RANGE_EMPTY``。"""
        if self.value_type == "enum":
            raise ParameterError("TYPE_MISMATCH", name=self.name, expect="numeric")
        a, b = self.bounds()
        lo2 = max(a, float(lo))
        hi2 = min(b, float(hi))
        if lo2 > hi2 + _EPS:
            raise ParameterError("RANGE_EMPTY", name=self.name,
                                 asked=[float(lo), float(hi)], allowed=[a, b])
        return (lo2, hi2)

    # ---------------------------------------------------------------- 目录
    def as_catalog(self) -> dict:
        """给模型看的目录：**只有口径，没有毫米数**。"""
        row = {"name": self.name, "type": self.value_type, "unit": self.unit,
               "scope": self.scope, "default": self.default}
        if self.value_type in ("float", "int"):
            lo, hi = self.bounds()
            row["allowed"] = [lo, hi]
        else:
            row["allowed"] = list(self.allowed)
        if self.normalized_by:
            row["normalized_by"] = self.normalized_by
        return row


#: 参数注册表（唯一入口；未注册的名字一律 ``UNKNOWN_PARAM``）
PARAMS: dict[str, ParameterSpec] = {}


def register(spec: ParameterSpec) -> ParameterSpec:
    """登记一个参数。声明本身不合法就当场炸，不留到运行期。"""
    if spec.name in PARAMS:
        raise ValueError(f"参数重复注册: {spec.name}")
    if spec.value_type not in VALUE_TYPES:
        raise ValueError(f"{spec.name}: 未知类型 {spec.value_type}")
    if spec.scope not in SCOPES:
        raise ValueError(f"{spec.name}: 未知作用域 {spec.scope}")
    if spec.unit not in UNITS:
        raise ValueError(f"{spec.name}: 未知单位 {spec.unit}")
    if spec.normalized_by and spec.normalized_by not in BASES:
        raise ValueError(f"{spec.name}: 未知归一化基准 {spec.normalized_by}")
    if not spec.consumer:
        raise ValueError(f"{spec.name}: 必须声明真正的消费者")
    spec.check(spec.default)                 # 默认值必须落在授权区间内
    PARAMS[spec.name] = spec
    return spec


# --------------------------------------------------------------------- 首批参数
# 每个参数都写清"为什么要有它"，否则它迟早会变成一个被藏起来的魔数。
register(ParameterSpec(
    "min_overlap_ratio", "float", 0.60, (0.40, 0.95), "pattern_family", "ratio",
    consumer="executors:_op_regenerate_opening",
    affects=("openings_select", "walls_merge", "rooms"),
    description="两条平行线沿向重叠占较短者的比例达到多少才算一个洞口的边"))
register(ParameterSpec(
    "parallel_angle_tol_deg", "float", 1.50, (0.20, 5.00), "pattern_family",
    "degree", consumer="executors:_op_retry_wall_pairing",
    affects=("walls_seg", "wall_model"),
    description="多少度以内算平行（画图误差，不是几何尺度）"))
register(ParameterSpec(
    "wall_gap_ratio", "float", 1.00, (0.20, 4.00), "drawing", "ratio",
    consumer="executors:_op_retry_wall_pairing",
    affects=("walls_seg",), normalized_by="thickness",
    description="墙两面线的间距是墙厚的多少倍以内仍可能是一道墙"))
register(ParameterSpec(
    "column_aspect_ratio", "float", 3.00, (1.50, 8.00), "pattern_family", "ratio",
    consumer="executors:_op_regenerate_column",
    affects=("columns",),
    description="柱候选长边/短边的上限比，超过就不按方柱算"))
register(ParameterSpec(
    "line_bundle_min_count", "int", 3, (2, 8), "pattern_family", "ratio",
    consumer="executors:_op_group_line_bundle",
    affects=("windows", "doors", "openings_select"),
    description="一组平行线至少几条才算一个线束（窗族）形态"))
register(ParameterSpec(
    "allow_unanchored_window_candidate", "bool", False, (False, True),
    "drawing", "enum", consumer="executors:_op_regenerate_opening",
    affects=("windows",),
    description="允许生成不贴墙的窗候选（只能进 uncertain，不能升 confirmed）"))
register(ParameterSpec(
    "region_layer_roles", "enum", "window", ROLES, "region", "enum",
    consumer="plan_compiler:compile_plan", affects=("load",),
    description="区域补丁允许写出的图层角色闭集（超出即拒收）"))


#: 组合约束：单参数合法不等于组合合法（``min <= max`` 之类）
CONSTRAINTS: tuple = (
    ("min_overlap_ratio<=0.95", lambda v: float(v.get("min_overlap_ratio", 0.0)) <= 0.95),
    ("wall_gap_ratio>=0.2", lambda v: float(v.get("wall_gap_ratio", 1.0)) >= 0.2),
    ("line_bundle>=2", lambda v: int(v.get("line_bundle_min_count", 3)) >= 2),
)


def spec(name: str) -> ParameterSpec:
    s = PARAMS.get(str(name))
    if s is None:
        raise ParameterError("UNKNOWN_PARAM", name=str(name))
    return s


def has(name: str) -> bool:
    return str(name) in PARAMS


def public_catalog(names=None) -> list[dict]:
    """模型可见目录（只有名字/类型/口径/区间，绝不含图纸专属毫米数）。"""
    picked = list(names) if names else sorted(PARAMS)
    return [PARAMS[n].as_catalog() for n in picked if n in PARAMS]


def main_public_catalog() -> list[dict]:
    """模型可以在"提议"阶段自己提区间的参数（其余只能由程序给）。"""
    return public_catalog([n for n, s in sorted(PARAMS.items())
                           if s.value_type in ("float", "int")])


def validate_mapping(values: dict) -> dict:
    """把 ``{name: value}`` 逐个校验（含组合约束），返回归一化后的新 dict。"""
    out: dict = {}
    for name, value in (values or {}).items():
        out[str(name)] = spec(name).check(value)
    for label, fn in CONSTRAINTS:
        try:
            ok = bool(fn(out))
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise ParameterError("CONSTRAINT_VIOLATED", rule=label,
                                 values={k: out.get(k) for k in
                                         ("min_overlap_ratio", "wall_gap_ratio",
                                          "line_bundle_min_count")})
    return out


def normalize(name: str, value, basis: dict):
    """把参数值换算成"这张图的毫米数"（只对 ``drawing_unit`` 生效）。

    归一化基准没测出来就报 ``BASIS_MISSING``——宁可停机，也不拿默认 200 去凑。
    """
    s = spec(name)
    raw = s.check(value)
    if s.unit != "drawing_unit":
        return raw
    base = (basis or {}).get(s.normalized_by or "thickness")
    if base in (None, 0):
        raise ParameterError("BASIS_MISSING", name=name, basis=s.normalized_by)
    return round(float(raw) * float(base), 3)


def points(name: str, lo: float, hi: float, count: int = 3) -> list:
    """在授权区间里生成实验点（默认点优先，再低/中/高，整型去重）。"""
    s = spec(name)
    lo2, hi2 = s.within(lo, hi)
    count = max(1, int(count))
    if count == 1:
        cands = [s.check(s.default)]
    else:
        step = (hi2 - lo2) / float(count - 1)
        cands = [lo2 + step * i for i in range(count)]
    out: list = []
    for c in [s.check(s.default)] + [s.check(v) for v in cands]:
        if c not in out:
            out.append(c)
    return out


# --------------------------------------------------------------------- 配置对象
class RecognitionConfig:
    """一次运行/一次试验的参数快照：不可变、可哈希、可审计。

    与"模块级常量 + os.environ"的区别就在这儿：两个区域同时跑不同参数时，各自
    持有自己的 config，互不干扰；``take()`` 让"这个参数有没有被真正读"变成可
    检验的事实。
    """

    __slots__ = ("_values", "_origin", "_consumed", "digest", "scope")

    def __init__(self, values=None, *, origin=None, scope: str = "drawing"):
        clean = validate_mapping(dict(values or {}))
        self._values = MappingProxyType(clean)
        self._origin = MappingProxyType(dict(origin or {}))
        self._consumed: set = set()
        self.scope = scope
        self.digest = _digest(clean)

    # ---- 读
    def get(self, name: str, default=None):
        """读默认值（**不算消费**，仅用于展示/默认路径）。"""
        if name in self._values:
            return self._values[name]
        s = PARAMS.get(name)
        return s.default if s is not None else default

    def take(self, name: str):
        """算子真正读取参数：登记消费 + 返回生效值（未显式给就用默认）。"""
        s = spec(name)
        self._consumed.add(s.name)
        return self._values.get(s.name, s.default)

    def has(self, name: str) -> bool:
        return name in self._values

    def explicit(self) -> dict:
        return dict(self._values)

    def origin(self) -> dict:
        return dict(self._origin)

    def consumed(self) -> list:
        return sorted(self._consumed)

    def unconsumed(self, names) -> list:
        """被编译进来、却没有任何算子读过的参数（必须判失败）。"""
        return sorted(str(n) for n in (names or ()) if str(n) not in self._consumed)

    # ---- 派生
    def with_values(self, extra: dict, *, origin: str = "proposal") -> "RecognitionConfig":
        merged = dict(self._values)
        merged.update(dict(extra or {}))
        org = dict(self._origin)
        for k in (extra or {}):
            org[str(k)] = origin
        return RecognitionConfig(merged, origin=org, scope=self.scope)

    def as_dict(self) -> dict:
        return {"schema": SCHEMA, "scope": self.scope, "values": dict(self._values),
                "origin": dict(self._origin), "digest": self.digest,
                "consumed": self.consumed()}

    def __repr__(self) -> str:                       # pragma: no cover - 调试用
        return f"RecognitionConfig({dict(self._values)}, digest={self.digest[:8]})"


def _digest(values: dict) -> str:
    raw = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def config(values=None, *, origin=None, scope: str = "drawing") -> RecognitionConfig:
    """便捷构造（等价于 ``RecognitionConfig(...)``，保持调用点短）。"""
    return RecognitionConfig(values, origin=origin, scope=scope)


def merge(*configs) -> RecognitionConfig:
    """按顺序合并多个 config（后面的覆盖前面的），保留 origin。"""
    values: dict = {}
    origin: dict = {}
    for cfg in configs:
        if not isinstance(cfg, RecognitionConfig):
            continue
        for k, v in cfg.explicit().items():
            values[k] = v
            origin[k] = cfg.origin().get(k, "merge")
    return RecognitionConfig(values, origin=origin)


def summary(values=None) -> dict:
    """注册表自检摘要（进 meta / 审计）。"""
    return {"schema": SCHEMA, "count": len(PARAMS),
            "params": {n: s.as_catalog() for n, s in sorted(PARAMS.items())},
            "constraints": [c[0] for c in CONSTRAINTS],
            "values": dict(values or {})}
