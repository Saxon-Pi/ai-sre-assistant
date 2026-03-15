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

インフラ・アプリのインシデント対応では、以下の作業に多くの時間が掛かる

-   ログ調査
-   エラー分類
-   原因推定
-   対処方針の検討

AI SRE アシスタント はこれらの作業を
**LLM推論パイプライン**として自動化し、インシデントの初期対応を支援することを目的としている

------------------------------------------------------------------------

# システムアーキテクチャ

``` mermaid
flowchart TD

A[API Gateway] --> B[Demo Lambda <br> （エラー発生起点）]

B --> C[CloudWatch Logs]

C --> D[Subscription Filter]

D --> E[Log Ingest Lambda <br>（エラーログの取得、<br>ステートマシン起動）]

E --> F[Step Functions]

F --> G[Step1: Facts Lambda <br> （エラー情報の抽出）]
G --> H[Step2: Hypotheses Lambda <br> （エラー原因の仮説生成）]
H --> I[Step3: Finalize Lambda <br> （最終判断、<br>推奨アクション生成）]

G --> J[Amazon Bedrock Claude]
H --> J
I --> J
I --> K[Slack Notification <br> （分析結果の通知）]
```

## アーキテクチャポイント

- **CloudWatch Logs Subscription Filter** が分析パイプラインを自動起動
- **Step Functions** により LLM 推論を段階的に制御
- **Amazon Bedrock Claude** がログ分析を実行
- **Slack 通知** によりインシデント情報を即時共有

------------------------------------------------------------------------

# AI 推論フロー（Prompt Chaining）

``` mermaid
flowchart LR

A[Logs] --> B[Facts Extraction]
B --> C[Hypothesis Generation]
C --> D[Incident Analysis]
D --> E[Slack Notification]
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
追加予定  
1. Slack通知の抜粋  
2. Step Functions の最終 state の抜粋  

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

### ④ 将来拡張しやすいアーキテクチャ

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
- Structured LLM Output
- LLM Orchestration

------------------------------------------------------------------------

# 今後の拡張（予定）

- Choice分岐による調査フロー
- Agent型インシデント調査
- 類似インシデント検索（Vector DB / RAG）
- 長期インシデントメモリ
- CloudWatch Metrics 相関分析
- 推論の重複抑制 / クールダウン
