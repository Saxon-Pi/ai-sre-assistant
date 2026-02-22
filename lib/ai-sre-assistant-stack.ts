import * as cdk from 'aws-cdk-lib/core';
import { Construct } from 'constructs';
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigw from "aws-cdk-lib/aws-apigateway";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as logs_destinations from "aws-cdk-lib/aws-logs-destinations";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as path from "path";

export class AiSreAssistantStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // DynamoDB (アプリ側・デモ用)
    const table = new dynamodb.Table(this, "DemoTable", {
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // デモ用なので削除OK
    });

    // App Lambda (APIのバックエンド)
    const appFn = new lambda.Function(this, "AppLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../services/app_lambda")),
      environment: {
        TABLE_NAME: table.tableName,
      },
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
    });
    table.grantReadWriteData(appFn);

    // App Lambda ロググループ (サブスクリプションフィルタと確実に連携するため明示)
    const appLogGroup = new logs.LogGroup(this, "AppLambdaLogGroup", {
      logGroupName: `/aws/lambda/${appFn.functionName}`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // API Gateway
    const api = new apigw.RestApi(this, "DemoApi", {
      restApiName: "ai-sre-assistant-demo",
      deployOptions: { stageName: "dev" },
    });
    const root = api.root.addResource("demo");
    root.addMethod("GET", new apigw.LambdaIntegration(appFn));

    // Secret Manager (Slack Incoming Webhook URL用)
    // {"webhook_url":"https://hooks.slack.com/services/XXX/YYY/ZZZ"}
    const slackWebhookSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "SlackWebhookSecret",
      "slack/webhook/ai-sre-assistant"
    );

    // Ingest Lambda (サブスクリプションフィルタ受口)
    const ingestFn = new lambda.Function(this, "LogIngestLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../services/ingest_lambda")),
      environment: {
        SLACK_WEBHOOK_SECRET_NAME: "slack/webhook/ai-sre-assistant",
        BEDROCK_MODEL_ID: process.env.BEDROCK_MODEL_ID ?? "anthropic.claude-3-haiku-20240307-v1:0",
        AWS_REGION: cdk.Stack.of(this).region,
      },
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
    });
    slackWebhookSecret.grantRead(ingestFn);

    // サブスクリプションフィルタ: エラー行だけを流す
    const destination = new logs_destinations.LambdaDestination(ingestFn);
    new logs.SubscriptionFilter(this, "AppErrorSubscription", {
      logGroup: appLogGroup,
      destination,
      filterPattern: logs.FilterPattern.anyTerm("ERROR", "Error", "Exception", "Traceback"),
    });

    // Bedrock 呼び出し権限
    ingestFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: ["*"], // あとでモデルARN に絞る
      })
    );

    // Outputs
    new cdk.CfnOutput(this, "ApiUrl", { value: api.url });

  }
}
