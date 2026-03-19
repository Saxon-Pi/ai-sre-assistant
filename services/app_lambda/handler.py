import os
import json
import time
import boto3
from botocore.exceptions import ClientError

"""
LLM によるエラー分析デモ用にエラーを簡単に発生させるための API
API Gateway から GET /demo?mode=timeout|iam|conditional|ok を叩くことで、
App Lambda 内で タイムアウト/例外/権限エラー/正常応答 を引き起こすことができる

【実行方法】
スタック出力の ApiUrl に demo?mode=<エラー種別> を追加して、
API Gateway にアクセスすると擬似エラーを発生させることができる

 - timeout:     タイムアウト
 - conditional: 条件付き書き込み失敗
 - iam:         権限不足
 - ambiguous:   原因が曖昧なエラー (仮説のconfidenceを低くするため)
 - badjson:     複数の原因が推測されるエラー (仮説のバリエーションを確認するため)
 - throttle:    スロットリング

例：
https://xxx.execute-api.ap-northeast-1.amazonaws.com/dev/demo?mode=timeout
"""

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])

def handler(event, context):
    qs = event.get("queryStringParameters") or {}
    mode = (qs.get("mode") or "ok").lower()

    # タイムアウト発生
    if mode == "timeout":
        print("ERROR: about to timeout intentionally (demo)")
        # CloudWatch に flush される猶予を作る
        time.sleep(1)
        # Lambda timeout: 10s のため確実にタイムアウトさせる
        time.sleep(30)

    # 条件付き書き込みの失敗
    if mode == "conditional":
        # 例外発生（ログにTracebackが出る）
        raise Exception("ConditionalCheckFailed: demo exception for testing")

    # 権限不足によるアクセス拒否
    if mode == "iam":
        # AccessDenied 発生
        try:
            table.put_item(Item={"pk": "demo", "v": "x"})
        except ClientError as e:
            print("ERROR: DynamoDB access failed", e)
            raise
    
    # 曖昧なエラー (仮説のconfidenceが低くなる)
    if mode == "ambiguous":
        print("ERROR: unexpected processing failure detected")
        raise Exception("operation failed")
    
    # 原因が複数考えられるような複雑なエラー (アプリ側バグ/外部依存先のレスポンス異常/データ形式不一致 など)
    if mode == "badjson":
        fake_response = "<<<invalid-json>>>"
        print("ERROR: failed to parse downstream response")
        print(f"raw_response={fake_response}")
        raise ValueError("JSONDecodeError: Expecting value: line 1 column 1 (char 0)")
    
    # スロットリング (設定/負荷/一時障害 など推奨アクションを多様化させる狙い)
    if mode == "throttle":
        print("ERROR: upstream request throttled")
        raise Exception("ProvisionedThroughputExceededException: Rate exceeded for demo request")

    # ok
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"ok": True, "mode": mode}),
    }
