import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { KmsKeyConstruct } from './kms-key';

function makeStack() {
  const app = new cdk.App();
  return new cdk.Stack(app, 'TestStack');
}

const BASE_CONFIG = {
  alias: 'bmw-d-use1-data_encryption',
  description: 'KMS key for tenant bmw',
  enableKeyRotation: true,
  tags: {},
};

describe('KmsKeyConstruct', () => {
  it('exposes key as a public readonly IKey property', () => {
    const stack = makeStack();
    const construct = new KmsKeyConstruct(stack, 'TestKey', BASE_CONFIG);

    expect(construct.key).toBeDefined();
    expect(construct.key).not.toBeNull();
  });

  it('enables key rotation when enableKeyRotation: true', () => {
    const stack = makeStack();
    new KmsKeyConstruct(stack, 'TestKey', BASE_CONFIG);

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: true,
    });
  });

  it('does not enable key rotation when enableKeyRotation: false', () => {
    const stack = makeStack();
    new KmsKeyConstruct(stack, 'TestKey', { ...BASE_CONFIG, enableKeyRotation: false });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: false,
    });
  });

  it('applies the alias from config', () => {
    const stack = makeStack();
    new KmsKeyConstruct(stack, 'TestKey', BASE_CONFIG);

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::KMS::Alias', {
      AliasName: 'alias/bmw-d-use1-data_encryption',
    });
  });

  it('applies the description from config', () => {
    const stack = makeStack();
    new KmsKeyConstruct(stack, 'TestKey', BASE_CONFIG);

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::KMS::Key', {
      Description: 'KMS key for tenant bmw',
    });
  });

  it('applies tags from config', () => {
    const stack = makeStack();
    new KmsKeyConstruct(stack, 'TestKey', {
      ...BASE_CONFIG,
      tags: { Tenant: 'bmw', Environment: 'd' },
    });

    const template = Template.fromStack(stack);
    const keys = template.findResources('AWS::KMS::Key');
    const keyProps = Object.values(keys)[0].Properties;
    const tags: { Key: string; Value: string }[] = keyProps.Tags ?? [];

    expect(tags.some((t) => t.Key === 'Tenant' && t.Value === 'bmw')).toBe(true);
    expect(tags.some((t) => t.Key === 'Environment' && t.Value === 'd')).toBe(true);
  });

  it('does not create any SSM Parameter Store resources', () => {
    const stack = makeStack();
    new KmsKeyConstruct(stack, 'TestKey', BASE_CONFIG);

    const template = Template.fromStack(stack);
    const ssmParams = template.findResources('AWS::SSM::Parameter');
    expect(Object.keys(ssmParams)).toHaveLength(0);
  });
});
