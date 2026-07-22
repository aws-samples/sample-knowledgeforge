import { NamingUtil } from '../naming';

describe('NamingUtil', () => {
  test('validates correct resource name', () => {
    const naming = new NamingUtil({
      tenantId: 'acme',
      envCode: 'd',
      regionCode: 'use1',
    });

    const validation = naming.validateResourceName('acme-d-use1-lambda-processor');
    expect(validation.isValid).toBe(true);
    expect(validation.errors).toHaveLength(0);
  });

  test('generates correct prefix', () => {
    const naming = new NamingUtil({
      tenantId: 'acme',
      envCode: 'p',
      regionCode: 'use1',
    });

    const prefix = naming.getPrefix();
    expect(prefix).toBe('acme-p-use1');
  });

  test('handles different environments', () => {
    const envs = ['s', 'd', 'u', 'p'];

    envs.forEach((env) => {
      const naming = new NamingUtil({
        tenantId: 'test',
        envCode: env,
        regionCode: 'use1',
      });
      const prefix = naming.getPrefix();
      expect(prefix).toContain(env);
    });
  });

  test('validates resource name with functionality', () => {
    const naming = new NamingUtil({
      tenantId: 'acme',
      envCode: 'd',
      regionCode: 'use1',
    });

    const validation = naming.validateResourceName(
      'acme-d-use1-order-processor',
      'order-processor'
    );
    expect(validation.isValid).toBe(true);
  });

  test('detects invalid resource name', () => {
    const naming = new NamingUtil({
      tenantId: 'acme',
      envCode: 'd',
      regionCode: 'use1',
    });

    const validation = naming.validateResourceName('wrong-d-use1-lambda');
    expect(validation.isValid).toBe(false);
    expect(validation.errors.length).toBeGreaterThan(0);
  });

  test('generates correct SSM path', () => {
    const naming = new NamingUtil({
      tenantId: 'acme',
      envCode: 'd',
      regionCode: 'use1',
    });

    const ssmPath = naming.generateSsmPath('lambda', 'OrderProcessor', 'arn');
    expect(ssmPath).toBe('/acme-d-use1/lambda/OrderProcessor/arn');
  });
});
