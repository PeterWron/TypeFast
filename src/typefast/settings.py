"""配置与密钥管理。

配置默认存源码树根 config.json（装到 site-packages 时回落 %APPDATA%/TypeFast/config.json）；API Key 走 Windows 凭据管理器（keyring）。
运行数据（日志 / 记录 / 状态 / 演示语音）默认落源码树内的 data/，规则见 data_dir()。
provider 一律用字符串注册名，便于以后插件化替换。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_NAME = "TypeFast"
CONFIG_ENV = "TYPEFAST_CONFIG"
DATA_ENV = "TYPEFAST_DATA"
SERVICE_NAME = "typefast"

# ###替换成你的api
# API Key 既不写进代码也不写进 config.json：统一存到 Windows 凭据管理器（keyring）。
#   typefast key set siliconflow_api_key "###替换成你的api"   :: 识别（硅基流动）
#   typefast key set polish_api_key "###替换成你的api"        :: 润色（OpenAI 兼容接口）
#   typefast key set asr_api_key "###替换成你的api"           :: 识别（其他 OpenAI 兼容接口）
#   typefast key set volc_app_key "###替换成你的api"          :: 火山引擎（另加 volc_access_key）
# 也可以在图形界面「API Key」页填写（界面只显示 已配置 / 未配置，不回显明文）。
API_KEY_HINT = 'API Key 不写进这个文件（密钥一律存 Windows 凭据管理器）：typefast key set siliconflow_api_key "###替换成你的api"（润色用 polish_api_key）'


@dataclass
class AsrConfig:
    provider: str = "siliconflow"
    model: str = "XingChenAGI/XingChenASR-V3.2-Ultra"
    endpoint: str = ""
    language: str = "zh-en"  # zh / en / ja / zh-en（中英混说）/ auto
    hotwords: List[str] = field(default_factory=list)


@dataclass
class PolishConfig:
    provider: str = "openai_compat"
    model: str = "Qwen/Qwen2.5-7B-Instruct"
    endpoint: str = "https://api.siliconflow.cn/v1/chat/completions"
    enabled: bool = True
    temperature: float = 0.1
    max_tokens: int = 2000
    deadline_ms: int = 1500
    short_text_chars: int = 10


@dataclass
class HotkeyConfig:
    hold: str = "right_ctrl"
    cancel: str = "esc"
    mouse_middle: bool = True


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    input_device: str = ""
    preroll_ms: int = 300
    keep_warm: bool = True   # 常开流：避免约 20% 的「打开后整段不出数据」，并补回按下前的预缓冲
    normalize: bool = True  # 送识别前按峰值保守归一（低电平麦克风会漏字）


@dataclass
class SegmentConfig:
    # 切段目标 5.0s：实测同一段音频，6.0s 与 5.0s 都能得到与整段识别完全一致的结果
    # （相似度 1.0000），而切到 2.5-3.5s 会出现同音字错误——这个模型需要约 5 秒音频上下文。
    target_commit_s: float = 5.0
    # 实测：短于 5 秒的片段这个模型会整句丢内容，所以最小切段时长与目标一致
    min_commit_s: float = 5.0
    live_margin_s: float = 1.2
    search_radius_s: float = 2.0
    # 只在 0.30s 以上的真停顿处切，避免切在句子中间丢上下文
    min_pause_s: float = 0.30
    # 同一时刻允许几段在途：并发提交把长语音墙钟从 4.2s 降到 3.1s
    max_inflight: int = 3
    # False = 不边录边切段，松手后整段送识别：准确率优先，代价是等待随音频变长
    live_commit: bool = True


@dataclass
class DeliveryConfig:
    paste_threshold_chars: int = 30
    restore_clipboard: bool = True
    strip_chat_trailing_period: bool = True


@dataclass
class PrivacyConfig:
    keep_audio: bool = False
    exclude_apps: List[str] = field(default_factory=lambda: ["keepass.exe", "1password.exe"])
    block_password_fields: bool = True


@dataclass
class RouterConfig:
    mode: str = "quality"
    quality_chain: List[str] = field(default_factory=list)
    speed_chain: List[str] = field(default_factory=list)


@dataclass
class Config:
    version: str = "0.1.0"
    asr: AsrConfig = field(default_factory=AsrConfig)
    polish: PolishConfig = field(default_factory=PolishConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    segment: SegmentConfig = field(default_factory=SegmentConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    router: RouterConfig = field(default_factory=RouterConfig)

    @staticmethod
    def path() -> Path:
        """配置路径：TYPEFAST_CONFIG 环境变量 > 源码树根 config.json > %APPDATA%/TypeFast/config.json。"""
        override = os.environ.get(CONFIG_ENV)
        if override:
            return Path(override)
        root = project_root()
        if root is not None:
            return root / "config.json"
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / APP_NAME / "config.json"

    @classmethod
    def load(cls) -> "Config":
        p = cls.path()
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Config":
        cfg = cls()
        for name in ("asr", "polish", "hotkey", "audio", "segment", "delivery", "privacy", "router"):
            section = raw.get(name) or {}
            target = getattr(cfg, name)
            for key, value in section.items():
                if hasattr(target, key):
                    setattr(target, key, value)
        if "version" in raw:
            cfg.version = str(raw["version"])
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        # ###替换成你的api：JSON 不支持注释，用一个被解析器忽略的字段把「密钥写哪儿」一起写进配置
        data: Dict[str, Any] = {"###替换成你的api": API_KEY_HINT}
        data.update(asdict(self))
        return data

    def save(self) -> Path:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return p


def project_root() -> Optional[Path]:
    """源码树根：从本文件向上找带 pyproject.toml 的目录；装到 site-packages 时返回 None。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def data_dir() -> Path:
    """运行数据目录：TYPEFAST_DATA 环境变量 > 源码树内 data/ > %LOCALAPPDATA%/TypeFast。"""
    override = os.environ.get(DATA_ENV)
    if override:
        d = Path(override)
    else:
        root = project_root()
        if root is not None:
            d = root / "data"
        else:
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
            d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_subdir(name: str) -> Path:
    """数据子目录：logs 日志 / records 文字记录 / state 运行状态 / audio 语音。"""
    d = data_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_secret(name: str) -> Optional[str]:
    try:
        import keyring
    except Exception:
        return None
    try:
        return keyring.get_password(SERVICE_NAME, name)
    except Exception:
        return None


def set_secret(name: str, value: str) -> bool:
    try:
        import keyring
    except Exception:
        return False
    try:
        keyring.set_password(SERVICE_NAME, name, value)
        return True
    except Exception:
        return False
