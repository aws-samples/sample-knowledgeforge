import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { NamingUtil } from '../../utils/naming';

/**
 * API Gateway webhook construct with OAuth2 (Cognito) authentication.
 *
 * Creates a REST API with a single POST endpoint backed by a Lambda function,
 * secured with a Cognito User Pool authorizer for OAuth2 token validation.
 *
 * TicketSystem sends a Bearer token in the Authorization header.
 * Cognito validates the token before the request reaches the Lambda.
 */
export interface ApiGatewayWebhookConstructProps {
  /** API name */
  apiName: string;
  /** Lambda function to handle webhook requests */
  handler: lambda.IFunction;
  /** Resource path segments (e.g. ['webhook', 'review-callback']) */
  pathSegments: string[];
  /** Stage name (default: 'prod') */
  stageName?: string;
  /** Cognito User Pool ARN for OAuth2 authorization (if not provided, creates one) */
  cognitoUserPoolArn?: string;
  /** OAuth2 scopes required (e.g. ['webhook/callback']) */
  oauthScopes?: string[];
  /** Throttle rate limit (requests per second, default: 10) */
  throttleRateLimit?: number;
  /** Throttle burst limit (default: 20) */
  throttleBurstLimit?: number;
  /** Optional NamingUtil for validation */
  naming?: NamingUtil;
  /** Optional SSM prefix for storing API URL and Cognito details */
  ssmPrefix?: string;
}

export class ApiGatewayWebhookConstruct extends Construct {
  public readonly api: apigateway.RestApi;
  public readonly apiUrl: string;
  public readonly userPool: cognito.IUserPool;
  public readonly userPoolClientId: string = '';

  constructor(scope: Construct, id: string, props: ApiGatewayWebhookConstructProps) {
    super(scope, id);

    if (props.naming) {
      const validation = props.naming.validateResourceName(props.apiName);
      if (!validation.isValid) {
        console.warn(`API Gateway naming validation warnings for "${props.apiName}":`);
        validation.errors.forEach((e) => console.warn(`  - ${e}`));
      }
    }

    // Cognito User Pool for OAuth2
    let userPool: cognito.IUserPool;
    if (props.cognitoUserPoolArn) {
      userPool = cognito.UserPool.fromUserPoolArn(this, 'ExistingPool', props.cognitoUserPoolArn);
    } else {
      const pool = new cognito.UserPool(this, 'UserPool', {
        userPoolName: `${props.apiName}-auth`,
        selfSignUpEnabled: false,
        signInAliases: { email: false, username: true },
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

      // Resource server for OAuth2 scopes
      const resourceServer = pool.addResourceServer('ResourceServer', {
        identifier: 'webhook',
        scopes: [{ scopeName: 'callback', scopeDescription: 'Webhook callback access' }],
      });

      // App client for TicketSystem (client credentials flow)
      const client = pool.addClient('TicketSystemClient', {
        userPoolClientName: `${props.apiName}-sn-client`,
        generateSecret: true,
        oAuth: {
          flows: { clientCredentials: true },
          scopes: [cognito.OAuthScope.resourceServer(resourceServer, {
            scopeName: 'callback',
            scopeDescription: 'Webhook callback access',
          })],
        },
      });

      // Domain for token endpoint
      pool.addDomain('Domain', {
        cognitoDomain: { domainPrefix: props.apiName.replace(/[^a-z0-9-]/g, '-').toLowerCase() },
      });

      userPool = pool;
      this.userPoolClientId = client.userPoolClientId;
    }
    this.userPool = userPool;

    // API Gateway
    this.api = new apigateway.RestApi(this, 'Api', {
      restApiName: props.apiName,
      deployOptions: {
        stageName: props.stageName || 'prod',
        throttlingRateLimit: props.throttleRateLimit ?? 10,
        throttlingBurstLimit: props.throttleBurstLimit ?? 20,
      },
    });

    // Cognito authorizer
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'Authorizer', {
      cognitoUserPools: [userPool],
      authorizerName: `${props.apiName}-authorizer`,
    });

    // Build resource path
    let resource = this.api.root;
    for (const segment of props.pathSegments) {
      resource = resource.addResource(segment);
    }

    // POST method with Cognito authorization
    resource.addMethod('POST', new apigateway.LambdaIntegration(props.handler), {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
      authorizationScopes: props.oauthScopes || ['webhook/callback'],
    });

    this.apiUrl = this.api.url;

    if (props.ssmPrefix) {
      new ssm.StringParameter(this, 'ApiUrlParam', {
        parameterName: `${props.ssmPrefix}/api-gateway/${id}/url`,
        stringValue: this.api.url,
        description: `API Gateway URL for ${props.apiName}`,
      });
    }
  }
}
