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
"""

import json
from typing import Any, Dict, List
from common import invoke_claude  # モデル実行は共通化

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

def handler(event, context):
    log_lines: List[str] = event.get("log_lines", [])
    log_group: str = event.get("log_group", "")

    function_name = extract_function_name_from_log_group(log_group)

    prompt = f"""
あなたは優秀な Site Reliability Engineer です。
以下のCloudWatchログから、観測できる事実と、必要最小限の推定情報を抽出してください。

重要ルール:
- observed_error_type には、ログに明示されている例外名・エラー種別のみを書いてください。
- ログに明示されていない場合は、observed_error_type は空にしてください。
- inferred_error_type には、ログ内容から推定できるエラー種別を書いてください。
- inferred_error_type が推定できない場合は空にしてください。
- error_type_confidence は inferred_error_type に対する確信度を 0〜100 の整数で出してください。
- 事実と推定を混同しないでください。
- 説明文や補足文は不要です。
- 文章は日本語とし、AWSサービス名、例外名、HTTPステータス、RequestId などの技術用語は英語のまま扱ってください。
- key_log_lines には根拠になる重要なログ行を 1〜3 行入れてください。

出力は次の形式を厳守してください。

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

参考情報:
function_name_from_log_group: {function_name}

ログ行:
{json.dumps(log_lines, ensure_ascii=False)}
"""

    llm_output = invoke_claude(prompt, max_tokens=500)
    facts = parse_facts(llm_output)

    # log_group から取得できる function_name は補完する
    if not facts["function_name"]:
        facts["function_name"] = function_name

    return facts
