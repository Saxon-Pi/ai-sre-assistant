"""
Step_facts の出力情報を元にエラー原因の仮説を立てる Lambda 関数

【出力項目】
以下の項目をセットで最大3つ出力する
title：問題の見出し
reasoning：推測したエラー原因
confidence：回答の信頼度（モデルの主観的数値）

【出力イメージ】
"hypotheses": [
    {
      "title": "Incorrect DynamoDB Conditional Check",
      "reasoning": "The observed error type is \"ConditionalCheckFailed\", which indicates that a DynamoDB conditional check failed during the operation. This could be caused by the application attempting to update or insert a record that does not meet the expected conditions, such as a specific attribute value or state.",
      "confidence": 90
    },
    {
      "title": "Incorrect DynamoDB Table Schema",
      "reasoning": "The inferred error type is \"DynamoDB conditional check failure\", which suggests that the issue may be related to the DynamoDB table schema. The application may be attempting to perform an operation that is not compatible with the current table structure, leading to the conditional check failure.",
      "confidence": 80
    },
    {
      "title": "Transient DynamoDB Service Issue",
      "reasoning": "The error type confidence is relatively high (90%), but the observed and inferred error types do not provide a clear indication of the root cause. It is possible that the issue is a transient problem with the DynamoDB service, such as a temporary outage or increased latency, which could result in the conditional check failure.",
      "confidence": 70
    }
  ]
"""

import json
from typing import Any, Dict, List
from common.invoke_claude import invoke_claude  # モデル実行は共通化

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

# 出力のデータ構造を整形 (後続の choice ステートで使用する情報を付加)
def build_hypotheses_result(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    top_confidence = 0
    if items:
        top_confidence = max(item.get("confidence", 0) for item in items)

    return {
        "items": items,                   # 仮説のリスト (title, reasoning, confidenceのセット)
        "top_confidence": top_confidence, # リスト内の confidence の最大値
        "count": len(items),              # 生成された仮説の件数
    }

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    facts: Dict[str, Any] = event.get("facts", {})

    # 再試行時の情報
    loop: Dict[str, Any] = event.get("loop", {})
    investigation: Dict[str, Any] = event.get("investigation", {})
    retry_count = loop.get("retry_count", 0)
    retry_reason = investigation.get("retry_reason", "")
    extra_guidance = investigation.get("extra_guidance", "")

    # ログに明示されている例外名・エラー種別
    observed_error_type = facts.get("observed_error_type", "")
    # ログ内容から前ステップで推定したエラー種別と信頼度
    inferred_error_type = facts.get("inferred_error_type", "")
    error_type_confidence = facts.get("error_type_confidence", 0)
    # 事実ベースのエラー種別を優先
    primary_error_type = observed_error_type or inferred_error_type

    prompt = f"""
[ROLE]
あなたは経験豊富な Site Reliability Engineer です。
AWS、CloudWatch Logs、分散システム障害の原因分析に精通しています。

[OBJECTIVE]
与えられた facts をもとに、エラー原因の仮説を最大3つ生成してください。
仮説は、運用担当者が初動対応や追加調査に使えるレベルの具体性を持たせてください。

[REASONING RULES]
- observed_error_type が存在する場合は、最優先の根拠として扱ってください。
- observed_error_type が空で、inferred_error_type が存在する場合は、補助的な根拠として扱ってください。
- error_type_confidence が低い場合は、断定を避けて幅広い仮説を出してください。
- facts に含まれていない情報を断定してはいけません。
- 仮説は「原因候補」であり、確定診断ではありません。
- 仮説は confidence が高い順に並べてください。
- 同じ内容を表現違いで重複させないでください。
- 一般論ではなく、与えられた facts にできるだけ結びついた仮説を出してください。
- reasoning には、必ず facts または key_log_lines に基づく根拠を含めてください。
- retry_count が 0 より大きい場合は、前回よりも facts と key_log_lines に強く結びついた仮説を優先してください。
- retry_count が 0 より大きい場合は、一般的すぎる仮説を避けてください。

[LANGUAGE RULES]
- title は必ず日本語で書いてください。
- reasoning は必ず日本語で書いてください。
- 英語の文章を書いてはいけません。
- ただし AWSサービス名、例外名、HTTPステータス、RequestId、CloudWatch Logs に含まれる原文は英語のまま使用してください。
- 例外名やサービス名を不自然に和訳しないでください。
- 内部的な推論は英語で行っても構いませんが、最終出力は必ず日本語にしてください。

[CONFIDENCE RULES]
- confidence は 0〜100 の整数で出してください。
- confidence は「その仮説の信頼度」を表し、根拠が弱い場合は低い数値にしてください。
- observed_error_type、inferred_error_type、key_log_lines に明確な AWS サービス名や例外名が観測されない場合、confidence は 80 未満にしてください。
- observed_error_type が空で、inferred_error_type が抽象的な表現の場合、top confidence は 70 以下にしてください。
- facts.error_type_confidence を上回る confidence を安易に付けてはいけません。

[OUTPUT SCHEMA]
説明文や補足文は不要です。
必ず次の形式だけで出力してください。

1)
title: <必ず日本語の仮説タイトル。英語の文章は禁止>
reasoning: <必ず日本語の説明。技術用語のみ英語可>
confidence: <0-100 integer>

2)
title: <必ず日本語の仮説タイトル。英語の文章は禁止>
reasoning: <必ず日本語の説明。技術用語のみ英語可>
confidence: <0-100 integer>

3)
title: <必ず日本語の仮説タイトル。英語の文章は禁止>
reasoning: <必ず日本語の説明。技術用語のみ英語可>
confidence: <0-100 integer>

[EXAMPLE]
1)
title: DynamoDB 条件付きチェックの不一致
reasoning: observed_error_type に ConditionalCheckFailed が含まれているため、DynamoDB の条件付き更新が期待した条件を満たしていない可能性があります。
confidence: <0-100 integer>

2)
title: アプリケーションの状態管理ロジック不整合
reasoning: 条件式が前提とするデータ状態と、実際のデータ状態が一致していない可能性があります。
confidence: <0-100 integer>

3)
title: 一時的な依存先の不整合
reasoning: 依存するデータ更新タイミングのずれにより、条件付きチェックが一時的に失敗した可能性があります。
confidence: <0-100 integer>

[RETRY CONTEXT]
retry_count: {retry_count}
retry_reason: {retry_reason}
extra_guidance: {extra_guidance}

[INPUT DATA]
primary_error_type: {primary_error_type}
observed_error_type: {observed_error_type}
inferred_error_type: {inferred_error_type}
error_type_confidence: {error_type_confidence}

facts:
{json.dumps(facts, ensure_ascii=False)}
"""

    llm_output = invoke_claude(prompt, max_tokens=700)
    items = parse_hypotheses(llm_output)
    return build_hypotheses_result(items)
