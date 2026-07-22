import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Template } from 'aws-cdk-lib/assertions';
import { CustomDynamoDB } from '../dynamodb-construct';
import { NamingUtil } from '../../../utils/naming';

describe('CustomDynamoDB', () => {
  let app: cdk.App;
  let stack: cdk.Stack;
  let naming: NamingUtil;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
    naming = new NamingUtil({
      tenantId: 'test-tenant',
      envCode: 'd',
      regionCode: 'use1',
    });
  });

  test('creates DynamoDB table with partition key', () => {
    new CustomDynamoDB(stack, 'TestTable', {
      naming,
      tableName: 'test-d-use1-dynamodb-table',
      partitionKey: {
        name: 'id',
        type: dynamodb.AttributeType.STRING,
      },
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [
        {
          AttributeName: 'id',
          KeyType: 'HASH',
        },
      ],
      AttributeDefinitions: [
        {
          AttributeName: 'id',
          AttributeType: 'S',
        },
      ],
    });
  });

  test('creates DynamoDB table with partition and sort key', () => {
    new CustomDynamoDB(stack, 'TestTable', {
      naming,
      tableName: 'test-d-use1-dynamodb-table',
      partitionKey: {
        name: 'pk',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'sk',
        type: dynamodb.AttributeType.STRING,
      },
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [
        {
          AttributeName: 'pk',
          KeyType: 'HASH',
        },
        {
          AttributeName: 'sk',
          KeyType: 'RANGE',
        },
      ],
      AttributeDefinitions: [
        {
          AttributeName: 'pk',
          AttributeType: 'S',
        },
        {
          AttributeName: 'sk',
          AttributeType: 'S',
        },
      ],
    });
  });

  test('uses PAY_PER_REQUEST billing mode by default', () => {
    new CustomDynamoDB(stack, 'TestTable', {
      naming,
      tableName: 'test-d-use1-dynamodb-table',
      partitionKey: {
        name: 'id',
        type: dynamodb.AttributeType.STRING,
      },
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::DynamoDB::Table', {
      BillingMode: 'PAY_PER_REQUEST',
    });
  });

  test('applies custom billing mode', () => {
    const dynamoTable = new CustomDynamoDB(stack, 'TestTable', {
      naming,
      tableName: 'test-d-use1-dynamodb-table',
      partitionKey: {
        name: 'id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PROVISIONED,
    });

    // Verify the table is created with provisioned billing
    expect(dynamoTable.table).toBeDefined();

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      ProvisionedThroughput: {
        ReadCapacityUnits: 5,
        WriteCapacityUnits: 5,
      },
    });
  });

  test('retains table by default', () => {
    new CustomDynamoDB(stack, 'TestTable', {
      naming,
      tableName: 'test-d-use1-dynamodb-table',
      partitionKey: {
        name: 'id',
        type: dynamodb.AttributeType.STRING,
      },
    });

    const template = Template.fromStack(stack);

    template.hasResource('AWS::DynamoDB::Table', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
    });
  });

  test('applies custom removal policy', () => {
    new CustomDynamoDB(stack, 'TestTable', {
      naming,
      tableName: 'test-d-use1-dynamodb-table',
      partitionKey: {
        name: 'id',
        type: dynamodb.AttributeType.STRING,
      },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const template = Template.fromStack(stack);

    template.hasResource('AWS::DynamoDB::Table', {
      DeletionPolicy: 'Delete',
      UpdateReplacePolicy: 'Delete',
    });
  });

  test('creates SSM parameters for ARN and name', () => {
    new CustomDynamoDB(stack, 'TestTable', {
      naming,
      tableName: 'test-d-use1-dynamodb-table',
      partitionKey: {
        name: 'id',
        type: dynamodb.AttributeType.STRING,
      },
    });

    const template = Template.fromStack(stack);

    template.resourceCountIs('AWS::SSM::Parameter', 2);
  });

  test('exposes table properties', () => {
    const dynamoTable = new CustomDynamoDB(stack, 'TestTable', {
      naming,
      tableName: 'test-d-use1-dynamodb-table',
      partitionKey: {
        name: 'id',
        type: dynamodb.AttributeType.STRING,
      },
    });

    expect(dynamoTable.table).toBeDefined();
    expect(dynamoTable.tableArn).toBeDefined();
  });

  test('supports NUMBER attribute type', () => {
    new CustomDynamoDB(stack, 'TestTable', {
      naming,
      tableName: 'test-d-use1-dynamodb-table',
      partitionKey: {
        name: 'id',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::DynamoDB::Table', {
      AttributeDefinitions: [
        {
          AttributeName: 'id',
          AttributeType: 'N',
        },
      ],
    });
  });

  test('supports BINARY attribute type', () => {
    new CustomDynamoDB(stack, 'TestTable', {
      naming,
      tableName: 'test-d-use1-dynamodb-table',
      partitionKey: {
        name: 'id',
        type: dynamodb.AttributeType.BINARY,
      },
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::DynamoDB::Table', {
      AttributeDefinitions: [
        {
          AttributeName: 'id',
          AttributeType: 'B',
        },
      ],
    });
  });

  test('supports mixed attribute types for partition and sort keys', () => {
    new CustomDynamoDB(stack, 'TestTable', {
      naming,
      tableName: 'test-d-use1-dynamodb-table',
      partitionKey: {
        name: 'pk',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'timestamp',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::DynamoDB::Table', {
      AttributeDefinitions: [
        {
          AttributeName: 'pk',
          AttributeType: 'S',
        },
        {
          AttributeName: 'timestamp',
          AttributeType: 'N',
        },
      ],
    });
  });
});
