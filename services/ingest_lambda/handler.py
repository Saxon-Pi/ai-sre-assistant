"""
CloudWatch Logs (サブスクリプションフィルタ) からエラーログを取得し、
LLM 分析用 StepFunctions をトリガする Lambda 関数
"""

import os
import json
import base64
import gzip
from typing import Any, Dict, List
from urllib.parse import quote
import boto3

sfn = boto3.client("stepfunctions")
STATEMACHINE_ARN = os.environ.get("STATEMACHINE_ARN", "")

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

def handler(event, context):
    # エラーログの取得
    payload = _decode_cwl_payload(event)
    log_group = payload.get("logGroup", "")
    log_stream = payload.get("logStream", "")
    lines = _extract_messages(payload)

    if not STATEMACHINE_ARN:
        raise RuntimeError("STATEMACHINE_ARN is empty")

    # ステートマシンの入力
    input_obj = {
        "source": "cloudwatch-logs",
        "log_group": log_group,
        "log_stream": log_stream,
        "log_lines": lines,
    }

    sfn.start_execution(
        stateMachineArn=STATEMACHINE_ARN,
        input=json.dumps(input_obj, ensure_ascii=False),
    )

    return {"ok": True}
