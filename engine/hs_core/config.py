"""HS_* 环境变量配置加载。默认值必须安全：内容采集与云端 Judge 默认关闭。"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    """解析布尔开关；非法值视为 default，不抛异常。"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    """解析浮点数；NaN / 非法值 / 缺省一律取 default。"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if math.isnan(value):
        return default
    return value


@dataclass(frozen=True)
class Config:
    """运行配置（env -> Config，全程只读）。"""

    home: Path = field(default_factory=lambda: Path.home() / ".harness_scope")
    spool_dir: Path = field(default_factory=lambda: Path.home() / ".harness_scope" / "spool")
    db_path: Path = field(
        default_factory=lambda: Path.home() / ".harness_scope" / "db" / "harness_scope.db"
    )
    # 安全默认：off。只有显式设为 1/true/yes/on 才开启。
    content_capture: bool = False
    cloud_judge: bool = False
    default_budget_cny: float = 0.5
    debug: bool = False


def load_config() -> Config:
    """从 HS_* 环境变量加载配置；缺省值全部安全。"""
    home = Path(os.environ.get("HS_HOME") or Path.home() / ".harness_scope")
    return Config(
        home=home,
        spool_dir=Path(os.environ.get("HS_SPOOL_DIR") or home / "spool"),
        db_path=Path(os.environ.get("HS_DB_PATH") or home / "db" / "harness_scope.db"),
        content_capture=_env_bool("HS_CONTENT_CAPTURE", default=False),
        cloud_judge=_env_bool("HS_CLOUD_JUDGE", default=False),
        default_budget_cny=_env_float("HS_DEFAULT_BUDGET_CNY", default=0.5),
        debug=_env_bool("HS_DEBUG", default=False),
    )
