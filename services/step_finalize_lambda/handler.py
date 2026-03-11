"""
Step_facts と Step_hypotheses の出力情報を元に
最終結論と推奨アクションを導出し Slack に通知する Lambda 関数

【出力項目】
severity: エラーの影響度 
  (P0:緊急・サービス停止、P1:重大障害、P2:中程度の障害、P3:軽微)
summary: エラー原因のサマリ
recommended_actions: エラー解決のための推奨アクション
  (優先度:high/medium/low)
"""

import os, json, urllib.request, boto3
from typing import Any, Dict, List
from urllib.parse import quote
from common.invoke_claude import invoke_claude  # モデル実行は共通化

secrets = boto3.client("secretsmanager")
SLACK_WEBHOOK_SECRET_NAME = os.environ.get("SLACK_WEBHOOK_SECRET_NAME", "")

# Secrets Manager から Slack Webhook URL 取得
# {"webhook_url":"https://hooks.slack.com/services/XXX/YYY/ZZZ"}
def get_slack_webhook_url() -> str:
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
def post_to_slack(text: str) -> None:
    webhook = get_slack_webhook_url()
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

# Step Functions の state（facts/hypotheses/analysis）を統合し Slack 通知用テキストに整形
def build_slack_message(
    log_group: str,
    log_stream: str,
    logs_url: str,
    facts: Dict[str, Any],
    hypotheses: List[Dict[str, Any]],
    analysis: Dict[str, Any],
) -> str:
    severity = analysis.get("severity", "P2") # severity がなければ "P2" とする
    summary = analysis.get("summary", "")
    actions = analysis.get("recommended_actions", [])

    observed_error_type = facts.get("observed_error_type", "")
    inferred_error_type = facts.get("inferred_error_type", "")
    error_type_confidence = facts.get("error_type_confidence", 0)
    function_name = facts.get("function_name", "")
    affected_service = facts.get("affected_service", "")
    key_log_lines = facts.get("key_log_lines", [])

    # 影響度に応じて絵文字表示
    severity_emoji_map = {
        "P0": "🚨",
        "P1": "⚠️",
        "P2": "🟡",
        "P3": "ℹ️",
    }
    severity_emoji = severity_emoji_map.get(severity, "🟡")

    # アクションの優先度順に並べ替え
    priority_order = {"high": 0, "medium": 1, "low": 2}
    sorted_actions = sorted(
        actions,
        key=lambda x: priority_order.get(x.get("priority", "medium"), 1)
    )

    lines: List[str] = []

    # Header
    lines.append(f"{severity_emoji} *AI SRE Assistant* [{severity}]")
    if function_name:
        lines.append(f"*Function*: `{function_name}`")
    lines.append(f"*LogGroup*: `{log_group}`")
    lines.append(f"*LogStream*: `{log_stream}`")
    lines.append(f"*Logs*: <{logs_url}|Open in CloudWatch Logs>")
    
    # Summary
    lines.append("")
    lines.append("*Summary*")
    lines.append(summary or "要約を生成できませんでした。")

    # Facts
    lines.append("")
    lines.append("*Facts*")
    if observed_error_type:
        lines.append(f"• observed_error_type: `{observed_error_type}`")
    else:
        lines.append("• observed_error_type: (not detected)")

    if inferred_error_type:
        lines.append(
            f"• inferred_error_type: `{inferred_error_type}` "
            f"(confidence: {error_type_confidence})"
        )
    else:
        lines.append("• inferred_error_type: (not inferred)")

    if affected_service:
        lines.append(f"• affected_service: `{affected_service}`")

    if key_log_lines:
        lines.append("• key_log_lines:")
        for log_line in key_log_lines[:3]:
            lines.append(f"  - `{log_line}`")

    # Log (LLMが根拠にしたログ)
    if key_log_lines:
        lines.append("• key_log_lines:")
        for log_line in key_log_lines[:3]:
            lines.append(f"  - {log_line}")

    # Hypotheses
    if hypotheses:
        lines.append("")
        lines.append("*Hypotheses*")
        for idx, h in enumerate(hypotheses[:3], start=1):
            title = h.get("title", "")
            reasoning = h.get("reasoning", "")
            confidence = h.get("confidence", 0)

            lines.append(f"{idx}. *{title}* ({confidence})")
            if reasoning:
                lines.append(f"   - {reasoning}")

    # Recommended actions
    if sorted_actions:
        lines.append("")
        lines.append("*Recommended actions*")
        for a in sorted_actions[:5]:
            priority = a.get("priority", "medium")
            action = a.get("action", "")

            if priority == "high":
                prefix = "🔴"
            elif priority == "medium":
                prefix = "🟠"
            else:
                prefix = "🔵"

            lines.append(f"{prefix} [{priority}] {action}")

    # 統合
    return "\n".join(lines)

