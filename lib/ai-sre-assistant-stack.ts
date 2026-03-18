import * as cdk from 'aws-cdk-lib/core';
import { Construct } from 'constructs';
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigw from "aws-cdk-lib/aws-apigateway";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as logs_destinations from "aws-cdk-lib/aws-logs-destinations";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import * as path from "path";

const modelId = process.env.BEDROCK_MODEL_ID ?? "anthropic.claude-3-haiku-20240307-v1:0";

export class AiSreAssistantStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // =====================================================
    // アプリケーション（意図的に不具合を発生させるデモ用アプリ）
    // =====================================================

    // DynamoDB
    const table = new dynamodb.Table(this, "DemoTable", {
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // デモ用なので削除OK
    });

    // App Lambda
    const appFn = new lambda.Function(this, "AppLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../services/app_lambda")),
      environment: {
        TABLE_NAME: table.tableName,
      },
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
    });
    //table.grantReadWriteData(appFn);
    table.grantReadData(appFn); // Read only で権限エラーを発生させる

    // App Lambda ロググループ (サブスクリプションフィルタと確実に連携するため明示)
    /*
    const appLogGroup = new logs.LogGroup(this, "AppLambdaLogGroup", {
      logGroupName: `/aws/lambda/${appFn.functionName}`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    */

    // 既存ロググループを参照
    const appLogGroup = logs.LogGroup.fromLogGroupName(
      this,
      "AppLambdaLogGroup",
      `/aws/lambda/${appFn.functionName}`
    );

    // API Gateway
    const api = new apigw.RestApi(this, "DemoApi", {
      restApiName: "ai-sre-assistant-demo",
      deployOptions: { stageName: "dev" },
    });
    const root = api.root.addResource("demo");
    root.addMethod("GET", new apigw.LambdaIntegration(appFn));

    // =====================================================
    // LLM 実行 Lambda
    // =====================================================

    // Secret Manager (Slack Incoming Webhook URL用)
    // {"webhook_url":"https://hooks.slack.com/services/XXX/YYY/ZZZ"}
    const slackWebhookSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "SlackWebhookSecret",
      "slack/webhook/ai-sre-assistant"
    );

    // Bedrock 呼び出し共通部の Lambda Layer
    const commonLayer = new lambda.LayerVersion(this, "CommonLayer", {
      code: lambda.Code.fromAsset(path.join(__dirname, "../layers/shared")),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      description: "Common utilities for AI SRE Assistant",
    });

    // Ingest Lambda (サブスクリプションフィルタ受口→StepFunction実行)
    const ingestFn = new lambda.Function(this, "LogIngestLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../services/ingest_lambda")),
      environment: {
        SLACK_WEBHOOK_SECRET_NAME: "slack/webhook/ai-sre-assistant",
        BEDROCK_MODEL_ID: process.env.BEDROCK_MODEL_ID ?? "anthropic.claude-3-haiku-20240307-v1:0",
      },
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
    });

    // StepFunctions で LLM 実行を 3段階に分けてエラー原因と対策を分析させる
    // Step1: Facts (エラーログから事実だけを抽出)
    const factsFn = new lambda.Function(this, "FactsLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../services/step_facts_lambda")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      environment: { BEDROCK_MODEL_ID: modelId },
      layers: [commonLayer],
    });

    // Step2: Hypotheses (前ステップの情報から仮説生成)
    const hypoFn = new lambda.Function(this, "HypothesesLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../services/step_hypotheses_lambda")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      environment: { BEDROCK_MODEL_ID: modelId },
      layers: [commonLayer],
    });

    // Step3: Finalize + Slack notify (分析サマリと推奨アクションをSlack通知)
    const finalizeFn = new lambda.Function(this, "FinalizeLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../services/step_finalize_lambda")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      environment: {
        BEDROCK_MODEL_ID: modelId,
        SLACK_WEBHOOK_SECRET_NAME: "slack/webhook/ai-sre-assistant",
      },
      layers: [commonLayer],
    });
    slackWebhookSecret.grantRead(finalizeFn);

    // サブスクリプションフィルタ: エラー行だけを流す
    const destination = new logs_destinations.LambdaDestination(ingestFn);
    new logs.SubscriptionFilter(this, "AppErrorSubscription", {
      logGroup: appLogGroup,
      destination,
      filterPattern: logs.FilterPattern.anyTerm(
        "ERROR", "Error", "Exception", "Traceback",
        "Task timed out", "timed out"
      ),
    });

    // Bedrock invoke 権限
    for (const fn of [ingestFn, factsFn, hypoFn, finalizeFn]) {
      fn.addToRolePolicy(new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: ["*"],
      }));
    }

    // =====================================================
    // Step Functions
    // =====================================================
    
    /*
    resultPath を使用して各ステップの実行履歴を管理する (以下イメージ)
    {
      "log": { "...": "..." },
      "facts": { "...": "..." },
      "loop": {
        "retry_count": 1,
        "max_retries": 2
      },
      "investigation": {
        "retry_reason": "top hypothesis confidence is below threshold",
        "extra_guidance": "facts と key_log_lines により強く結びついた仮説を優先し..."
      },
      "hypotheses": {
        "items": [
          {
            "title": "DynamoDB 条件付きチェックの不一致",
            "reasoning": "key_log_lines に ConditionalCheckFailed が明示されており...",
            "confidence": 86
          },
          {
            "title": "アプリケーションの更新前提条件不整合",
            "reasoning": "ConditionExpression が前提とするデータ状態と...",
            "confidence": 73
          }
        ],
        "top_confidence": 86,
        "count": 2
      },
      "analysis": {
        "severity": "P2",
        "summary": "DynamoDB の条件付きチェック失敗により、一部機能で更新処理が正常に完了していない可能性があります。",
        "recommended_actions": [
          {
            "priority": "high",
            "action": "DynamoDB の ConditionExpression とアプリケーション側の更新条件を確認する。"
          }
        ]
      }
    }
    */
    
    const MAX_RETRIES = 2; // stepHypos 最大試行回数

    const stepFacts = new tasks.LambdaInvoke(this, "ExtractFacts", {
      lambdaFunction: factsFn,
      payloadResponseOnly: true, // Lambdaの return 値だけを Step Functions に返す
      inputPath: "$",
      resultPath: "$.facts",
      outputPath: "$",
    });

    // ループ制御に使う状態 (state) の初期化
    const stepInitLoopContext = new sfn.Pass(this, "InitializeLoopContext", {
      result: sfn.Result.fromObject({
        retry_count: 0,           // stepHypos 再試行回数カウンタ
        max_retries: MAX_RETRIES, // 最大試行回数
      }),
      resultPath: "$.loop",
    });

    // 再試行時の追加プロンプトの初期化 (2周目から情報が付加される)
    const stepInitInvestigationContext = new sfn.Pass(this, "InitializeInvestigationContext", {
      result: sfn.Result.fromObject({
        retry_reason: "",   // 再試行理由
        extra_guidance: "", // 再試行時の指示
      }),
      resultPath: "$.investigation",
    });

    const stepHypos = new tasks.LambdaInvoke(this, "GenerateHypotheses", {
      lambdaFunction: hypoFn,
      payloadResponseOnly: true,
      inputPath: "$",
      resultPath: "$.hypotheses",
      outputPath: "$",
    });

    const stepFinalize = new tasks.LambdaInvoke(this, "FinalizeAndNotify", {
      lambdaFunction: finalizeFn,
      payloadResponseOnly: true,
      inputPath: "$",
      resultPath: "$.analysis",
      outputPath: "$",
    });

    // stepHypos 再試行のための state 更新
    const stepPrepareRetryContext = new sfn.Pass(this, "PrepareRetryContext", {
      parameters: {
        // 元の state を維持
        "log.$": "$.log",
        "facts.$": "$.facts",
        "hypotheses.$": "$.hypotheses",

        // retry_count (stepHypos試行回数)を +1
        "loop.retry_count.$": "States.MathAdd($.loop.retry_count, 1)",
        "loop.max_retries.$": "$.loop.max_retries",

        // 再試行時にプロンプトに追加する文脈
        "investigation.retry_reason": "top hypothesis confidence is below threshold",
        "investigation.extra_guidance":
          "facts と key_log_lines により強く結びついた仮説を優先し、一般論を避けてください。前回の仮説と重複しない観点があれば補ってください。",
      },
    });
    
    // Choice (stepHyposを再実行するか / stepFinalizeに進むか)
    const stepCheckHyposConfidence = new sfn.Choice(this, "CheckHypothesisConfidence")
      // 仮説が1件以上あり、top_confidence が閾値以上: stepFinalize に進む
      .when(
        sfn.Condition.and(
          sfn.Condition.numberGreaterThan("$.hypotheses.count", 0),
          sfn.Condition.numberGreaterThanEquals("$.hypotheses.top_confidence", 80)
        ),
        stepFinalize
      )
      // 再試行回数が MAX_RETRIES 未満 かつ、以下条件のどちらかに当てはまれば stepHypos 再実行
      // ・top_confidence が閾値以下
      // ・仮説 (hypotheses.items) が 0件
      .when(
        sfn.Condition.and(
          sfn.Condition.numberLessThan("$.loop.retry_count", MAX_RETRIES),
          sfn.Condition.or(
            sfn.Condition.numberEquals("$.hypotheses.count", 0),
            sfn.Condition.numberLessThan("$.hypotheses.top_confidence", 80)
          )
        ),
        sfn.Chain.start(stepPrepareRetryContext).next(stepHypos)
      )
      // それ以外は stepFinalize に進む (打ち切り)
      .otherwise(stepFinalize);
    
    const definition = stepFacts
      .next(stepInitLoopContext)
      .next(stepInitInvestigationContext)
      .next(stepHypos)
      .next(stepCheckHyposConfidence);

    const stateMachine = new sfn.StateMachine(this, "AiSreAssistantStateMachine", {
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      timeout: cdk.Duration.minutes(5),
    });

    ingestFn.addEnvironment("STATEMACHINE_ARN", stateMachine.stateMachineArn);
    stateMachine.grantStartExecution(ingestFn);

    // Outputs
    new cdk.CfnOutput(this, "ApiUrl", { value: api.url });

  }
}
