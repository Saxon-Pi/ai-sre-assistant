import os
import json
import base64
import gzip
import urllib.request
from typing import Any, Dict, List
from urllib.parse import quote
import re

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

# ロググループ URL 作成
def _cloudwatch_logs_url(region: str, log_group: str, log_stream: str) -> str:
    lg = quote(log_group, safe="")
    ls = quote(log_stream, safe="")
    return f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#logsV2:log-groups/log-group/{lg}/log-events/{ls}"

# LLM の回答 JSON の前後に余計な文字列が存在しても JSON 部分だけを抽出 (JSONDecodeError対策)
def _extract_json_object(text: str) -> str:
    if not text:
        raise ValueError("Empty model output")

    # JSON 部の { } のインデックスを取得 ({}がなければJSONなしと見なしエラー)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No JSON object found in output: {text[:200]}")

    candidate = text[start:end+1]

    # 制御文字を除去
    candidate = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", candidate)
    return candidate

# Bedrock の LLM によるエラー分析
# MVP: 推論を1回だけ行う (将来Step化しやすいフォーマットで作成)
# 指示は日本語、出力ルールは英語に分けることで、出力は日本語＋精度向上狙い
def _invoke_bedrock(log_lines: List[str]) -> Dict[str, Any]:
    prompt = f"""
あなたはSREのインシデント一次切り分けアシスタントです。
以下のCloudWatchログ行を分析し、指定されたJSONスキーマで出力してください。

【重要: 出力言語】
- summary / reasoning / action など、人が読む文章は必ず日本語で書いてください。
- AWSサービス名、API名、例外クラス名、メトリクス名、ログの引用は英語のまま保持してください。
- 技術識別子（例: DynamoDB, AccessDeniedException, ProvisionedThroughputExceededException, RequestId）は翻訳しないでください。
- もし英語の説明文が混ざった場合、その回答は不正です（技術識別子・ログ引用は除く）。
- Do NOT translate exception class names such as ConditionalCheckFailed, AccessDeniedException, ProvisionedThroughputExceededException.
- Exception names must remain exactly as they appear in logs.

【重要: フォーマット制約】
- Return ONLY valid JSON. No markdown. No extra text.
- JSONの先頭は必ず "{" で開始し、末尾は "}" で終了してください。

【重要: 内容制約】
- facts にはログから直接観測できる事実のみを書いてください（推測は禁止）。
- hypotheses は最大3つ。confidence は 0〜100 の整数。
- summary は 2〜3 文で簡潔に。

【出力JSONスキーマ】
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

【ログ行】
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

    json_str = _extract_json_object(text)
    return json.loads(json_str)

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

    try:
        # Bedrock LLM による分析
        analysis = _invoke_bedrock(lines)
    # JSON パースに失敗したらエラー
    except Exception as e:
        print("[ERROR] Bedrock/JSON parse failed:", repr(e))
        # トリガーとなったエラーのログ URL だけは通知する
        region = boto3.session.Session().region_name or "ap-northeast-1"
        logs_url = _cloudwatch_logs_url(region, log_group, log_stream)
        _post_to_slack(
            "\n".join([
                "*AI SRE Assistant* [P2]",
                "LLM出力のJSONパースに失敗しました（フォーマット崩れの可能性）。",
                f"Error: `{e}`",
                f"Logs: <{logs_url}|Open in CloudWatch Logs>",
            ])
        )
        return {"ok": False}

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
    
    # ロググループ URL
    region = boto3.session.Session().region_name or "ap-northeast-1"
    logs_url = _cloudwatch_logs_url(region, log_group, log_stream)
    msg.append(f"Logs: <{logs_url}|Open in CloudWatch Logs>")

    _post_to_slack("\n".join(msg))

    return {"ok": True}
