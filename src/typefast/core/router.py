"""润色模型路由：质量优先 / 速度优先两条候选链 + 额度熔断。

策略：按链取第一个未熔断的模型；调用方遇到额度类失败时 mark_exhausted，
冷却期内自动跳下一个候选；全链熔断退化为 last_resort。
默认模型名是占位值，正式环境请按所用厂商在 config 里填写。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from typefast.settings import data_subdir

QUALITY_CHAIN = ["qwen-plus", "qwen-flash", "glm-4-flash"]
SPEED_CHAIN = ["qwen-flash", "glm-4-flash", "qwen-plus"]
LAST_RESORT = "qwen-flash"
COOLDOWN_S = 20 * 3600


class ModelRouter:
    """候选链与熔断状态；状态落盘，重启后仍然有效。"""

    def __init__(self, quality: Optional[List[str]] = None, speed: Optional[List[str]] = None,
                 state_path: Optional[Path] = None, cooldown_s: float = COOLDOWN_S) -> None:
        self.quality = list(quality or QUALITY_CHAIN)
        self.speed = list(speed or SPEED_CHAIN)
        self.cooldown_s = cooldown_s
        self.state_path = state_path or (data_subdir("state") / "router_state.json")
        self.exhausted: Dict[str, float] = {}
        self._load()

    def chain(self, mode: str = "quality") -> List[str]:
        return self.speed if mode == "speed" else self.quality

    def candidates(self, mode: str = "quality", now: Optional[float] = None) -> List[str]:
        now = time.time() if now is None else now
        chain = [m for m in self.chain(mode) if not self.is_exhausted(m, now)]
        return chain or [self.speed[0] if self.speed else LAST_RESORT]

    def is_exhausted(self, model: str, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return now < float(self.exhausted.get(model, 0.0))

    def mark_exhausted(self, model: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.exhausted[model] = now + self.cooldown_s
        self._save()

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.exhausted = {str(k): float(v) for k, v in raw.items()}
        except Exception:
            self.exhausted = {}

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.exhausted), encoding="utf-8")
        except Exception:
            pass
