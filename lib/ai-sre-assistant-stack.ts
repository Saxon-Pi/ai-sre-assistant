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
    });

    // Step2: Hypotheses (前ステップの情報から仮説生成)
    const hypoFn = new lambda.Function(this, "HypothesesLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../services/step_hypotheses_lambda")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      environment: { BEDROCK_MODEL_ID: modelId },
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
    resultPath を使用して各ステップの実行履歴を管理する
    {
      "log": {...},
      "facts": {...},
      "hypotheses": [...],
      "analysis": {...}
    }
    */

    const stepFacts = new tasks.LambdaInvoke(this, "ExtractFacts", {
      lambdaFunction: factsFn,
      inputPath: "$",
      resultPath: "$.facts",
      outputPath: "$",
    });

    const stepHypos = new tasks.LambdaInvoke(this, "GenerateHypotheses", {
      lambdaFunction: hypoFn,
      inputPath: "$",
      resultPath: "$.hypotheses",
      outputPath: "$",
    });

    const stepFinalize = new tasks.LambdaInvoke(this, "FinalizeAndNotify", {
      lambdaFunction: finalizeFn,
      inputPath: "$",
      resultPath: "$.analysis",
      outputPath: "$",
    });

    const stateMachine = new sfn.StateMachine(this, "AiSreAssistantStateMachine", {
      definitionBody: sfn.DefinitionBody.fromChainable(
        stepFacts.next(stepHypos).next(stepFinalize)
      ),
      timeout: cdk.Duration.minutes(5),
    });

    ingestFn.addEnvironment("STATEMACHINE_ARN", stateMachine.stateMachineArn);
    stateMachine.grantStartExecution(ingestFn);

    // Outputs
    new cdk.CfnOutput(this, "ApiUrl", { value: api.url });

  }
}
