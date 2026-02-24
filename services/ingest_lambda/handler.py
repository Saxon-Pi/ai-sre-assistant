import os
import json
import base64
import gzip
import urllib.request
from typing import Any, Dict, List

import boto3

"""
エラーログを LLM で分析して エラー内容と原因、取るべきアクションを通知するスクリプト
LLM の出力は以下の要素で構成される
- facts: ログから取得したエラーのタイプや発生時刻など (推測NGの事実ベース)
- hypotheses: ログから読み取れる事実をもとにした LLM の推測 (confidenceは確信度)
- recommended_actions: LLM が提案する仮説検証・エラー解決のための具体的手順
- overall_assessment: 総括 (仮説の有力度、重要度)
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
# 指示は日本語、出力ルールは英語に分けることで、出力は日本語＋精度向上狙い
def _invoke_bedrock(log_lines: List[str]) -> Dict[str, Any]:
    prompt = f"""
あなたはSREのインシデント一次切り分けアシスタントです。
以下のCloudWatchログを分析し、事実→仮説→推奨アクションを構造化してください。

出力ルール:
Output Rules (Format Constraints - English):
- Return ONLY valid JSON (no markdown, no extra text).
- hypotheses must be at most 3 items.
- confidence must be an integer between 0 and 100.

Language Rules:
- All explanatory sentences must be written in Japanese.
- Keep AWS service names, exception names, and metric names in original English.
- Do NOT translate technical identifiers.

Content Rules:
- Do NOT invent facts.
- In "facts", include only information directly observable from logs.
- "summary" must be concise (2–3 sentences).

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

# Slack に分析結果を通知
def _post_to_slack(text: str) -> None:
    webhook = _get_slack_webhook_url()
    if not webhook:
        print("Slack webhook is empty; skip posting.")
        return

    payload = {"text": text}
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        res.read()

def handler(event, context):
    # エラーログの取得
    payload = _decode_cwl_payload(event)
    log_group = payload.get("logGroup", "")
    log_stream = payload.get("logStream", "")
    lines = _extract_messages(payload)

    # Bedrock LLM による分析
    analysis = _invoke_bedrock(lines)
    # LLM の推論結果
    summary = analysis.get("overall_assessment", {}).get("summary", "")     # 分析の概要
    severity = analysis.get("overall_assessment", {}).get("severity", "P2") # エラーの重要度
    facts = analysis.get("facts", {})                                       # ログから読み取れる事実
    hypos = analysis.get("hypotheses", [])                                  # エラー原因の仮説
    actions = analysis.get("recommended_actions", [])                       # 検証・解決の具体的アクション

    # Slack 通知
    # 推論結果の要素ごとにテキスト整形
    msg = []
    msg.append(f"*AI SRE Assistant*  [{severity}]")
    msg.append(f"LogGroup: `{log_group}`")
    msg.append(f"LogStream: `{log_stream}`")
    
    if summary:
        msg.append(f"\n*Summary*\n{summary}")

    if facts:
        msg.append("\n*Facts*")
        for k, v in facts.items():
            msg.append(f"• {k}: {v}")

    if hypos:
        msg.append("\n*Hypotheses*")
        for h in hypos[:3]:
            msg.append(f"• {h.get('title','')} ({h.get('confidence',0)}): {h.get('reasoning','')}")

    if actions:
        msg.append("\n*Recommended actions*")
        for a in actions[:5]:
            msg.append(f"• [{a.get('priority','medium')}] {a.get('action','')}")

    _post_to_slack("\n".join(msg))

    return {"ok": True}
