#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { ArticlePipelineStack } from "../lib/pipeline-stack";

const app = new cdk.App();

new ArticlePipelineStack(app, "ArticlePipelineStack", {
  env: {
    account: app.node.tryGetContext("account") || process.env.CDK_DEFAULT_ACCOUNT,
    region: "eu-west-1",
  },
});

app.synth();
