"""
エラーログから主に「観測された事実」を抽出する Lambda 関数
※ 事実ベースで情報を抽出できない場合に備えて一部は推論する (後続ステップで使い分ける)

【事実項目】
※ 抽出不可の場合は空
"observed_error_type": "",  # ログに明示されている例外名・エラー種別
"timestamp": "",            # エラー発生時刻
"affected_service": "",     # 影響を受けたAWSサービスやコンポーネント
"http_status": "",          # 関連するHTTPステータスコード
"request_id": "",           # リクエスト単位の識別子
"function_name": "",        # エラーが発生したLambda関数名
"key_log_lines": [],        # 根拠として重要なログ行

【推論項目】
"inferred_error_type": "",  # ログ内容からLLMが推定したエラー種別
"error_type_confidence": 0, # inferred_error_type の推定信頼度

【出力イメージ】
"facts": {
    "observed_error_type": "ConditionalCheckFailed",
    "inferred_error_type": "DynamoDB conditional check failure",
    "error_type_confidence": 90,
    "timestamp": "",
    "affected_service": "DynamoDB",
    "http_status": "",
    "request_id": "",
    "function_name": "AiSreAssistantStack-AppLambda46D23914-nKXsWjUMarqM",
    "key_log_lines": [
      "\"[ERROR] Exception: ConditionalCheckFailed: demo exception for testing\"",
      "\"Traceback (most recent call last):\"",
      "\"  File \\\"/var/task/handler.py\\\", line 29, in handler\\n    raise Exception(\\\"ConditionalCheckFailed: demo exception for testing\\\")\""
    ]
  }
"""

import json
from typing import Any, Dict, List
from common.invoke_claude import invoke_claude  # モデル実行は共通化

# Lambda の関数名を取得
def extract_function_name_from_log_group(log_group: str) -> str:
    prefix = "/aws/lambda/"
    if log_group.startswith(prefix):
        return log_group[len(prefix):]
    return ""

# LLM のテキスト出力を、安全に Python の構造化データ (dict) に変換する簡易パーサ
# JSON 生成を LLM に任せると JSON 前後に文字が入り、json.loads() に失敗するケースがあるため
def parse_facts(text: str) -> Dict[str, Any]:
    facts = {
        "observed_error_type": "",  # ログに明示されている例外名・エラー種別
        "inferred_error_type": "",  # ログ内容からLLMが推定したエラー種別
        "error_type_confidence": 0, # inferred_error_type の推定信頼度
        "timestamp": "",            # エラー発生時刻
        "affected_service": "",     # 影響を受けたAWSサービスやコンポーネント
        "http_status": "",          # 関連するHTTPステータスコード
        "request_id": "",           # リクエスト単位の識別子
        "function_name": "",        # エラーが発生したLambda関数名
        "key_log_lines": [],        # 根拠として重要なログ行
    }

    mode = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("observed_error_type:"):
            facts["observed_error_type"] = line.split(":", 1)[1].strip()
        elif line.startswith("inferred_error_type:"):
            facts["inferred_error_type"] = line.split(":", 1)[1].strip()
        elif line.startswith("error_type_confidence:"):
            value = line.split(":", 1)[1].strip()
            try:
                facts["error_type_confidence"] = int(value)
            except ValueError:
                facts["error_type_confidence"] = 0
        elif line.startswith("timestamp:"):
            facts["timestamp"] = line.split(":", 1)[1].strip()
        elif line.startswith("affected_service:"):
            facts["affected_service"] = line.split(":", 1)[1].strip()
        elif line.startswith("http_status:"):
            facts["http_status"] = line.split(":", 1)[1].strip()
        elif line.startswith("request_id:"):
            facts["request_id"] = line.split(":", 1)[1].strip()
        elif line.startswith("function_name:"):
            facts["function_name"] = line.split(":", 1)[1].strip()
        elif line.startswith("key_log_lines:"):
            mode = "key_log_lines"
        elif line.startswith("- ") and mode == "key_log_lines":
            facts["key_log_lines"].append(line[2:].strip())
        else:
            mode = None

    return facts

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    log = event.get("log", {})
    log_lines: List[str] = log.get("lines", [])
    log_group: str = log.get("log_group", "")

    function_name = extract_function_name_from_log_group(log_group)

    prompt = f"""
[ROLE]
あなたは経験豊富な Site Reliability Engineer です。
AWS、CloudWatch Logs、分散システム障害の初動分析に精通しています。

[OBJECTIVE]
以下のCloudWatch Logsをもとに、観測できる事実と、必要最小限の推定情報を抽出してください。
このステップでは、後続の仮説生成に使うための構造化 facts を作成することが目的です。

[REASONING RULES]
- observed_error_type には、ログに明示されている例外名・エラー種別のみを書いてください。
- ログに明示されていない場合は、observed_error_type は空にしてください。
- inferred_error_type には、ログ内容から推定できるエラー種別を書いてください。
- inferred_error_type が推定できない場合は空にしてください。
- observed_error_type と inferred_error_type を混同しないでください。
- facts に含める情報は、ログまたは参考情報から妥当と判断できる範囲に限定してください。
- key_log_lines には、後続の仮説生成に役立つ重要なログ行を 1〜3 行含めてください。
- function_name には、ログに明示されていない場合でも、参考情報の function_name_from_log_group を優先的に使ってください。

[CONFIDENCE RULES]
- error_type_confidence は inferred_error_type に対する確信度を 0〜100 の整数で出してください。
- 根拠が弱い場合は高すぎる confidence を付けないでください。

[OUTPUT SCHEMA]
説明文や補足文は不要です。
必ず次の形式だけで出力してください。

observed_error_type: <string or empty>
inferred_error_type: <string or empty>
error_type_confidence: <0-100 integer>
timestamp: <string or empty>
affected_service: <string or empty>
http_status: <string or empty>
request_id: <string or empty>
function_name: <string or empty>
key_log_lines:
- <log line 1>
- <log line 2>
- <log line 3>

[EXAMPLE]
observed_error_type: ConditionalCheckFailed
inferred_error_type: DynamoDB conditional check failure
error_type_confidence: 90
timestamp:
affected_service: DynamoDB
http_status:
request_id:
function_name: sample-lambda-function
key_log_lines:
- [ERROR] Exception: ConditionalCheckFailed: demo exception for testing
- Traceback (most recent call last):
- File "/var/task/handler.py", line 29, in handler

[INPUT DATA]
function_name_from_log_group: {function_name}

log_lines:
{json.dumps(log_lines, ensure_ascii=False)}
"""

    llm_output = invoke_claude(prompt, max_tokens=500)
    facts = parse_facts(llm_output)

    # log_group から取得できる function_name は補完する
    if not facts["function_name"]:
        facts["function_name"] = function_name

    return facts
