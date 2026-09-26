"""火山引擎大模型录音识别（极速版 flash，同步一步出结果）。

0.0.1 状态：端点与 header 按公开文档实现，字段需联调核对；
若厂商调整，只改 build_payload / build_headers，不影响上层。"""

from __future__ import annotations

import base64
import uuid

from typefast.core.contracts import ErrorKind, ProviderError
from typefast.providers.base import AsrRequest, AsrResult
from typefast.providers.openai_compat import _post_json, pcm16_wav_bytes
from typefast.settings import get_secret

FLASH_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
RESOURCE_TURBO = "volc.bigasr.auc_turbo"
RESOURCE_STANDARD = "volc.bigasr.auc"
NO_SPEECH_CODE = "20000003"

# 该接口没有中英混说标记：zh-en / auto 一律按中文提交（模型仍会输出夹杂的英文词）
VOLC_LANGUAGE = {"zh-en": "zh", "zh_en": "zh", "zh+en": "zh", "mixed": "zh", "auto": "zh"}


class VolcengineFlashAsrProvider:
    name = "volcengine_flash"

    def __init__(self, app_key: str = "", access_key: str = "",
                 resource_id: str = RESOURCE_TURBO, timeout_s: float = 20.0) -> None:
        # ###替换成你的api：火山引擎要两个槽
        #   typefast key set volc_app_key "###替换成你的api"  /  typefast key set volc_access_key "###替换成你的api"
        self.app_key = app_key or get_secret("volc_app_key")
        self.access_key = access_key or get_secret("volc_access_key")
        self.resource_id = resource_id
        self.timeout_s = timeout_s

    def build_headers(self) -> dict:
        return {
            "X-Api-App-Key": self.app_key or "",
            "X-Api-Access-Key": self.access_key or "",
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
            "Content-Type": "application/json",
        }

    def build_payload(self, req: AsrRequest) -> dict:
        wav = pcm16_wav_bytes(req.samples, req.sample_rate)
        payload = {
            "user": {"uid": "typefast"},
            "audio": {"format": "wav", "data": base64.b64encode(wav).decode("ascii")},
            "request": {
                "model_name": "bigmodel",
                "enable_punc": True,
                "enable_itn": True,
                "language": VOLC_LANGUAGE.get((req.language or "").strip().lower(), req.language or "zh"),
            },
        }
        if req.hotwords:
            payload["request"]["context"] = {
                "hotwords": [{"word": w} for w in req.hotwords[:100]]
            }
        return payload

    def transcribe(self, req: AsrRequest) -> AsrResult:
        if not (self.app_key and self.access_key):
            raise ProviderError(ErrorKind.NOT_CONFIGURED, "未配置火山引擎 app key / access key")
        data = _post_json(FLASH_URL, self.build_headers(), self.build_payload(req), self.timeout_s)
        code = str(data.get("code", "0") or "0")
        if code == NO_SPEECH_CODE:
            raise ProviderError(ErrorKind.NO_SPEECH, "无有效语音")
        if code not in ("0", "20000000", ""):
            raise ProviderError(ErrorKind.SERVER_FAILED, "识别失败 code=" + code, str(data)[:300])
        result = data.get("result") or {}
        text = result.get("text") if isinstance(result, dict) else ""
        if not text:
            raise ProviderError(ErrorKind.NO_SPEECH, "识别结果为空")
        return AsrResult(text=str(text).strip(), provider=self.name)
