import os
import json
import time
import boto3
from botocore.exceptions import ClientError

"""
LLM によるエラー分析デモ用にエラーを簡単に発生させるための API
API Gateway から GET /demo?mode=timeout|iam|conditional|ok を叩くことで、
App Lambda 内で タイムアウト/例外/権限エラー/正常応答 を引き起こすことができる
"""

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])

def handler(event, context):
    qs = event.get("queryStringParameters") or {}
    mode = (qs.get("mode") or "ok").lower()

    if mode == "timeout":
        # Lambda timeout: 10s のため確実にタイムアウトさせる
        time.sleep(30)

    if mode == "conditional":
        # 例外発生（ログにTracebackが出る）
        raise Exception("ConditionalCheckFailed: demo exception for testing")

    if mode == "iam":
        # AccessDenied 発生
        try:
            table.put_item(Item={"pk": "demo", "v": "x"})
        except ClientError as e:
            print("ERROR: DynamoDB access failed", e)
            raise

    # ok
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"ok": True, "mode": mode}),
    }
