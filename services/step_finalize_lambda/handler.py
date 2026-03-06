import os, json, boto3

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
bedrock = boto3.client("bedrock-runtime")

def invoke_claude(prompt: str, max_tokens: int = 700) -> str:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }
    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps(body).encode("utf-8"),
        accept="application/json",
        contentType="application/json",
    )
    raw = resp["body"].read().decode("utf-8")
    data = json.loads(raw)

    # 回答の取得 (Claude系は content[0].text に回答が入る)
    return data.get("content", [{}])[0].get("text", "")
