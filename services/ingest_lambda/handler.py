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

# ログ内容の取得 (デフォルトは末尾最大30行まで)
def _extract_messages(payload: Dict[str, Any], max_lines: int = 30) -> List[str]:
    msgs = []
    for le in payload.get("logEvents", []):
        m = le.get("message", "").strip()
        if m:
            msgs.append(m)
    return msgs[-max_lines:]

# Bedrock の LLM によるエラー分析
# MVP: 推論を1回だけ行う (将来Step化しやすいフォーマットで作成)
def _invoke_bedrock(log_lines: List[str]) -> Dict[str, Any]:
    prompt = f"""
あなたはSREのインシデント一次切り分けアシスタントです。
以下のCloudWatchログ行を分析し、事実・仮説・推奨アクションを構造化してください。

Important:
- Return ONLY valid JSON (no markdown, no extra text).
- Do NOT invent facts. Put only evidence-based items in facts.
- hypotheses must be top 3 and include confidence 0-100.

Output JSON schema:
{{
  "facts": {{
    "error_type": "",
    "timestamp": "",
    "affected_service": "",
    "http_status": "",
    "key_log_lines": []
  }},
  "hypotheses": [
    {{"title":"","reasoning":"","confidence":0}}
  ],
  "recommended_actions": [
    {{"action":"","priority":"high|medium|low"}}
  ],
  "overall_assessment": {{
    "summary":"",
    "severity":"P0|P1|P2|P3"
  }}
}}

Log lines:
{json.dumps(log_lines, ensure_ascii=False)}
"""

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 800,
        "temperature": 0.2,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ],
    }

    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps(body).encode("utf-8"),
        accept="application/json",
        contentType="application/json",
    )

    raw = resp["body"].read().decode("utf-8")
    data = json.loads(raw)

    # 回答の取得 (Claude系は content[0].text に回答が入る)
    text = data.get("content", [{}])[0].get("text", "")
    return json.loads(text)



def handler(event, context):
    payload = _decode_cwl_payload(event)
    log_group = payload.get("logGroup", "")
    log_stream = payload.get("logStream", "")
    lines = _extract_messages(payload)
