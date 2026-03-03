#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { AiSreAssistantStack } from '../lib/ai-sre-assistant-stack';

const app = new cdk.App();
new AiSreAssistantStack(app, 'AiSreAssistantStack', {
});
