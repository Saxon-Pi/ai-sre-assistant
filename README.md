# AI SRE アシスタント

AI SRE アシスタントは LLM を用いて CloudWatch Logs を解析し、  
インシデントの原因分析を自動生成する**AI支援SREツールのプロトタイプ**である  
*SRE: Site Reliability Engineering

Step Functions による **LLM推論オーケストレーション**と Amazon
Bedrock（Claude）を組み合わせ、ログから以下の情報を自動生成する

- 観測事象（Facts）
- 原因仮説（Hypotheses）
- インシデント要約（Summary）
- 推奨アクション（Recommended actions）

本システムはインシデント分析・対応という実務上の課題を、
以下の技術検証・学習を兼ねて実装することをテーマに作成した
- LLM オーケストレーション（Step Functions による Prompt Chaining）
- サーバレスな AIアーキテクチャ（Bedrock + Lambda）
- プロンプトエンジニアリング
- LLM 出力構造化

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

A[API Gateway] --> B[Demo Lambda <br>（エラー発生起点）]
B --> C[CloudWatch Logs]
C --> D[Subscription Filter]
D --> E[Log Ingest Lambda <br>（エラーログ取得、<br>ステートマシン起動）]

E --> F[Step Functions]

F --> G[Step1: Facts Lambda <br>（エラー情報の抽出）]
G --> H[Initialize Loop Context <br>（retry_count 初期化）]
H --> I[Step2: Hypotheses Lambda <br>（原因仮説の生成）]
I --> J{Confidence Check}

J -->|top_confidence >= 85| K[Step3: Finalize Lambda <br>（最終判断、<br>推奨アクション生成）]
J -->|低confidence + 再試行可能| L[Prepare Retry Context]
L --> I
J -->|再試行回数上限| K

G --> M[Amazon Bedrock]
I --> M
K --> M

K --> N[Slack 通知]
```

------------------------------------------------------------------------

# Agent Loop

仮説の信頼度（confidence）に応じて、 Hypotheses（仮説生成ステップ）を再実行する **Agent
Loop** を実装

## 特徴

-   confidence ベースの分岐（Step Functions Choice）
-   retry_count、max_retries によるループ制御（無限ループ防止）
-   再試行時に追加プロンプト（extra_guidance）を付与

## 現在のアプローチ

-   再試行では「追加ガイダンス」による推論改善を実施
-   追加情報の付与は行わない

## 今後の改善ポイント

-   再試行時のログ探索範囲の拡張（幅広い視点で推論させる）
-   CloudWatch Metrics 連携（推論生成の参考情報の追加）

------------------------------------------------------------------------

# AI 推論フロー（Prompt Chaining）

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

インシデント分析を単一の LLM で行うのではなく、
複数の LLM 推論を段階的に実行する **Prompt Chaining** を採用している 

メリット:

- 推論精度向上
- 推論過程の可視化
- デバッグ容易性
- プロンプト制御性

------------------------------------------------------------------------

# サンプル出力
以下の画像は実際にエラー分析を実行した際の Slack 通知内容となる  

------------------------------------------------------------------------

# 設計の工夫ポイント

本システムでは、単一の LLM 呼び出しでインシデント分析を完結させるのではなく  
**Facts → Hypotheses → Analysis** の3段階に推論を分割している

この構成を採用した理由について、次のことが挙げられる

### ① 推論精度の向上

インシデント分析を以下の3段階に分割することで、各ステップの役割が明確になり推論精度が向上する

- 事象抽出
- 原因の仮説生成
- 最終判断

### ② 推論過程の可視化

Step Functions により各ステップの入出力を保持することで LLM がどのような情報を元に判断したかを追跡できる

これはインシデント分析において重要な
**説明可能性（Explainability）** の確保にもつながる

### ③ 観測事象と推定の分離

ログから直接確認できる事実と LLM が推定した情報を区別し、分けて出力している

- `observed_error_type`
- `inferred_error_type`

これにより以下を明確に区別できるような設計にしている

- ログに基づく確定情報
- LLM による推定

## ④ Agent Loop による LLM 出力の精度向上
曖昧なログが入力された時など、仮説 confidence が低い場合は仮説生成を再実行する   
再実行のための retry context を設けることで追加指示や入力情報の拡張をすることが可能  

### ⑤ 将来拡張しやすいアーキテクチャ

Step Functions を採用することで、将来的に以下のような拡張を容易に追加できる

- Choice 分岐による追加調査
- Agent 型のインシデント調査フロー
- メトリクスや関連ログの追加取得

------------------------------------------------------------------------

# 技術スタック

AWS

- AWS CDK (TypeScript)
- AWS Lambda (Python)
- Amazon Bedrock (Claude 3)
- AWS Step Functions
- CloudWatch Logs
- API Gateway

Integration

- Slack Webhook

AI Engineering

- Prompt Engineering
- Prompt Chaining
- Agent Loop
- Structured LLM Output
- LLM Orchestration

------------------------------------------------------------------------

# 今後の拡張（予定）

- 調査型 Agent Loop
- 類似インシデント検索（Vector DB / RAG）
- CloudWatch Metrics 分析
- コスト制御（スロットリング / クールダウン）
