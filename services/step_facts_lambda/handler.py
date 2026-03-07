"""
エラーログから「観測された事実」だけを抽出する Lambda 関数
- 抽出には LLM を使用し、推測は禁止とする
- 可能なら一部はルールで抽出することも検討する（RequestId、function_nameなど）
"""

import json
from typing import Any, Dict
from common import invoke_claude  # モデル実行は共通化

# LLM のテキスト出力を、安全に Python の構造化データ (dict) に変換する簡易パーサ
# JSON 生成を LLM に任せると JSON 前後に文字が入り、json.loads() に失敗するケースがあるため
def parse_facts(text: str) -> Dict[str, Any]:
    facts = {"error_type": "", "timestamp": "", "affected_service": "", "http_status": "", "key_log_lines": []}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("error_type:"):
            facts["error_type"] = line.split(":", 1)[1].strip()
        elif line.startswith("timestamp:"):
            facts["timestamp"] = line.split(":", 1)[1].strip()
        elif line.startswith("affected_service:"):
            facts["affected_service"] = line.split(":", 1)[1].strip()
        elif line.startswith("http_status:"):
            facts["http_status"] = line.split(":", 1)[1].strip()
        elif line.startswith("- "):
            facts["key_log_lines"].append(line[2:].strip())
    return facts

def handler(event, context):
    log_lines = event.get("log_lines", [])

    prompt = f"""
あなたは優秀な Site Reliability Engineer です。
以下のログから「観測可能な事実のみ」を抽出してください（推測禁止）。
出力は次の形式を厳守してください。説明文は不要です。

error_type: <string or empty>
timestamp: <string or empty>
affected_service: <string or empty>
http_status: <string or empty>
key_log_lines:
- <log line 1>
- <log line 2>

ログ行:
{json.dumps(log_lines, ensure_ascii=False)}
"""

    out = invoke_claude(prompt, max_tokens=400)
    facts = parse_facts(out)

    event["facts"] = facts
    return event
