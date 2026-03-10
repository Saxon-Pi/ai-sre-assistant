"""
Step_facts の出力情報を元にエラー原因の仮説を立てる Lambda 関数
"""

import json
from typing import Any, Dict, List
from common import invoke_claude  # モデル実行は共通化

# LLM のテキスト出力を、安全に Python の構造化データ (dict) に変換する簡易パーサ
# JSON 生成を LLM に任せると JSON 前後に文字が入り、json.loads() に失敗するケースがあるため
def parse_hypotheses(text: str) -> List[Dict[str, Any]]:
    hypotheses: List[Dict[str, Any]] = []
    current: Dict[str, Any] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("1)") or line.startswith("2)") or line.startswith("3)"):
            if current:
                hypotheses.append(current)
            current = {
                "title": "",
                "reasoning": "",
                "confidence": 0,
            }
        elif line.startswith("title:") and current is not None:
            current["title"] = line.split(":", 1)[1].strip()
        elif line.startswith("reasoning:") and current is not None:
            current["reasoning"] = line.split(":", 1)[1].strip()
        elif line.startswith("confidence:") and current is not None:
            value = line.split(":", 1)[1].strip()
            try:
                current["confidence"] = int(value)
            except ValueError:
                current["confidence"] = 0

    if current:
        hypotheses.append(current)

    return hypotheses[:3]

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    facts: Dict[str, Any] = event.get("facts", {})

    # ログに明示されている例外名・エラー種別
    observed_error_type = facts.get("observed_error_type", "")
    # ログ内容から前ステップで推定したエラー種別と信頼度
    inferred_error_type = facts.get("inferred_error_type", "")
    error_type_confidence = facts.get("error_type_confidence", 0)
    # 事実ベースのエラー種別を優先
    primary_error_type = observed_error_type or inferred_error_type

    prompt = f"""
あなたは優秀な Site Reliability Engineer です。
以下のfactsをもとに、エラー原因の仮説を最大3つ生成してください。

重要ルール:
- observed_error_type が存在する場合は、それを最優先の根拠として扱ってください。
- observed_error_type が空で、inferred_error_type が存在する場合は、それを補助的な根拠として扱ってください。
- error_type_confidence が低い場合は、断定を避けて幅広い仮説を出してください。
- facts から読み取れないことを断定しないでください。
- 仮説は「原因候補」であり、確定診断ではありません。
- reasoning は日本語で書いてください。
- AWSサービス名、例外名、HTTPステータスなどの技術用語は英語のまま扱ってください。
- confidence は、その仮説自体の確信度を 0〜100 の整数で出してください。
- 説明文や補足文は不要です。

出力は次の形式を厳守してください。

1)
title: <string>
reasoning: <string>
confidence: <0-100 integer>

2)
title: <string>
reasoning: <string>
confidence: <0-100 integer>

3)
title: <string>
reasoning: <string>
confidence: <0-100 integer>

参考情報:
primary_error_type: {primary_error_type}
observed_error_type: {observed_error_type}
inferred_error_type: {inferred_error_type}
error_type_confidence: {error_type_confidence}

facts:
{json.dumps(facts, ensure_ascii=False)}
"""

    llm_output = invoke_claude(prompt, max_tokens=700)
    hypotheses = parse_hypotheses(llm_output)

    return hypotheses
