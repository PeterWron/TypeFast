"""硅基流动（SiliconFlow）语音识别：OpenAI 兼容的 multipart 转写接口。

- 端点：POST https://api.siliconflow.cn/v1/audio/transcriptions
- 认证：Authorization: Bearer <key>
- 请求：multipart，file=16k 单声道 wav，model=XingChenAGI/XingChenASR-V3.2-Ultra
- 返回：{ duration: 5.44, text: ..., usage: {type: duration, seconds: 5}}
- 实测（2026-09）：5.4s 音频约 1.6s 返回；同一 Key 也能用于 /v1/chat/completions 做润色。

注意：该接口按实测不接受 language 参数（自动识别语种），所以这里不发送。"""

from __future__ import annotations

import json
import time

import requests

from typefast.core.contracts import ErrorKind, ProviderError
from typefast.providers.base import AsrRequest, AsrResult
from typefast.providers.openai_compat import classify_http_error, pcm16_wav_bytes
from typefast.settings import get_secret

DEFAULT_ENDPOINT = "https://api.siliconflow.cn/v1/audio/transcriptions"
DEFAULT_MODEL = "XingChenAGI/XingChenASR-V3.2-Ultra"
# ###替换成你的api：识别用的硅基流动 Key 存在凭据管理器，代码里只放槽位名
#   typefast key set siliconflow_api_key "###替换成你的api"
DEFAULT_SECRET = "siliconflow_api_key"


class SiliconFlowAsrProvider:
    name = "siliconflow"

    def __init__(self, model: str = DEFAULT_MODEL, endpoint: str = DEFAULT_ENDPOINT,
                 secret_name: str = DEFAULT_SECRET, api_key: str = "",
                 timeout_s: float = 60.0) -> None:
        self.model = model or DEFAULT_MODEL
        self.endpoint = endpoint or DEFAULT_ENDPOINT
        self.secret_name = secret_name
        self.api_key = api_key
        self.timeout_s = timeout_s

    def build_files(self, req: AsrRequest) -> dict:
        wav = pcm16_wav_bytes(req.samples, req.sample_rate)
        return {"file": ("speech.wav", wav, "audio/wav")}

    def build_data(self, req: AsrRequest) -> dict:
        return {"model": self.model}

    def transcribe(self, req: AsrRequest) -> AsrResult:
        key = self.api_key or get_secret(self.secret_name)
        if not key:
            raise ProviderError(ErrorKind.NOT_CONFIGURED, "未配置硅基流动 API Key（" + self.secret_name + "）")
        started = time.monotonic()
        headers = {"Authorization": "Bearer " + key}
        files = self.build_files(req)  # 含 WAV 编码
        encode_ms = (time.monotonic() - started) * 1000.0
        try:
            resp = requests.post(self.endpoint, headers=headers, files=files,
                                data=self.build_data(req), timeout=self.timeout_s, stream=True)
        except requests.Timeout:
            raise ProviderError(ErrorKind.TIMEOUT, "识别请求超时")
        except requests.RequestException as exc:
            raise ProviderError(ErrorKind.NETWORK, "网络错误", str(exc))
        if resp.status_code >= 400:
            raise classify_http_error(resp.status_code, resp.text)
        ttfb_ms = (time.monotonic() - started) * 1000.0
        raw_body = resp.content
        download_ms = max(0.0, (time.monotonic() - started) * 1000.0 - ttfb_ms)
        try:
            body = json.loads(raw_body.decode("utf-8", "ignore"))
        except ValueError:
            raise ProviderError(ErrorKind.SERVER_FAILED, "返回不是合法 JSON", raw_body.decode("utf-8", "ignore")[:300])
        text = str(body.get("text") or "").strip()
        if not text:
            raise ProviderError(ErrorKind.NO_SPEECH, "识别结果为空")
        return AsrResult(text=text, provider=self.name, latency_ms=(time.monotonic() - started) * 1000,
                        encode_ms=encode_ms, ttfb_ms=ttfb_ms, download_ms=download_ms)
