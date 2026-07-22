import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as lambda_event_sources from 'aws-cdk-lib/aws-lambda-event-sources';
import { Construct } from 'constructs';
import * as path from 'path';
import * as fs from 'fs';
import {
  NamingUtil,
  CustomDynamoDB,
  StandardLambda,
  LambdaLayer,
  LambdaRoleConstruct,
  AppConfigConstruct,
  LlmOpsConstruct,
  BedrockGuardrailConstruct,
  EventBridgeRuleConstruct,
  StepFunctionAslConstruct,
  KmsKeyConstruct,
} from '@kbanalytics/cdk-constructs';
import { S3BucketConstruct } from '@kbanalytics/cdk-constructs/constructs/s3/s3-bucket-construct';

export interface ArticleCurationSharedStackProps extends cdk.StackProps {
  project: string;
  envName: string;
  envCode: string;
  regionCode: string;
  sharedPrefix: string;
  baseConfig: any;
  tags: Record<string, string>;
  tenants: Array<{ tenantId: string; config: any }>;
}

export class ArticleCurationSharedStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ArticleCurationSharedStackProps) {
    super(scope, id, props);

    const { project, envName, envCode, regionCode, sharedPrefix, baseConfig, tags: globalTags } = props;
    const region = this.region;
    const accountId = this.account;

    const naming = new NamingUtil({ tenantId: 'shared', envCode, regionCode });
    const ssmPrefix = `/${naming.getPrefix()}`;

    // ── KMS Key (Customer Managed Key for encryption at rest) ─────────────
    // Creates a shared CMK for DynamoDB, SQS, and S3 pipeline bucket encryption.
    // Key alias follows standard naming: shared-<envCode>-<regionCode>-kbcuration
    const automationConfig = baseConfig.automation;
    const automationAccountId = automationConfig.account_id;
    const automationRegion = automationConfig.region;
    const automationTaskRoleName = `shared-${envCode}-${regionCode}-${automationConfig.task_role}`;

    const kmsConstruct = new KmsKeyConstruct(this, 'PipelineEncryptionKey', {
      alias: `alias/${naming.prefixName('kbcuration')}`,
      description: 'Shared CMK for KB Curation pipeline resources (DynamoDB, SQS, S3)',
      enableKeyRotation: true,
      tags: globalTags,
    });
    const encryptionKey = kmsConstruct.key;

