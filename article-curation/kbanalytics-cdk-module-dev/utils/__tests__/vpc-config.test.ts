import * as cdk from 'aws-cdk-lib';
import { resolveVpcConfig, parseVpcFromConfig, mergeVpcConfig } from '../vpc-config';

describe('parseVpcFromConfig', () => {
  it('returns undefined when cfg is undefined', () => {
    expect(parseVpcFromConfig(undefined)).toBeUndefined();
  });

  it('returns undefined when cfg has no vpc_id', () => {
    expect(parseVpcFromConfig({ subnet_ids: ['subnet-aaa'] })).toBeUndefined();
  });

  it('parses a valid vpc block', () => {
    const result = parseVpcFromConfig({
      vpc_id: 'vpc-123',
      subnet_ids: ['subnet-aaa', 'subnet-bbb'],
      security_group_ids: ['sg-ccc'],
    });
    expect(result).toEqual({
      vpcId: 'vpc-123',
      subnetIds: ['subnet-aaa', 'subnet-bbb'],
      securityGroupIds: ['sg-ccc'],
    });
  });

  it('defaults subnet_ids and security_group_ids to empty arrays', () => {
    const result = parseVpcFromConfig({ vpc_id: 'vpc-123' });
    expect(result).toEqual({
      vpcId: 'vpc-123',
      subnetIds: [],
      securityGroupIds: [],
    });
  });
});

describe('mergeVpcConfig', () => {
  const globalVpc = {
    vpc_id: 'vpc-global',
    subnet_ids: ['subnet-g1'],
    security_group_ids: ['sg-g1'],
  };

  const resourceVpc = {
    vpc_id: 'vpc-resource',
    subnet_ids: ['subnet-r1'],
    security_group_ids: ['sg-r1'],
  };

  it('returns undefined when both are undefined', () => {
    expect(mergeVpcConfig(undefined, undefined)).toBeUndefined();
  });

  it('returns global when resource is undefined', () => {
    const result = mergeVpcConfig(globalVpc, undefined);
    expect(result?.vpcId).toBe('vpc-global');
  });

  it('returns resource when global is undefined', () => {
    const result = mergeVpcConfig(undefined, resourceVpc);
    expect(result?.vpcId).toBe('vpc-resource');
  });

  it('resource takes precedence over global', () => {
    const result = mergeVpcConfig(globalVpc, resourceVpc);
    expect(result?.vpcId).toBe('vpc-resource');
    expect(result?.subnetIds).toEqual(['subnet-r1']);
  });
});

describe('resolveVpcConfig', () => {
  it('returns undefined when vpcConfig is undefined', () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');
    expect(resolveVpcConfig(stack, 'Test', undefined)).toBeUndefined();
  });

  it('returns undefined when vpcConfig has no vpcId', () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestStack');
    expect(
      resolveVpcConfig(stack, 'Test', { vpcId: '', subnetIds: [], securityGroupIds: [] })
    ).toBeUndefined();
  });
});
