"""算法级配置：几何容差、候选预算、证据权重。

只放"算法怎么算"，不放某张图的语义阈值；图纸事实仍在 profile。
证据权重同一尺度，不可用证据不参与分母。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GeometryConfig:
    snap_tol_mm: float = 5.0
    near_wall_factor: float = 2.5   # 以半个墙厚为单位：|v| <= factor * w/2
    max_candidates: int = 20000
    max_local_iterations: int = 4
    w_block: float = 0.40
    w_wall_proximity: float = 0.20
    w_wall_alignment: float = 0.20
    w_span_overlap: float = 0.20
    w_leaf_returns: float = 0.25
    w_swing_clear: float = 0.15
    w_judge: float = 0.30          # 语义裁决（模型判定门/窗）作为一条证据
    min_positive: int = 2


CONFIG = GeometryConfig()
