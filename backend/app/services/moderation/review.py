"""TMS 机审服务（章节发布前必过，腾讯云文本内容安全）
S 验证结论：域名 tms.tencentcloudapi.com，接口 TextModeration，文本需 Base64。
命中→入待复核；接口异常→进延迟队列（不得直接上架）。
"""
from __future__ import annotations
import base64, json, logging
from typing import Dict
from ...core.config import settings

logger = logging.getLogger(__name__)

try:
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.tms.v20201229 import tms_client, models
except ImportError:
    tms_client = None


class ReviewService:
    def __init__(self):
        if tms_client is None:
            raise RuntimeError("pip install tencentcloud-sdk-python-tms")
        cred = credential.Credential(settings.TENCENT_SECRET_ID, settings.TENCENT_SECRET_KEY)
        hp = HttpProfile(); hp.endpoint = "tms.tencentcloudapi.com"
        cp = ClientProfile(); cp.httpProfile = hp
        self._client = tms_client.TmsClient(cred, settings.TENCENT_REGION, cp)

    def review_text(self, content: str, data_id: str = "") -> Dict:
        """返回 {suggestion, label, score} 或抛异常（调用方决定入延迟队列）。"""
        req = models.TextModerationRequest()
        b64 = base64.b64encode(content[:9000].encode("utf-8")).decode("utf-8")
        req.Content = b64
        req.BizType = "TencentCloudDefault"
        if data_id:
            req.DataId = data_id
        resp = self._client.TextModeration(req)
        js = json.loads(resp.to_json_string())
        return {
            "suggestion": js.get("Suggestion", ""),   # Pass|Block|Review
            "label": js.get("Label", "Normal"),
            "score": js.get("Score", 0),
        }

    def is_safe(self, content: str, data_id: str = "") -> Dict:
        """机审入口：返回 {pass: bool, suggestion, label}。
        接口异常时抛错，由上层决定进延迟队列。"""
        r = self.review_text(content, data_id)
        return {
            "pass": r["suggestion"] == "Pass",
            "suggestion": r["suggestion"],
            "label": r["label"],
            "score": r["score"],
        }
