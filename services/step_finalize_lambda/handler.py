"""
Step_facts と Step_hypotheses の出力情報を元に
最終結論と推奨アクションを導出し Slack に通知する Lambda 関数
"""

import os, json, urllib.request, boto3
from typing import Any, Dict, List
from urllib.parse import quote
from common import invoke_claude  # モデル実行は共通化

secrets = boto3.client("secretsmanager")
SLACK_WEBHOOK_SECRET_NAME = os.environ.get("SLACK_WEBHOOK_SECRET_NAME", "")

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

# ロググループ URL 作成
def _cloudwatch_logs_url(region: str, log_group: str, log_stream: str) -> str:
    lg = quote(log_group, safe="")
    ls = quote(log_stream, safe="")
    return f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#logsV2:log-groups/log-group/{lg}/log-events/{ls}"

def handler(event, context):
    region = boto3.session.Session().region_name or "ap-northeast-1"
    log_group = event.get("log_group", "")
    log_stream = event.get("log_stream", "")
    facts = event.get("facts", {})
    hypotheses = event.get("hypotheses", [])

    prompt = f"""