# ロググループ URL 作成
def cloudwatch_logs_url(region: str, log_group: str, log_stream: str) -> str:
    lg = quote(log_group, safe="")
    ls = quote(log_stream, safe="")
    return f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#logsV2:log-groups/log-group/{lg}/log-events/{ls}"

# LLM のテキスト出力を、安全に Python の構造化データ (dict) に変換する簡易パーサ
# JSON 生成を LLM に任せると JSON 前後に文字が入り、json.loads() に失敗するケースがあるため
def parse_analysis(text: str) -> dict:
    analysis = {
        "severity": "P2",
        "summary": "",
        "recommended_actions": [],
    }

    mode = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("severity:"):
            analysis["severity"] = line.split(":", 1)[1].strip() or "P2"
        elif line.startswith("summary:"):
            analysis["summary"] = line.split(":", 1)[1].strip()
        elif line.startswith("recommended_actions:"):
            mode = "recommended_actions"
        elif line.startswith("- ") and mode == "recommended_actions":
            item = line[2:].strip()
            priority = "medium"
            action = item
            if item.startswith("[") and "]" in item:
                priority = item[1:item.find("]")].strip()
                action = item[item.find("]") + 1:].strip()
            analysis["recommended_actions"].append({
                "priority": priority,
                "action": action,
            })
        else:
            mode = None

    return analysis

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    log = event.get("log", {})
    facts = event.get("facts", {})
    hypotheses = event.get("hypotheses", [])

    log_group = log.get("log_group", "")
    log_stream = log.get("log_stream", "")
    log_lines = log.get("lines", [])

    region = boto3.session.Session().region_name or "ap-northeast-1"
    logs_url = cloudwatch_logs_url(region, log_group, log_stream)

    try:
        prompt = f"""
あなたは優秀な Site Reliability Engineer です。
以下の log, facts, hypotheses をもとに、インシデントの重要度・要約・推奨アクションを決定してください。

重要ルール:
- summary は日本語で 2〜3 文で簡潔に書いてください。
- AWSサービス名、例外名、HTTPステータス、RequestId などの技術用語は英語のまま扱ってください。
- observed_error_type が存在する場合は、それを最優先の根拠として扱ってください。
- observed_error_type が空で、inferred_error_type が存在する場合は、それを補助的な根拠として扱ってください。
- error_type_confidence が低い場合は断定を避けてください。
- recommended_actions は具体的な確認・対処手順にしてください。
- 危険な破壊的操作（削除・停止など）を断定的に指示しないでください。
- 説明文や補足文は不要です。

severity の定義:
- P0: 全面停止や極めて重大な障害
- P1: 主要機能に大きな影響がある重大障害
- P2: 部分的な障害、または単発・限定的な障害
- P3: 軽微な問題、影響が小さな問題

出力は次の形式を厳守してください。

severity: <P0|P1|P2|P3>
summary: <string>
recommended_actions:
- [high|medium|low] <action>
- [high|medium|low] <action>
- [high|medium|low] <action>

log:
{json.dumps(log, ensure_ascii=False)}

facts:
{json.dumps(facts, ensure_ascii=False)}

hypotheses:
{json.dumps(hypotheses, ensure_ascii=False)}

参考用ログ抜粋:
{json.dumps(log_lines[:10], ensure_ascii=False)}
"""

        llm_output = invoke_claude(prompt, max_tokens=700)
        analysis = parse_analysis(llm_output)

        # 最終出力を Slack 通知
        slack_message = build_slack_message(
            log_group=log_group,
            log_stream=log_stream,
            logs_url=logs_url,
            facts=facts,
            hypotheses=hypotheses,
            analysis=analysis,
        )
        post_to_slack(slack_message)

        return analysis

    #  実行失敗時のエラー通知
    except Exception as e:
        error_message = "\n".join(
            [
                "*AI SRE Assistant* [P2]",
                "Finalize ステップでエラーが発生しました。",
                f"Error: `{e}`",
                f"Logs: <{logs_url}|Open in CloudWatch Logs>",
            ]
        )

        try:
            post_to_slack(error_message)
        except Exception as slack_error:
            print(f"Failed to post fallback Slack message: {slack_error}")

        raise
