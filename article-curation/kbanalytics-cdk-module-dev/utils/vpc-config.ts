import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';

/**
 * VPC configuration that can be specified at the global level (applies to Lambda functions only)
 * or per-resource (overrides global for Lambdas; standalone for Glue/Redshift).
 *
 * Config YAML example:
 * ```yaml
 * # Global (top-level) — Lambda functions only
 * vpc:
 *   vpc_id: "vpc-0abc123"
 *   subnet_ids: ["subnet-aaa", "subnet-bbb"]
 *   security_group_ids: ["sg-ccc"]
 *
 * # Per-lambda override
 * shared_lambdas:
 *   send_email:
 *     vpc:
 *       vpc_id: "vpc-0xyz789"
 *       subnet_ids: ["subnet-ddd"]
 *       security_group_ids: ["sg-eee"]
 *
 * # Non-Lambda resources use per-resource config directly
 * # Glue: connectionNames, Redshift: vpcId/subnetIds/securityGroupIds
 * ```
 */
export interface VpcConfig {
  /** VPC ID to look up */
  vpcId: string;
  /** Subnet IDs for placement */
  subnetIds: string[];
  /** Security group IDs to attach */
  securityGroupIds: string[];
}

/**
 * Resolved CDK VPC objects ready to pass to constructs.
 */
export interface ResolvedVpc {
  vpc: ec2.IVpc;
  vpcSubnets: ec2.SubnetSelection;
  securityGroups: ec2.ISecurityGroup[];
}

/**
 * Resolves a VpcConfig (from YAML) into CDK VPC objects.
 * Returns undefined if no VPC config is provided.
 */
export function resolveVpcConfig(
  scope: Construct,
  id: string,
  vpcConfig?: VpcConfig
): ResolvedVpc | undefined {
  if (!vpcConfig || !vpcConfig.vpcId) {
    return undefined;
  }

  // Use fromVpcAttributes instead of fromLookup to avoid cross-account
  // context lookups at synth time. We already have explicit subnet/SG IDs.
  const vpc = ec2.Vpc.fromVpcAttributes(scope, `${id}Vpc`, {
    vpcId: vpcConfig.vpcId,
    availabilityZones: ['dummy'], // not used when subnets are explicit
  });

  const vpcSubnets: ec2.SubnetSelection = {
    subnets: vpcConfig.subnetIds.map((subnetId, idx) =>
      ec2.Subnet.fromSubnetId(scope, `${id}Subnet${idx}`, subnetId)
    ),
  };

  const securityGroups = vpcConfig.securityGroupIds.map((sgId, idx) =>
    ec2.SecurityGroup.fromSecurityGroupId(scope, `${id}Sg${idx}`, sgId)
  );

  return { vpc, vpcSubnets, securityGroups };
}

/**
 * Parses a VPC block from YAML config into a VpcConfig.
 * Returns undefined if the block is missing or empty.
 */
export function parseVpcFromConfig(cfg: any): VpcConfig | undefined {
  if (!cfg?.vpc_id) {
    return undefined;
  }
  return {
    vpcId: cfg.vpc_id,
    subnetIds: cfg.subnet_ids || [],
    securityGroupIds: cfg.security_group_ids || [],
  };
}

/**
 * Merges per-lambda VPC config with global VPC config.
 * Per-lambda takes precedence if present.
 * Only used for Lambda functions — other resources use their own VPC config directly.
 */
export function mergeVpcConfig(
  globalVpc: any | undefined,
  resourceVpc: any | undefined
): VpcConfig | undefined {
  const effective = resourceVpc || globalVpc;
  return parseVpcFromConfig(effective);
}
