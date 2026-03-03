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

# # LLM の回答 JSON の前後に余計な文字列が存在しても JSON 部分だけを抽出 (JSONDecodeError対策)
# def _extract_json_object(text: str) -> str:
#     if not text:
#         raise ValueError("Empty model output")

#     # JSON 部の { } のインデックスを取得 ({}がなければJSONなしと見なしエラー)
#     start = text.find("{")
#     end = text.rfind("}")
#     if start == -1 or end == -1 or end <= start:
#         raise ValueError(f"No JSON object found in output: {text[:200]}")

#     candidate = text[start:end+1]

#     # 制御文字を除去
#     candidate = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", candidate)
#     return candidate

# LLM の回答をパースして JSON に変換 (モデルが JSON 出力を守らないケースがあるため)
def _parse_tag_output(text: str) -> Dict[str, Any]:
    # セクション抽出
    def section(name: str) -> str:
        m = re.search(rf"{name}:\s*(.*?)(?:\n[A-Z]+:|\Z)", text, flags=re.S)
        return (m.group(1).strip() if m else "")

    facts_s = section("FACTS")
    hypos_s = section("HYPOTHESES")
    actions_s = section("ACTIONS")
    assess_s = section("ASSESSMENT")

    facts = {}
    for line in facts_s.splitlines():
        line = line.strip()
        if line.startswith("-"):
            kv = line[1:].strip().split(":", 1)
            if len(kv) == 2:
                facts[kv[0].strip()] = kv[1].strip()

    # hypotheses
    hypotheses = []
    # 1) title... をブロックで取る
    for m in re.finditer(r"\d+\)\s*title:\s*(.*?)\n\s*reasoning:\s*(.*?)\n\s*confidence:\s*(\d+)", hypos_s, flags=re.S):
        hypotheses.append({
            "title": m.group(1).strip(),
            "reasoning": m.group(2).strip(),
            "confidence": int(m.group(3)),
        })
    # actions
    recommended_actions = []
    for line in actions_s.splitlines():
        line = line.strip()
        m = re.match(r"-\s*\[(high|medium|low)\]\s*(.+)", line)
        if m:
            recommended_actions.append({"priority": m.group(1), "action": m.group(2).strip()})
    # assessment
    severity = ""
    summary = ""
    for line in assess_s.splitlines():
        line = line.strip()
        if line.startswith("- severity:"):
            severity = line.split(":", 1)[1].strip()
        if line.startswith("- summary:"):
            summary = line.split(":", 1)[1].strip()

    return {
        "facts": {
            "error_type": facts.get("error_type", ""),
            "timestamp": facts.get("timestamp", ""),
            "affected_service": facts.get("affected_service", ""),
            "http_status": facts.get("http_status", ""),
            "key_log_lines": [facts.get("key_log_lines", "")] if facts.get("key_log_lines") else [],
        },
        "hypotheses": hypotheses[:3],
        "recommended_actions": recommended_actions[:5],
        "overall_assessment": {
            "summary": summary,
            "severity": severity or "P2",
        }
    }

# Bedrock の LLM によるエラー分析
# MVP: 推論を1回だけ行う (将来Step化しやすいフォーマットで作成)
# 指示は日本語、出力ルールは英語に分けることで、出力は日本語＋精度向上狙い
def _invoke_bedrock(log_lines: List[str]) -> Dict[str, Any]:
    prompt = f"""
あなたはSREのインシデント一次切り分けアシスタントです。
以下のCloudWatchログを分析し、必ず指定のタグ形式で出力してください。

【重要】
- 出力する文章は絶対に日本語にしてください。（技術用語・例外名・AWS名・ログ引用は原文の英語のままとする）
- JSONは絶対に出力しないでください。
- 下のタグとフォーマットを厳密に守ってください。

【出力フォーマット（厳守）】
FACTS:
- error_type: <string or empty>
- timestamp: <string or empty>
- affected_service: <string or empty>
- http_status: <string or empty>
- key_log_lines: <one-line summary of key lines>

HYPOTHESES:
1) title: <string>
   reasoning: <string>
   confidence: <0-100 integer>
2) title: <string>
   reasoning: <string>
   confidence: <0-100 integer>
3) title: <string>
   reasoning: <string>
   confidence: <0-100 integer>

ACTIONS:
- [high|medium|low] <action>
- [high|medium|low] <action>
- [high|medium|low] <action>

ASSESSMENT:
- severity: <P0|P1|P2|P3>
- summary: <2-3 sentences>

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
    return _parse_tag_output(text)

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
