import * as appconfig from 'aws-cdk-lib/aws-appconfig';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { NamingUtil } from '../../utils/naming';

/**
 * AppConfig construct for hosted configuration profiles.
 *
 * Creates an AppConfig application, environment, profile, hosted config version,
 * and deploys it using AllAtOnce strategy.
 *
 * Pattern: <tenant_id>-<env_code>-<region_code>-<functionality>
 * Example: example-s-euw1-content_pipeline_config
 */
export interface AppConfigConstructProps {
  /** AppConfig application name */
  appName: string;
  /** Profile name (e.g. 'shared-pipeline-config', 'tenant-config') */
  profileName: string;
  /** Environment name (e.g. 'sandbox', 'dev', 'prod') */
  envName: string;
  /** Configuration content as a JSON-serializable object */
  configContent: Record<string, any>;
  /** Optional NamingUtil for validation and SSM paths */
  naming?: NamingUtil;
  /** Optional SSM prefix for storing app ID and profile ID */
  ssmPrefix?: string;
}

export class AppConfigConstruct extends Construct {
  public readonly appId: string;
  public readonly envId: string;
  public readonly profileId: string;

  constructor(scope: Construct, id: string, props: AppConfigConstructProps) {
    super(scope, id);

    if (props.naming) {
      const validation = props.naming.validateResourceName(props.appName);
      if (!validation.isValid) {
        console.warn(`AppConfig naming validation warnings for "${props.appName}":`);
        validation.errors.forEach((e) => console.warn(`  - ${e}`));
      }
    }

    const app = new appconfig.CfnApplication(this, 'App', {
      name: props.appName,
    });
    this.appId = app.ref;

    const env = new appconfig.CfnEnvironment(this, 'Env', {
      applicationId: app.ref,
      name: props.envName,
    });
    this.envId = env.ref;

    const profile = new appconfig.CfnConfigurationProfile(this, 'Profile', {
      applicationId: app.ref,
      name: props.profileName,
      locationUri: 'hosted',
    });
    this.profileId = profile.ref;

    const version = new appconfig.CfnHostedConfigurationVersion(this, 'Version', {
      applicationId: app.ref,
      configurationProfileId: profile.ref,
      contentType: 'application/json',
      content: JSON.stringify(props.configContent, null, 2),
    });

    const strategy = new appconfig.CfnDeploymentStrategy(this, 'Strategy', {
      name: `${props.appName}-instant_deploy`,
      deploymentDurationInMinutes: 0,
      growthFactor: 100,
      replicateTo: 'NONE',
      finalBakeTimeInMinutes: 0,
    });

    new appconfig.CfnDeployment(this, 'Deployment', {
      applicationId: app.ref,
      environmentId: env.ref,
      configurationProfileId: profile.ref,
      configurationVersion: version.ref,
      deploymentStrategyId: strategy.ref,
    });

    // SSM parameters
    if (props.ssmPrefix) {
      new ssm.StringParameter(this, 'AppIdParam', {
        parameterName: `${props.ssmPrefix}/appconfig/${id}/app-id`,
        stringValue: app.ref,
        description: `AppConfig app ID for ${props.appName}`,
      });
      new ssm.StringParameter(this, 'AppNameParam', {
        parameterName: `${props.ssmPrefix}/appconfig/${id}/app-name`,
        stringValue: props.appName,
        description: `AppConfig app name for ${props.appName}`,
      });
    }
  }
}
