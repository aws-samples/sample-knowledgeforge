import * as cdk from 'aws-cdk-lib';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';

export interface KmsKeyConfig {
  alias: string;
  description: string;
  enableKeyRotation: boolean;
  tags: Record<string, string>;
}

export class KmsKeyConstruct extends Construct {
  public readonly key: kms.IKey;

  constructor(scope: Construct, id: string, config: KmsKeyConfig) {
    super(scope, id);

    const key = new kms.Key(this, 'Key', {
      description: config.description,
      enableKeyRotation: config.enableKeyRotation,
      alias: config.alias,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Apply tags
    Object.entries(config.tags).forEach(([k, v]) => {
      cdk.Tags.of(this).add(k, v);
    });

    this.key = key;
  }
}
