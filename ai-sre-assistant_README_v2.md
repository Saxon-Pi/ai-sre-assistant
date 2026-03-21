# AI SRE アシスタント

AI SRE アシスタントは LLM を用いて CloudWatch Logs を解析し、\
インシデントの原因分析を自動生成する**AI支援SREツールのプロトタイプ**である\
\*SRE: Site Reliability Engineering

Step Functions による **LLM推論オーケストレーション**と Amazon
Bedrock（Claude）を組み合わせ、 ログから以下の情報を自動生成する

-   観測事象（Facts）
-   原因仮説（Hypotheses）
-   インシデント要約（Summary）
-   推奨アクション（Recommended actions）

------------------------------------------------------------------------

# システムの目的

インフラ・アプリのインシデント対応では以下の作業に多くの時間がかかる

-   ログ調査
-   エラー分類
-   原因推定
-   対処方針の検討

本システムはこれらを **LLM推論パイプライン**として自動化し、
初期対応の高速化を目的としている

------------------------------------------------------------------------

# システムアーキテクチャ（Agent Loop 対応）

``` mermaid
flowchart TD

A[API Gateway] --> B[Demo Lambda]
B --> C[CloudWatch Logs]
C --> D[Subscription Filter]
D --> E[Log Ingest Lambda]

E --> F[Step Functions]

F --> G[Step1: Facts Lambda]
G --> H[Initialize Loop Context]
H --> I[Step2: Hypotheses Lambda]
I --> J{Confidence Check}

J -->|top_confidence >= 85| K[Step3: Finalize Lambda]
J -->|低信頼 + retry可能| L[Prepare Retry Context]
L --> I
J -->|retry上限| K

G --> M[Amazon Bedrock]
I --> M
K --> M

K --> N[Slack]
```

------------------------------------------------------------------------

# Agent Loop（今回の追加）

仮説の信頼度（confidence）に応じて、 Hypotheses を再実行する **Agent
Loop** を実装

## 特徴

-   confidence ベースの分岐（Step Functions Choice）
-   retry_count によるループ制御
-   max_retries による無限ループ防止
-   再試行時に追加プロンプト（extra_guidance）を付与

## 現在のアプローチ

-   再試行では「追加ガイダンス」による推論改善を実施
-   新しいデータ取得は行わない（Prompt Retry 型）

## 将来の拡張

-   ログ探索範囲の拡張
-   関連サービスログ取得
-   メトリクス連携

→ **再調査型 Agent Loop へ発展可能**

------------------------------------------------------------------------

# AI 推論フロー

``` mermaid
flowchart LR

A[Logs] --> B[Facts]
B --> C[Hypotheses]
C --> D{Confidence}
D -->|低| E[Retry]
E --> C
D -->|高| F[Analysis]
F --> G[Slack]
```

------------------------------------------------------------------------

# 設計の工夫

## Prompt Chaining

推論を3段階に分離し精度を向上

-   Facts（事実抽出）
-   Hypotheses（仮説生成）
-   Analysis（最終判断）

## Explainability

Step Functions により全中間データを保持

## 観測と推定の分離

-   observed_error_type
-   inferred_error_type

## Agent Loop

-   confidence による再推論制御
-   retry context の活用

------------------------------------------------------------------------

# 技術スタック

-   AWS CDK (TypeScript)
-   AWS Lambda (Python)
-   Amazon Bedrock (Claude)
-   Step Functions
-   CloudWatch Logs
-   API Gateway
-   Slack Webhook

------------------------------------------------------------------------

# 今後の拡張

-   調査型 Agent Loop
-   RAG（類似インシデント検索）
-   メトリクス相関分析
-   コスト制御（スロットリング / クールダウン）
