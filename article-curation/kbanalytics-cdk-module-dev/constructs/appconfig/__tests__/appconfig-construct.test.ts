import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AppConfigConstruct } from '../appconfig-construct';
import { NamingUtil } from '../../../utils/naming';

describe('AppConfigConstruct', () => {
  let app: cdk.App;
  let stack: cdk.Stack;
  let naming: NamingUtil;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
    naming = new NamingUtil({ tenantId: 'test', envCode: 'd', regionCode: 'use1' });
  });

  test('creates AppConfig application, environment, profile, and deployment', () => {
    new AppConfigConstruct(stack, 'TestConfig', {
      appName: 'test-d-use1-kb_config',
      profileName: 'pipeline-config',
      envName: 'dev',
      configContent: { key: 'value' },
      naming,
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::AppConfig::Application', 1);
    template.resourceCountIs('AWS::AppConfig::Environment', 1);
    template.resourceCountIs('AWS::AppConfig::ConfigurationProfile', 1);
    template.resourceCountIs('AWS::AppConfig::HostedConfigurationVersion', 1);
    template.resourceCountIs('AWS::AppConfig::DeploymentStrategy', 1);
    template.resourceCountIs('AWS::AppConfig::Deployment', 1);
  });

  test('creates SSM parameters when ssmPrefix provided', () => {
    new AppConfigConstruct(stack, 'TestConfig', {
      appName: 'test-d-use1-kb_config',
      profileName: 'pipeline-config',
      envName: 'dev',
      configContent: { key: 'value' },
      naming,
      ssmPrefix: '/test-d-use1',
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::SSM::Parameter', 2);
  });

  test('exposes app, env, and profile IDs', () => {
    const config = new AppConfigConstruct(stack, 'TestConfig', {
      appName: 'test-d-use1-kb_config',
      profileName: 'pipeline-config',
      envName: 'dev',
      configContent: { key: 'value' },
    });

    expect(config.appId).toBeDefined();
    expect(config.envId).toBeDefined();
    expect(config.profileId).toBeDefined();
  });
});