    // Grant cross-account automation role access for SQS encrypt/decrypt
    if (automationAccountId) {
      const automationRoleArn = `arn:aws:iam::${automationAccountId}:role/${automationTaskRoleName}`;
      encryptionKey.addToResourcePolicy(new iam.PolicyStatement({
        sid: 'AllowWS3AutomationRoleSQSEncrypt',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ArnPrincipal(automationRoleArn)],
        actions: ['kms:GenerateDataKey', 'kms:Decrypt'],
        resources: ['*'],
      }));
    }

    const encryptionKeyArn = encryptionKey.keyArn;

    // Tags
    Object.entries(globalTags).forEach(([k, v]) => cdk.Tags.of(this).add(k, v));
    cdk.Tags.of(this).add('Tenant', 'Shared');
    cdk.Tags.of(this).add('Region', region);

    const lc = baseConfig.lambda;

    // ── VPC Configuration (optional) ─────────────────────────────────────
    const vpcConfig = baseConfig.vpc || {};
    let lambdaVpcConfig: { vpc: cdk.aws_ec2.IVpc; vpcSubnets: cdk.aws_ec2.SubnetSelection; securityGroups: cdk.aws_ec2.ISecurityGroup[] } | undefined;

    if (vpcConfig.vpc_id && vpcConfig.subnet_ids?.length > 0) {
      const vpc = cdk.aws_ec2.Vpc.fromLookup(this, 'LambdaVpc', { vpcId: vpcConfig.vpc_id });
      const subnets = vpcConfig.subnet_ids.map((id: string, i: number) =>
        cdk.aws_ec2.Subnet.fromSubnetId(this, `LambdaSubnet${i}`, id)
      );
      const sg = new cdk.aws_ec2.SecurityGroup(this, 'LambdaSG', {
        vpc,
        securityGroupName: naming.prefixName('kb_lambda_sg'),
        description: 'Security group for KB Curation Lambda functions',
        allowAllOutbound: true,
      });
      lambdaVpcConfig = {
        vpc,
        vpcSubnets: { subnets },
        securityGroups: [sg],
      };
    }

    // Spread into every StandardLambda constructor
    const vpcProps = lambdaVpcConfig ? {
      vpc: lambdaVpcConfig.vpc,
      vpcSubnets: lambdaVpcConfig.vpcSubnets,
      securityGroups: lambdaVpcConfig.securityGroups,
    } : {};

    // ── DynamoDB Tables ──────────────────────────────────────────────────
    const articleTable = new CustomDynamoDB(this, 'ArticleMetadata', {
      tableName: naming.prefixName(baseConfig.dynamodb.article_metadata.name),
      partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'src_kb_article_id', type: dynamodb.AttributeType.STRING },
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      pointInTimeRecovery: true,
      naming,
      ...(encryptionKey ? { encryptionKey } : {}),
    });

    articleTable.table.addGlobalSecondaryIndex({ indexName: 'pipeline-status-index', partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'pipeline_status', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });
    articleTable.table.addGlobalSecondaryIndex({ indexName: 'duplicate-of-index', partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'duplicate_of', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });
    articleTable.table.addGlobalSecondaryIndex({ indexName: 'source-index', partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'source_file_name', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });
    articleTable.table.addGlobalSecondaryIndex({ indexName: 'ritm-number-index', partitionKey: { name: 'ritm_number', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });

    const jobStatusTable = new CustomDynamoDB(this, 'PipelineJobStatus', {
      tableName: naming.prefixName(baseConfig.dynamodb.pipeline_job_status.name),
      partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'job_id', type: dynamodb.AttributeType.STRING },
      naming,
      ...(encryptionKey ? { encryptionKey } : {}),
    });

    // Enable TTL for automatic cleanup of counter records (30 days)
    const cfnJobStatusTable = jobStatusTable.table.node.defaultChild as dynamodb.CfnTable;
    cfnJobStatusTable.timeToLiveSpecification = {
      attributeName: 'ttl',
      enabled: true,
    };

    // Tenant discovery is done via S3 prefix listing, no registry table needed

    // ── S3 Source Bucket (data team's bucket — imported, read-only) ─────
    // Use exact bucket name from config — no prefix, we don't own this bucket
    const sourceBucket = new S3BucketConstruct(this, 'SourceBucket', {
      bucketName: baseConfig.s3.source_bucket.name,
      versioning: true,
      encryption: true,
      tags: globalTags,
      ssmPrefix: ssmPrefix + '/s3-source',
      importExisting: baseConfig.s3.source_bucket.import_existing || false,
    });

    // ── S3 Problem Finder Source Bucket — just a name reference (not provisioned by us) ──
    // We only pass the bucket name as an env var. Lambda handles missing bucket gracefully.
    const problemFinderBucketName = baseConfig.s3.problem_finder_source_bucket?.name || '';

    // ── S3 Pipeline Bucket (our bucket for raw, generated, manifests, IC/RCA) ──
    const pipelineBucket = new S3BucketConstruct(this, 'PipelineBucket', {
      bucketName: naming.prefixName(baseConfig.s3.pipeline_bucket.name),
      versioning: true,
      encryption: true,
      tags: globalTags,
      ssmPrefix: ssmPrefix + '/s3',
      importExisting: baseConfig.s3.pipeline_bucket.import_existing || false,
      ...(encryptionKey ? { kmsKey: encryptionKey } : {}),
    });

    // ── Shared AppConfig ─────────────────────────────────────────────────
    const sharedAppConfigName = naming.prefixName('kb_shared_config');
    new AppConfigConstruct(this, 'SharedAppConfig', {
      appName: sharedAppConfigName,
      profileName: 'shared-pipeline-config',
      envName,
      configContent: {
        models: baseConfig.models,
        truncation: baseConfig.truncation,
        quality_dimensions: baseConfig.quality_dimensions,
        dedup: baseConfig.dedup,
        pipeline: baseConfig.pipeline,
        change_detection: baseConfig.change_detection,
        field_mapping: baseConfig.field_mapping,
      },
      naming,
      ssmPrefix,
    });

    // ── Lambda Layer ─────────────────────────────────────────────────────
    // AppConfig Lambda extension is published by an AWS-owned, region-specific account.
    // Look up the correct account ID for your region in the AWS AppConfig documentation.
    const appConfigExtension = lambda.LayerVersion.fromLayerVersionArn(
      this, 'AppConfigExt',
      `arn:aws:lambda:${region}:555555555555:layer:AWS-AppConfig-Extension:${baseConfig.appconfig_extension_layer_version}`
    );

    const sharedLayer = new LambdaLayer(this, 'SharedLayer', {
      layerName: naming.prefixName('kb_shared_layer'),
      description: 'Shared Python layer: appconfig_loader, html_utils, logger, token_tracker, validation',
      code: lambda.Code.fromAsset(path.resolve(__dirname, '../../lambda/shared'), {
        exclude: ['__pycache__', '*.pyc', '*.zip'],
      }),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      naming,
    });

    // ── LLMOps ───────────────────────────────────────────────────────────
    const llmops = new LlmOpsConstruct(this, 'LlmOps', {
      resourcePrefix: `${project}-${sharedPrefix}`,
      alarms: baseConfig.llmops?.alarms,
      logRetentionDays: baseConfig.llmops?.log_retention_days,
      s3LogExpirationDays: baseConfig.llmops?.s3_log_expiration_days,
      importExistingBucket: baseConfig.llmops?.import_existing || false,
      importExistingLogGroup: baseConfig.llmops?.import_existing || false,
      skipLoggingConfig: baseConfig.llmops?.import_existing || false,
      ssmPrefix,
    });

    // ── Lambda Role ──────────────────────────────────────────────────────
    const lambdaRoleConstruct = new LambdaRoleConstruct(this, 'LambdaRole', {
      roleName: naming.prefixName('kb_lambda_exec_role'),
      description: 'Execution role for KB Curation pipeline Lambda functions with access to DynamoDB, S3, Bedrock, and S3 Vectors',
      policies: [
        { actions: ['s3vectors:ListVectors', 's3vectors:GetVectors', 's3vectors:QueryVectors', 's3vectors:PutVectors', 's3vectors:DeleteVectors'],
          resources: [`arn:aws:s3vectors:${region}:${accountId}:bucket/*-kb-vectors`, `arn:aws:s3vectors:${region}:${accountId}:bucket/*-kb-vectors/index/*`] },
        { actions: ['bedrock:ApplyGuardrail'], resources: [`arn:aws:bedrock:${region}:${accountId}:guardrail/*`] },
        { actions: ['bedrock:GetPrompt'], resources: [`arn:aws:bedrock:${region}:${accountId}:prompt/*`] },
        { actions: ['comprehend:DetectDominantLanguage'], resources: ['*'] },
        { actions: ['appconfig:GetLatestConfiguration', 'appconfig:StartConfigurationSession'], resources: ['*'] },
        { actions: ['glue:GetTable', 'glue:GetPartitions', 'glue:GetDatabase'], resources: ['*'] },
        { actions: ['athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults', 'athena:StopQueryExecution'], resources: ['*'] },
        // S3 access for Athena: read Glue table data + write query results
        ...(baseConfig.change_detection?.glue?.database ? [
          { actions: ['s3:GetObject', 's3:ListBucket'],
            resources: [
              `arn:aws:s3:::${baseConfig.change_detection.glue.glue_data_bucket}`,
              `arn:aws:s3:::${baseConfig.change_detection.glue.glue_data_bucket}/*`,
            ]
          },
          { actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket'],
            resources: [
              `arn:aws:s3:::shared-${envCode}-${regionCode}-kb-pipeline`,
              `arn:aws:s3:::shared-${envCode}-${regionCode}-kb-pipeline/*`,
            ]
          },
        ] : []),
        { actions: ['secretsmanager:GetSecretValue'], resources: [`arn:aws:secretsmanager:${region}:${accountId}:secret:*-kbcuration-servicenow-api-key-*`] },
        // KMS decrypt/encrypt for reading encrypted S3 objects + cross-account trust key minting
        { actions: ['kms:Decrypt', 'kms:GenerateDataKey', 'kms:Encrypt'], resources: [`arn:aws:kms:${region}:*:key/*`] },
        // KMS permissions for pipeline CMK (DynamoDB, SQS, S3 pipeline bucket)
        ...(encryptionKeyArn ? [{ actions: ['kms:Decrypt', 'kms:GenerateDataKey', 'kms:Encrypt', 'kms:DescribeKey'], resources: [encryptionKeyArn] }] : []),
        // Invoke tenant-connector API Gateway (IAM auth) for ServiceNow config
        ...(baseConfig.servicenow?.tenant_connector_api_arn ? [{ actions: ['execute-api:Invoke'], resources: [baseConfig.servicenow.tenant_connector_api_arn] }] : []),
      ],
      tags: globalTags,
    });
    const lambdaRole = lambdaRoleConstruct.role;

    // Add VPC access policy if VPC is configured
    if (lambdaVpcConfig) {
      lambdaRole.addManagedPolicy(
        cdk.aws_iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaVPCAccessExecutionRole')
      );
    }

    articleTable.table.grantReadWriteData(lambdaRole);
    jobStatusTable.table.grantReadWriteData(lambdaRole);
    pipelineBucket.bucket.grantRead(lambdaRole);
    pipelineBucket.bucket.grantPut(lambdaRole);

    // Source bucket — grant ListBucket + GetObject to Lambda role
    sourceBucket.bucket.grantRead(lambdaRole);
    
    // Problem Finder resources — optional, deploy must not fail if these don't exist yet.
    // Grant broad S3 read + DynamoDB read via IAM policy (no CDK construct import needed).
    if (problemFinderBucketName) {
      lambdaRole.addToPrincipalPolicy(new cdk.aws_iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:ListBucket'],
        resources: [
          `arn:aws:s3:::${problemFinderBucketName}`,
          `arn:aws:s3:::${problemFinderBucketName}/*`,
        ],
      }));
    }
    if (baseConfig.problem_finder?.dynamodb_table) {
      lambdaRole.addToPrincipalPolicy(new cdk.aws_iam.PolicyStatement({
        actions: ['dynamodb:DescribeTable', 'dynamodb:Query', 'dynamodb:GetItem', 'dynamodb:Scan', 'dynamodb:BatchGetItem'],
        resources: [
          `arn:aws:dynamodb:${region}:${accountId}:table/${baseConfig.problem_finder.dynamodb_table}`,
          `arn:aws:dynamodb:${region}:${accountId}:table/${baseConfig.problem_finder.dynamodb_table}/index/*`,
        ],
      }));
    }

    llmops.grantBedrockInvoke(lambdaRole, [
      `arn:aws:bedrock:${region}:${accountId}:inference-profile/*`,
      'arn:aws:bedrock:*::foundation-model/*',
      `arn:aws:bedrock:*::foundation-model/${baseConfig.models.embedding_model_id}`,
    ]);

    // ── Lambda Functions (using StandardLambda construct) ─────────────────
    const lambdaEnv = {
      APPCONFIG_SHARED_APP: sharedAppConfigName,
      APPCONFIG_SHARED_PROFILE: 'shared-pipeline-config',
      APPCONFIG_PROFILE: 'tenant-config',
      APPCONFIG_ENV: envName,
      ENV_CODE: envCode,
      REGION_CODE: regionCode,
    };

    // Change detection logic is now embedded in the File Enumerator Lambda.
    // No separate change_detection Lambda needed.

    const fnArticleEmbed = new StandardLambda(this, 'ArticleEmbedFn', {
      functionName: naming.prefixName('article_embed'),
      description: 'Classifies articles and generates embeddings for vector storage',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.resolve(__dirname, '../../lambda/classify_embed')),
      memorySize: lc.article_embed_memory,
      timeout: cdk.Duration.minutes(lc.article_embed_timeout_minutes),
      role: lambdaRole,
      environment: lambdaEnv,
      logRetentionDays: lc.log_retention_days,
      naming,
      ...vpcProps,
    });
    fnArticleEmbed.function.addLayers(appConfigExtension, sharedLayer.layerVersion);

    const fnDedup = new StandardLambda(this, 'DedupFn', {
      functionName: naming.prefixName('dedup'),
      description: 'Performs deduplication, quality scoring, enrichment, and retirement detection for articles',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.resolve(__dirname, '../../lambda/dedup')),
      memorySize: lc.dedup_memory,
      timeout: cdk.Duration.minutes(lc.dedup_timeout_minutes),
      role: lambdaRole,
      environment: lambdaEnv,
      logRetentionDays: lc.log_retention_days,
      naming,
      ...vpcProps,
    });
    fnDedup.function.addLayers(appConfigExtension, sharedLayer.layerVersion);

    // ── Step Function (using CfnStateMachine for ASL template) ───────────
    // StepFunctionGenericConstruct expects CDK definition, but we use ASL JSON template
    // so we use CfnStateMachine directly with a dedicated role
    const sfnRoleConstruct = new LambdaRoleConstruct(this, 'SfnRole', {
      roleName: naming.prefixName('kb_sfn_exec_role'),
      description: 'Execution role for KB Curation Step Functions state machine',
      policies: [
        { actions: ['lambda:InvokeFunction'], resources: [`arn:aws:lambda:${region}:${accountId}:function:${naming.getPrefix()}-*`] },
        { actions: ['s3:GetObject', 's3:ListBucket', 's3:PutObject'], resources: [pipelineBucket.bucket.bucketArn, `${pipelineBucket.bucket.bucketArn}/*`] },
        { actions: ['states:StartExecution', 'states:DescribeExecution', 'states:StopExecution'], resources: ['*'] },
        { actions: ['logs:CreateLogDelivery', 'logs:GetLogDelivery', 'logs:UpdateLogDelivery', 'logs:DeleteLogDelivery', 'logs:ListLogDeliveries', 'logs:PutResourcePolicy', 'logs:DescribeResourcePolicies', 'logs:DescribeLogGroups'], resources: ['*'] },
        ...(encryptionKeyArn ? [{ actions: ['kms:Decrypt', 'kms:GenerateDataKey', 'kms:Encrypt', 'kms:DescribeKey'], resources: [encryptionKeyArn] }] : []),
      ],
    });
    // Override assume role to states.amazonaws.com
    const sfnCfnRole = sfnRoleConstruct.role.node.defaultChild as cdk.aws_iam.CfnRole;
    sfnCfnRole.addPropertyOverride('AssumeRolePolicyDocument', {
      Version: '2012-10-17',
      Statement: [{ Effect: 'Allow', Principal: { Service: 'states.amazonaws.com' }, Action: 'sts:AssumeRole' }],
    });
    jobStatusTable.table.grantWriteData(sfnRoleConstruct.role);

    // ── SQS FIFO Queue + DLQ ────────────────────────────────────────────
    const sqsConfig = baseConfig.sqs;

    const batchDlq = new sqs.Queue(this, 'BatchDLQ', {
      queueName: naming.prefixName(`${sqsConfig.batch_queue.name}_dlq.fifo`),
      fifo: true,
      encryption: encryptionKey ? sqs.QueueEncryption.KMS : sqs.QueueEncryption.SQS_MANAGED,
      ...(encryptionKey ? { encryptionMasterKey: encryptionKey } : {}),
    });

    // DLQ Depth Alarm - any message in DLQ means batch failed all retries
    const batchDlqAlarm = new cloudwatch.Alarm(this, 'BatchDLQAlarm', {
      alarmName: naming.prefixName('batch_dlq_depth_alarm'),
      alarmDescription: 'Alerts when messages appear in batch DLQ (failed all retries)',
      metric: batchDlq.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(5),
        statistic: 'Maximum',
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    const batchQueue = new sqs.Queue(this, 'BatchFIFOQueue', {
      queueName: naming.prefixName(`${sqsConfig.batch_queue.name}.fifo`),
      fifo: true,
      encryption: encryptionKey ? sqs.QueueEncryption.KMS : sqs.QueueEncryption.SQS_MANAGED,
      ...(encryptionKey ? { encryptionMasterKey: encryptionKey } : {}),
      visibilityTimeout: cdk.Duration.seconds(sqsConfig.batch_queue.visibility_timeout_seconds),
      deadLetterQueue: {
        queue: batchDlq,
        maxReceiveCount: sqsConfig.batch_queue.max_receive_count,
      },
    });

    // ── File Enumerator Lambda ───────────────────────────────────────────
    const fnFileEnumerator = new StandardLambda(this, 'FileEnumeratorFn', {
      functionName: naming.prefixName('file_enumerator'),
      description: 'Enumerates articles from S3/Glue, performs change detection, and queues batches for processing',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.resolve(__dirname, '../../lambda/file_enumerator'), {
        exclude: ['__pycache__', '*.pyc', 'tests'],
      }),
      memorySize: lc.file_enumerator_memory,
      timeout: cdk.Duration.minutes(lc.file_enumerator_timeout_minutes),
      role: lambdaRole,
      environment: {
        ...lambdaEnv,
        SOURCE_BUCKET: sourceBucket.bucket.bucketName,
        SOURCE_PREFIX: baseConfig.s3.source_bucket.prefix || '',
        PROBLEM_FINDER_SOURCE_BUCKET: problemFinderBucketName,
        PROBLEM_FINDER_TABLE: baseConfig.problem_finder?.dynamodb_table || '',
        PROBLEM_FINDER_GSI: baseConfig.problem_finder?.gsi_name || '',
        PIPELINE_BUCKET: pipelineBucket.bucket.bucketName,
        QUEUE_URL: batchQueue.queueUrl,
        BATCH_SIZE: String(baseConfig.pipeline.batch_size),
        JOB_STATUS_TABLE: jobStatusTable.table.tableName,
        ARTICLE_TABLE: articleTable.table.tableName,
        GLUE_DATABASE: baseConfig.change_detection?.glue?.database || '',
        GLUE_TABLE: baseConfig.change_detection?.glue?.table || '',
        ATHENA_OUTPUT_LOCATION: baseConfig.change_detection?.glue?.athena_output_location || '',
      },
      logRetentionDays: lc.log_retention_days,
      naming,
      ...vpcProps,
    });
    fnFileEnumerator.function.addLayers(appConfigExtension, sharedLayer.layerVersion);

    // ── Write Manifest Lambda ────────────────────────────────────────────
    const fnWriteManifest = new StandardLambda(this, 'WriteManifestFn', {
      functionName: naming.prefixName('write_manifest'),
      description: 'Writes job manifest to S3 with article batch metadata',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'write_manifest.lambda_handler',
      code: lambda.Code.fromAsset(path.resolve(__dirname, '../../lambda/file_enumerator'), {
        exclude: ['__pycache__', '*.pyc', 'tests'],
      }),
      memorySize: lc.write_manifest_memory,
      timeout: cdk.Duration.seconds(lc.write_manifest_timeout_seconds),
      role: lambdaRole,
      environment: {
        ...lambdaEnv,
        PIPELINE_BUCKET: pipelineBucket.bucket.bucketName,
      },
      logRetentionDays: lc.log_retention_days,
      naming,
      ...vpcProps,
    });
    fnWriteManifest.function.addLayers(appConfigExtension, sharedLayer.layerVersion);

    // ── ASL Template Processing ──────────────────────────────────────────
    const aslTemplate = fs.readFileSync(path.resolve(__dirname, '../../step-function/article-curation-pipeline.json'), 'utf-8');
    const aslBody = aslTemplate
      .replace(/<<WRITE_MANIFEST_FN>>/g, fnWriteManifest.function.functionName)
      .replace(/<<ARTICLE_EMBED_FN>>/g, fnArticleEmbed.function.functionName)
      .replace(/<<DEDUP_FN>>/g, fnDedup.function.functionName)
      .replace(/<<JOB_STATUS_TABLE>>/g, jobStatusTable.table.tableName)
      .replace(/<<SFN_MAX_ITEMS_PER_BATCH>>/g, String(baseConfig.step_function.max_items_per_batch))
      .replace(/<<SFN_MAX_CONCURRENCY>>/g, String(baseConfig.step_function.max_concurrency))
      .replace(/<<SFN_TOLERATED_FAILURE_PCT>>/g, String(baseConfig.step_function.tolerated_failure_pct));

    const aslObj = JSON.parse(aslBody);

    const sfnConstruct = new StepFunctionAslConstruct(this, 'CurationPipeline', {
      stateMachineName: naming.prefixName('article_curation_pipeline'),
      role: sfnRoleConstruct.role,
      definitionString: JSON.stringify(aslObj, null, 2),
      logRetentionDays: baseConfig.step_function?.log_retention_days || 30,
      naming,
      ssmPrefix,
    });

    const sfnArn = sfnConstruct.stateMachineArn;

    // ── EventBridge Schedule ─────────────────────────────────────────────
    new EventBridgeRuleConstruct(this, 'PipelineSchedule', {
      ruleName: naming.prefixName('kb_pipeline_schedule'),
      description: 'Runs KB curation pipeline on schedule',
      schedule: cdk.aws_events.Schedule.cron({
        ...(baseConfig.schedule.cron_minute ? { minute: baseConfig.schedule.cron_minute } : {}),
        ...(baseConfig.schedule.cron_hour ? { hour: baseConfig.schedule.cron_hour } : {}),
        ...(baseConfig.schedule.cron_day ? { day: baseConfig.schedule.cron_day } : {}),
        ...(baseConfig.schedule.cron_month ? { month: baseConfig.schedule.cron_month } : {}),
        ...(baseConfig.schedule.cron_weekday ? { weekDay: baseConfig.schedule.cron_weekday } : {}),
        ...(baseConfig.schedule.cron_year ? { year: baseConfig.schedule.cron_year } : {}),
      }),
      targets: [{ type: 'lambda', lambdaFunction: fnFileEnumerator.function }],
      tags: globalTags,
    });

    // ── Dispatcher Lambda ────────────────────────────────────────────────
    const dispatcherRole = new LambdaRoleConstruct(this, 'DispatcherRole', {
      roleName: naming.prefixName('kb_dispatcher_exec_role'),
      description: 'Execution role for dispatcher Lambda - triggers Step Functions and handles batch failures',
      policies: [
        { actions: ['states:StartExecution'],
          resources: [sfnArn] },
        { actions: ['states:DescribeExecution'],
          resources: [`arn:aws:states:${region}:${accountId}:execution:${sfnConstruct.stateMachineName}:*`] },
        ...(encryptionKeyArn ? [{ actions: ['kms:Decrypt', 'kms:GenerateDataKey', 'kms:Encrypt', 'kms:DescribeKey'], resources: [encryptionKeyArn] }] : []),
      ],
      tags: globalTags,
    });

    // Add VPC access policy to dispatcher role if VPC is configured
    if (lambdaVpcConfig) {
      dispatcherRole.role.addManagedPolicy(
        cdk.aws_iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaVPCAccessExecutionRole')
      );
    }

    const fnDispatcher = new StandardLambda(this, 'DispatcherFn', {
      functionName: naming.prefixName('dispatcher'),
      description: 'Consumes SQS batches and triggers Step Functions execution for article processing',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.resolve(__dirname, '../../lambda/dispatcher')),
      memorySize: lc.dispatcher_lambda_memory,
      timeout: cdk.Duration.minutes(lc.dispatcher_lambda_timeout_minutes),
      role: dispatcherRole.role,
      environment: {
        STATE_MACHINE_ARN: sfnArn,
        DLQ_URL: batchDlq.queueUrl,
        ARTICLE_TABLE: articleTable.table.tableName,
        MAX_CONSECUTIVE_FAILURES: String(baseConfig.pipeline.max_consecutive_batch_failures || 3),
      },
      logRetentionDays: lc.log_retention_days,
      naming,
      ...vpcProps,
    });
    fnDispatcher.function.addLayers(sharedLayer.layerVersion);

    // ── SQS Event Source Mapping (Dispatcher ← FIFO Queue) ──────────────
    fnDispatcher.function.addEventSource(new lambda_event_sources.SqsEventSource(batchQueue, {
      batchSize: 1,
      maxConcurrency: sqsConfig.batch_queue.max_concurrency,
    }));

    // ── IAM: Dispatcher — DynamoDB write (reset articles) + DLQ send ────
    articleTable.table.grantReadWriteData(dispatcherRole.role);
    batchDlq.grantSendMessages(dispatcherRole.role);

    // ── IAM: File Enumerator — SQS SendMessage ──────────────────────────
    batchQueue.grantSendMessages(lambdaRole);

    // ── IAM: File Enumerator — DynamoDB + S3 + AppConfig ─────────────────
    // (Already granted above via lambdaRole: articleTable read/write, pipelineBucket read/put, source bucket read, appconfig)

    // ── SN Task Creator ──────────────────────────────────────────────────
    const fnSnTaskCreator = new StandardLambda(this, 'SnTaskCreatorFn', {
      functionName: naming.prefixName('sn_task_creator'),
      description: 'Creates ServiceNow review tasks for generated articles - triggered by DynamoDB stream',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.resolve(__dirname, '../../lambda/sn_task_creator')),
      role: lambdaRoleConstruct.role,
      memorySize: lc.sn_task_creator_memory,
      timeout: cdk.Duration.seconds(lc.sn_task_creator_timeout_seconds),
      environment: {
        ...lambdaEnv,
        S3_BUCKET: pipelineBucket.bucket.bucketName,
        ARTICLE_TABLE: articleTable.table.tableName,
        JOB_STATUS_TABLE: jobStatusTable.table.tableName,
        TENANT_CONNECTOR_API_URL: baseConfig.servicenow?.tenant_connector_api_url || '',
        TENANT_CONNECTOR_API_HOST: baseConfig.servicenow?.tenant_connector_api_host || '',
      },
      logRetentionDays: lc.log_retention_days,
      naming,
      ...vpcProps,
    });
    fnSnTaskCreator.function.addLayers(appConfigExtension, sharedLayer.layerVersion);
    articleTable.table.grantReadWriteData(fnSnTaskCreator.function);
    jobStatusTable.table.grantReadWriteData(fnSnTaskCreator.function);
    pipelineBucket.bucket.grantRead(fnSnTaskCreator.function);
    fnSnTaskCreator.function.addEventSource(new lambda_event_sources.DynamoEventSource(articleTable.table, {
      startingPosition: lambda.StartingPosition.LATEST, batchSize: lc.sn_task_creator_stream_batch_size, retryAttempts: 2,
      filters: [lambda.FilterCriteria.filter({
        eventName: lambda.FilterRule.isEqual('MODIFY'),
        dynamodb: { NewImage: { pipeline_status: { S: lambda.FilterRule.isEqual('GENERATED') } } },
      })],
    }));

    // ── Approval SQS Queue (standard, consumed by webhook handler) ─────
    const approvalDlq = new sqs.Queue(this, 'ApprovalDLQ', {
      queueName: naming.prefixName(`${sqsConfig.approval_queue.name}_dlq`),
      encryption: encryptionKey ? sqs.QueueEncryption.KMS : sqs.QueueEncryption.SQS_MANAGED,
      ...(encryptionKey ? { encryptionMasterKey: encryptionKey } : {}),
    });

    // DLQ Depth Alarm - any message in approval DLQ means webhook processing failed all retries
    const approvalDlqAlarm = new cloudwatch.Alarm(this, 'ApprovalDLQAlarm', {
      alarmName: naming.prefixName('approval_dlq_depth_alarm'),
      alarmDescription: 'Alerts when messages appear in approval DLQ (webhook handler failed all retries)',
      metric: approvalDlq.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(5),
        statistic: 'Maximum',
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    const approvalQueue = new sqs.Queue(this, 'ApprovalQueue', {
      queueName: naming.prefixName(sqsConfig.approval_queue.name),
      encryption: encryptionKey ? sqs.QueueEncryption.KMS : sqs.QueueEncryption.SQS_MANAGED,
      ...(encryptionKey ? { encryptionMasterKey: encryptionKey } : {}),
      visibilityTimeout: cdk.Duration.seconds(sqsConfig.approval_queue.visibility_timeout_seconds),
      deadLetterQueue: {
        queue: approvalDlq,
        maxReceiveCount: sqsConfig.approval_queue.max_receive_count,
      },
    });

    // ── Webhook Handler Lambda (SQS-based approval/rejection) ────────────
    const fnWebhookHandler = new StandardLambda(this, 'WebhookHandlerFn', {
      functionName: naming.prefixName('webhook_handler'),
      description: 'Processes article approval/rejection messages from external teams and deletes vectors for rejected articles',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.resolve(__dirname, '../../lambda/webhook_handler')),
      memorySize: lc.webhook_handler_memory,
      timeout: cdk.Duration.seconds(lc.webhook_handler_timeout_seconds),
      environment: { 
        ARTICLE_TABLE: articleTable.table.tableName,
        ENV_CODE: envCode,
        REGION_CODE: regionCode,
      },
      logRetentionDays: lc.log_retention_days,
      naming,
      ...vpcProps,
    });
    articleTable.table.grantReadWriteData(fnWebhookHandler.function);
    
    // Grant S3 Vectors permissions for vector deletion (all tenant vector buckets)
    fnWebhookHandler.function.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['s3vectors:DeleteVectors'],
      resources: [`arn:aws:s3vectors:${region}:${accountId}:bucket/*-kb-vectors`, `arn:aws:s3vectors:${region}:${accountId}:bucket/*-kb-vectors/index/*`],
    }));

    // Wire SQS → Lambda event source
    fnWebhookHandler.function.addEventSource(new lambda_event_sources.SqsEventSource(approvalQueue, {
      batchSize: sqsConfig.approval_queue.batch_size,
    }));

    // Grant cross-account SendMessage to the approval queue (for automation account)
    if (automationAccountId) {
      const automationSenderRoleArn = `arn:aws:iam::${automationAccountId}:role/${automationTaskRoleName}`;
      approvalQueue.addToResourcePolicy(new cdk.aws_iam.PolicyStatement({
        effect: cdk.aws_iam.Effect.ALLOW,
        principals: [new cdk.aws_iam.ArnPrincipal(automationSenderRoleArn)],
        actions: ['sqs:SendMessage'],
        resources: [approvalQueue.queueArn],
      }));
    }

    // ── Shared Guardrail (default for all tenants) ───────────────────────
    new BedrockGuardrailConstruct(this, 'SharedGuardrail', {
      guardrailName: naming.prefixName('article_curation_guardrail'),
      description: 'Content safety and contextual grounding for KB Curation Pipeline',
      contentFilters: baseConfig.guardrail.content_filters.map((f: any) => ({
        type: f.type, inputStrength: f.input_strength || f.inputStrength, outputStrength: f.output_strength || f.outputStrength,
      })),
      groundingThreshold: baseConfig.guardrail.grounding_threshold,
      naming,
      ssmPrefix,
    });
  }
}
