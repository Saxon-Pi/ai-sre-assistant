import * as cdk from 'aws-cdk-lib/core';
import { Construct } from 'constructs';
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigw from "aws-cdk-lib/aws-apigateway";
import * as path from "path";

export class AiSreAssistantStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // DynamoDB (アプリ側・デモ用)
    const table = new dynamodb.Table(this, "DemoTable", {
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // デモなので
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

    // API Gateway
    const api = new apigw.RestApi(this, "DemoApi", {
      restApiName: "ai-sre-assistant-demo",
      deployOptions: { stageName: "dev" },
    });

    const root = api.root.addResource("demo");
    root.addMethod("GET", new apigw.LambdaIntegration(appFn));

    table.grantReadWriteData(appFn);

  }
}
