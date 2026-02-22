import os
import json
import base64
import gzip
from typing import Any, Dict, List

import boto3

"""
エラーログを LLM で分析して エラー内容と原因、取るべきアクションを通知するスクリプト
"""

secrets = boto3.client("secretsmanager")
SLACK_WEBHOOK_SECRET_NAME = os.environ.get("SLACK_WEBHOOK_SECRET_NAME", "")
_cached_webhook_url = None

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
bedrock = boto3.client("bedrock-runtime")

# Secrets Manager から Slack Webhook URL 取得
# {"webhook_url":"https://hooks.slack.com/services/XXX/YYY/ZZZ"}
def _get_slack_webhook_url() -> str:
    global _cached_webhook_url
    if _cached_webhook_url is not None:
        return _cached_webhook_url

    if not SLACK_WEBHOOK_SECRET_NAME:
        return ""

    res = secrets.get_secret_value(SecretId=SLACK_WEBHOOK_SECRET_NAME)
    secret_str = res.get("SecretString", "")
    if not secret_str:
        return ""

    obj = json.loads(secret_str)
    _cached_webhook_url = obj.get("webhook_url", "")
    return _cached_webhook_url

# event のデコード (サブスクリプションフィルタは Base64エンコード、gzip圧縮)
def _decode_cwl_payload(event: Dict[str, Any]) -> Dict[str, Any]:
    data = base64.b64decode(event["awslogs"]["data"])
    unzipped = gzip.decompress(data)
    return json.loads(unzipped)

# ログ内容の取得 (デフォルトは30行まで)
def _extract_messages(payload: Dict[str, Any], max_lines: int = 30) -> List[str]:
    msgs = []
    for le in payload.get("logEvents", []):
        m = le.get("message", "").strip()
        if m:
            msgs.append(m)
    return msgs[-max_lines:]


def handler(event, context):
    payload = _decode_cwl_payload(event)
    log_group = payload.get("logGroup", "")
    log_stream = payload.get("logStream", "")
    lines = _extract_messages(payload)
