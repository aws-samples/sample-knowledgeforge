import {
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  Tags,
  aws_applicationautoscaling as appscaling,
  aws_bedrock as bedrock,
  aws_cloudwatch as cloudwatch,
  aws_dynamodb as dynamodb,
  aws_ec2 as ec2,
  aws_ecr as ecr,
  aws_ecs as ecs,
  aws_iam as iam,
  aws_logs as logs,
  aws_s3 as s3,
  aws_s3_notifications as s3n,
  aws_sqs as sqs,
} from "aws-cdk-lib";
import { Construct } from "constructs";

export class ArticlePipelineStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    // ── Source Bucket — pre-existing, imported by name ───────────────
    const sourceBucket = s3.Bucket.fromBucketName(
      this,
      "SourceBucket",
      "incident-themes-bucket"
    );

    // ── Resource tags ───────────────────────────────────────────────
    Tags.of(this).add("project", "article-pipeline");
    Tags.of(this).add("environment", "production");
    Tags.of(this).add("owner", "devops-team");

    // ── VPC with private subnets and NAT gateway ────────────────────
    const vpc = new ec2.Vpc(this, "PipelineVpc", {
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        {
          name: "Private",
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
        {
          name: "Public",
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
      ],
    });

    // ── SQS Processing Queue + Dead Letter Queue ────────────────────
    const dlq = new sqs.Queue(this, "DeadLetterQueue", {
      queueName: "article-pipeline-dlq",
      retentionPeriod: Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });

    const processingQueue = new sqs.Queue(this, "ProcessingQueue", {
      queueName: "article-pipeline-queue",
      visibilityTimeout: Duration.minutes(30),
      retentionPeriod: Duration.days(4),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      deadLetterQueue: {
        maxReceiveCount: 3,
        queue: dlq,
      },
    });

    // ── CloudWatch alarm on DLQ message count ───────────────────────
    new cloudwatch.Alarm(this, "DlqAlarm", {
      alarmName: "article-pipeline-dlq-messages",
      metric: dlq.metricApproximateNumberOfMessagesVisible(),
      threshold: 0,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ── Output S3 bucket ────────────────────────────────────────────
    const outputBucket = new s3.Bucket(this, "OutputBucket", {
      bucketName: "article-generation-output-bucket",
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // ── DynamoDB Metrics Table ──────────────────────────────────────
    const metricsTable = new dynamodb.Table(this, "MetricsTable", {
      tableName: "article-pipeline-metrics",
      partitionKey: { name: "run_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "record_type", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: RemovalPolicy.RETAIN,
      timeToLiveAttribute: "ttl",
    });

    // ── ECR repository ──────────────────────────────────────────────
    const ecrRepo = new ecr.Repository(this, "EcrRepository", {
      repositoryName: "article-pipeline",
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    ecrRepo.addLifecycleRule({
      maxImageCount: 10,
      rulePriority: 1,
      description: "Retain last 10 images",
    });

    // ── Bedrock Guardrail ───────────────────────────────────────────
    const guardrail = new bedrock.CfnGuardrail(this, "ArticleGuardrail", {
      name: "article-generator-guardrail",
      description:
        "Content guardrail for multi-tenant KB and RCA article generation",
      blockedInputMessaging: "Input blocked by content guardrail.",
      blockedOutputsMessaging: "Output blocked by content guardrail.",
      contentPolicyConfig: {
        filtersConfig: [
          { type: "HATE", inputStrength: "NONE", outputStrength: "HIGH" },
          { type: "INSULTS", inputStrength: "NONE", outputStrength: "HIGH" },
          { type: "SEXUAL", inputStrength: "NONE", outputStrength: "HIGH" },
          { type: "VIOLENCE", inputStrength: "NONE", outputStrength: "HIGH" },
          { type: "MISCONDUCT", inputStrength: "NONE", outputStrength: "HIGH" },
        ],
      },
      contextualGroundingPolicyConfig: {
        filtersConfig: [{ type: "GROUNDING", threshold: 0.75 }],
      },
    });

    // ── CloudWatch log group ────────────────────────────────────────
    const logGroup = new logs.LogGroup(this, "PipelineLogGroup", {
      logGroupName: "article-pipeline-logs",
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ── ECS Fargate cluster and task definition ─────────────────────
    const cluster = new ecs.Cluster(this, "PipelineCluster", {
      clusterName: "article-pipeline-cluster",
      vpc,
    });

    const taskDef = new ecs.FargateTaskDefinition(this, "PipelineTaskDef", {
      cpu: 1024,
      memoryLimitMiB: 2048,
    });

    taskDef.addContainer("PipelineContainer", {
      image: ecs.ContainerImage.fromEcrRepository(ecrRepo, "v4"),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "pipeline",
        logGroup,
      }),
      environment: {
        SQS_QUEUE_URL: processingQueue.queueUrl,
        DYNAMODB_METRICS_TABLE: metricsTable.tableName,
        S3_OUTPUT_BUCKET: outputBucket.bucketName,
        S3_INPUT_BUCKET: sourceBucket.bucketName,
        GUARDRAIL_GUARDRAIL_ID: guardrail.attrGuardrailId,
        GUARDRAIL_GUARDRAIL_VERSION: guardrail.attrVersion,
      },
    });

    const service = new ecs.FargateService(this, "PipelineService", {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    // ── Auto Scaling — scale based on SQS queue depth ───────────────
    const scaling = service.autoScaleTaskCount({
      minCapacity: 1,
      maxCapacity: 5,
    });

    scaling.scaleOnMetric("ScaleOnQueueDepth", {
      metric: processingQueue.metricApproximateNumberOfMessagesVisible({
        period: Duration.minutes(1),
        statistic: "Average",
      }),
      scalingSteps: [
        { upper: 0, change: -4 },
        { lower: 1, upper: 5, change: 1 },
        { lower: 5, upper: 20, change: 2 },
        { lower: 20, change: 4 },
      ],
      adjustmentType: appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
      cooldown: Duration.minutes(3),
    });

    // ── IAM: task execution role ────────────────────────────────────
    ecrRepo.grantPull(taskDef.executionRole!);
    logGroup.grantWrite(taskDef.executionRole!);

    // ── IAM: task role with least-privilege ──────────────────────────
    const taskRole = taskDef.taskRole;

    sourceBucket.grantRead(taskRole);
    outputBucket.grantReadWrite(taskRole);

    processingQueue.grantSendMessages(taskRole);
    processingQueue.grantConsumeMessages(taskRole);
    dlq.grantSendMessages(taskRole);
    dlq.grantConsumeMessages(taskRole);

    metricsTable.grantReadWriteData(taskRole);

    taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: [
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
          "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0",
          `arn:aws:bedrock:*:${this.account}:inference-profile/eu.anthropic.claude-sonnet-4-5-20250929-v1:0`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/amazon.titan-embed-text-v2:0`,
        ],
      })
    );

    taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["bedrock:ApplyGuardrail"],
        resources: [
          `arn:aws:bedrock:${this.region}:${this.account}:guardrail/${guardrail.attrGuardrailId}`,
        ],
      })
    );

    taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["s3vectors:QueryVectors", "s3vectors:GetVectors"],
        resources: ["*"],
      })
    );

    // ── S3 event notification → SQS ─────────────────────────────────
    processingQueue.addToResourcePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal("s3.amazonaws.com")],
        actions: ["sqs:SendMessage"],
        resources: [processingQueue.queueArn],
        conditions: {
          ArnLike: { "aws:SourceArn": sourceBucket.bucketArn },
        },
      })
    );

    sourceBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.SqsDestination(processingQueue),
      { suffix: "themes.json" }
    );

    // ── CloudFormation outputs ──────────────────────────────────────
    new CfnOutput(this, "VpcId", { value: vpc.vpcId });
    new CfnOutput(this, "ProcessingQueueUrl", { value: processingQueue.queueUrl });
    new CfnOutput(this, "DlqUrl", { value: dlq.queueUrl });
    new CfnOutput(this, "MetricsTableName", { value: metricsTable.tableName });
    new CfnOutput(this, "OutputBucketName", { value: outputBucket.bucketName });
    new CfnOutput(this, "SourceBucketName", { value: sourceBucket.bucketName });
    new CfnOutput(this, "EcrRepoUri", { value: ecrRepo.repositoryUri });
    new CfnOutput(this, "EcsClusterArn", { value: cluster.clusterArn });
    new CfnOutput(this, "TaskDefinitionArn", { value: taskDef.taskDefinitionArn });
    new CfnOutput(this, "GuardrailId", { value: guardrail.attrGuardrailId });
  }
}
