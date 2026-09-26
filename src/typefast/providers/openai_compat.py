"""OpenAI 兼容 Provider：一套实现覆盖百炼（DashScope 兼容模式）、火山方舟、智谱、OpenAI。

- 润色：POST /chat/completions，低温、非流式、带硬超时；
- 识别：POST /chat/completions，用 input_audio 传 base64 wav（qwen-asr / gpt-4o-transcribe 等）。
0.0.1 状态：结构完成，具体字段需与官方文档逐个核对后联调。"""

from __future__ import annotations

import base64
import io
import wave

import numpy as np
import requests

from typefast.core.contracts import ErrorKind, ProviderError
from typefast.core.pipeline import SYSTEM_PROMPT
from typefast.providers.base import AsrRequest, AsrResult, PolishRequest
from typefast.settings import get_secret

DEFAULT_POLISH_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_TIMEOUT_S = 30.0

HOTWORD_HEADER = """
用户常用词（优先保持原样）："""
LANG_EN = """
本次要求：用英文输出。"""
LANG_JA = """
本次要求：用日文输出。"""
LANG_MIX = """本次要求：中英混说。保持说话人原本的语种搭配：中文用中文，英文单词或句子保留英文原样，不翻译、不音译；中英相邻处按中文排版习惯加空格（例如「把 API Key 填好」）；英文部分按英文习惯标点。"""


def pcm16_wav_bytes(samples, sample_rate: int = 16000) -> bytes:
    x = np.asarray(samples, dtype=np.float32)
    x = np.clip(x, -1.0, 1.0)
    pcm = (x * 32767.0).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def classify_http_error(status: int, body: str) -> ProviderError:
    detail = (body or "")[:300]
    if status in (401, 403):
        # Key 无效：与「模型额度耗尽」分开——换候选模型救不了坏 Key，也不该触发路由熔断
        return ProviderError(ErrorKind.AUTH, "Key 无效或无权限（401/403），请检查 API Key", detail)
    if status == 429:
        return ProviderError(ErrorKind.SERVER_BUSY, "服务繁忙，稍后重试", detail)
    if status >= 500:
        return ProviderError(ErrorKind.SERVER_BUSY, "服务端错误", detail)
    return ProviderError(ErrorKind.SERVER_FAILED, "请求被拒绝", detail)


def _post_json(url: str, headers: dict, payload: dict, timeout_s: float) -> dict:
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
    except requests.Timeout:
        raise ProviderError(ErrorKind.TIMEOUT, "请求超时")
    except requests.RequestException as exc:
        raise ProviderError(ErrorKind.NETWORK, "网络错误", str(exc))
    if resp.status_code >= 400:
        raise classify_http_error(resp.status_code, resp.text)
    try:
        return resp.json()
    except ValueError:
        raise ProviderError(ErrorKind.SERVER_FAILED, "返回不是合法 JSON", (resp.text or "")[:300])


class OpenAICompatPolishProvider:
    name = "openai_compat"

    def __init__(self, model: str = "qwen-flash", endpoint: str = DEFAULT_POLISH_ENDPOINT,
                 secret_name: str = "polish_api_key", api_key: str = "",
                 temperature: float = 0.1, max_tokens: int = 2000,
                 system_prompt: str = SYSTEM_PROMPT, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.model = model
        self.endpoint = endpoint
        self.secret_name = secret_name
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt
        self.timeout_s = timeout_s

    def polish(self, req: PolishRequest) -> str:
        # ###替换成你的api：默认读凭据管理器里的 polish_api_key（typefast key set polish_api_key "###替换成你的api"）
        key = self.api_key or get_secret(self.secret_name)
        if not key:
            raise ProviderError(ErrorKind.NOT_CONFIGURED, "未配置润色 API Key")
        system = self.system_prompt
        if req.hotwords:
            system += HOTWORD_HEADER + "、".join(req.hotwords[:40])
        if req.output_language == "en":
            system += LANG_EN
        elif req.output_language == "ja":
            system += LANG_JA
        elif req.output_language == "mix":
            system += LANG_MIX
        payload = {
            "model": (req.model or self.model),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": req.text},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        data = _post_json(self.endpoint, {"Authorization": "Bearer " + key, "Content-Type": "application/json"}, payload, self.timeout_s)
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            raise ProviderError(ErrorKind.SERVER_FAILED, "润色返回结构异常", str(data)[:300])


class OpenAICompatAsrProvider:
    name = "openai_compat_asr"

    def __init__(self, model: str = "qwen3-asr-flash", endpoint: str = DEFAULT_POLISH_ENDPOINT,
                 secret_name: str = "asr_api_key", api_key: str = "",
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.model = model
        self.endpoint = endpoint
        self.secret_name = secret_name
        self.api_key = api_key
        self.timeout_s = timeout_s

    def transcribe(self, req: AsrRequest) -> AsrResult:
        # ###替换成你的api：默认读凭据管理器里的 asr_api_key（typefast key set asr_api_key "###替换成你的api"）
        key = self.api_key or get_secret(self.secret_name)
        if not key:
            raise ProviderError(ErrorKind.NOT_CONFIGURED, "未配置识别 API Key")
        wav = pcm16_wav_bytes(req.samples, req.sample_rate)
        b64 = base64.b64encode(wav).decode("ascii")
        payload = {
            "model": (req.model or self.model),
            "messages": [{"role": "user", "content": [
                {"type": "input_audio",
                 "input_audio": {"data": "data:audio/wav;base64," + b64, "format": "wav"}},
            ]}],
            "temperature": 0.0,
        }
        data = _post_json(self.endpoint, {"Authorization": "Bearer " + key, "Content-Type": "application/json"}, payload, self.timeout_s)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError(ErrorKind.SERVER_FAILED, "识别返回结构异常", str(data)[:300])
        return AsrResult(text=str(text).strip(), provider=self.name)
