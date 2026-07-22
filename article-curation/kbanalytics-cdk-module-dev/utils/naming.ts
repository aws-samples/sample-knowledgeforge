/**
 * Naming utility for AWS resources
 *
 * Generates and validates resource names using a consistent convention:
 *   <tenantId>-<envCode>-<regionCode>-<functionality>
 *
 * Parameter files (e.g., us-east-1.yaml) provide:
 * - tenant_id: acme, bcme, shared, etc.
 * - env_code: s (sandbox), d (dev), u (uat), p (prod)
 * - region_code: use1, euc1, euw1, etc.
 *
 * Config files use short functional names (e.g. "poll_email") and the stack
 * calls `resolveName()` to auto-prefix them. Escape hatches:
 * - `existing: true` → pre-existing resource, name used as-is for lookups
 * - `raw_name: "full-custom-name"` → bypass auto-prefixing
 */

export interface NamingConfig {
  tenantId: string;
  envCode: string; // s, d, u, p
  regionCode: string; // use1, euc1, euw1, etc.
}

export interface ValidationResult {
  isValid: boolean;
  errors: string[];
}

export class NamingUtil {
  /** Allowed environment codes */
  static readonly ALLOWED_ENV_CODES = ['s', 'd', 'u', 'p'];

  /**
   * Common AWS region short codes derived from AZ ID prefixes.
   * New regions follow the same pattern (e.g. `usw2` for us-west-2, `apse1` for ap-southeast-1).
   */
  static readonly COMMON_REGION_CODES = [
    'use1',
    'use2',
    'usw1',
    'usw2',
    'euc1',
    'euw1',
    'euw2',
    'euw3',
    'apse1',
    'apse2',
    'apne1',
    'apne2',
  ];

  private config: NamingConfig;

  constructor(config: NamingConfig) {
    this.validateConfig(config);
    this.config = config;
  }

  /**
   * Validate the configuration from parameter files
   */
  private validateConfig(config: NamingConfig): void {
    if (!NamingUtil.ALLOWED_ENV_CODES.includes(config.envCode)) {
      throw new Error(
        `Invalid envCode "${config.envCode}". Allowed values: ${NamingUtil.ALLOWED_ENV_CODES.join(', ')} ` +
          `(s=sandbox, d=dev, u=uat, p=prod)`
      );
    }

    if (!NamingUtil.COMMON_REGION_CODES.includes(config.regionCode)) {
      console.warn(
        `Unknown regionCode "${config.regionCode}". Common codes: ${NamingUtil.COMMON_REGION_CODES.join(', ')}. ` +
          'Ensure it follows the AZ ID prefix pattern (e.g. usw2, apse1).'
      );
    }
  }

  /**
   * Generate a prefixed resource name.
   *   prefixName('poll_email')           → 'shared-d-use1-poll_email'
   *   prefixName('poll_email', 'acme')   → 'acme-d-use1-poll_email'
   *
   * @param functionality  Short functional name (e.g. 'poll_email', 'survey_bot')
   * @param tenantOverride Optional tenant prefix override (e.g. 'acme', 'bcme').
   *                       Defaults to the tenantId from config (typically 'shared').
   */
  prefixName(functionality: string, tenantOverride?: string): string {
    const tenant = tenantOverride || this.config.tenantId;
    return `${tenant}-${this.config.envCode}-${this.config.regionCode}-${functionality}`;
  }

  /**
   * Resolve a resource name from config.
   *
   * Priority:
   *   1. cfg.existing === true  → return cfg.name as-is (pre-existing resource)
   *   2. cfg.raw_name is set    → return cfg.raw_name as-is (escape hatch)
   *   3. Otherwise              → auto-prefix cfg.name with tenant-env-region
   *
   * @param cfg             Config object with `name`, optional `raw_name`, optional `existing`
   * @param tenantOverride  Optional tenant prefix override for per-tenant resources
   */
  resolveName(cfg: { name: string; raw_name?: string; existing?: boolean }, tenantOverride?: string): string {
    if (cfg.existing) {
      return cfg.name;
    }
    if (cfg.raw_name) {
      return cfg.raw_name;
    }
    return this.prefixName(cfg.name, tenantOverride);
  }

  /**
   * Validate that a resource name matches the expected pattern from parameter files
   * Expected pattern: <tenant_id>-<env_code>-<region_code>-<functionality>
   * Example: acme-d-use1-order-processor
   */
  validateResourceName(resourceName: string, functionality?: string): ValidationResult {
    const errors: string[] = [];
    const expectedPrefix = `${this.config.tenantId}-${this.config.envCode}-${this.config.regionCode}`;

    if (!resourceName.startsWith(expectedPrefix)) {
      errors.push(
        `Resource name "${resourceName}" does not start with expected prefix "${expectedPrefix}". ` +
          `Expected pattern: ${expectedPrefix}-<functionality>`
      );
    }

    if (functionality) {
      const expectedName = `${expectedPrefix}-${functionality}`;
      if (resourceName !== expectedName) {
        errors.push(
          `Resource name "${resourceName}" does not match expected name "${expectedName}"`
        );
      }
    }

    return {
      isValid: errors.length === 0,
      errors,
    };
  }

  /**
   * Generate SSM parameter path following the convention
   * Pattern: /<tenant_id>-<env_code>-<region_code>/<service>/<resource_id>/<attribute>
   * Example: /acme-d-use1/lambda/OrderProcessor/arn
   */
  generateSsmPath(service: string, resourceId: string, attribute: string = 'arn'): string {
    return `/${this.config.tenantId}-${this.config.envCode}-${this.config.regionCode}/${service}/${resourceId}/${attribute}`;
  }

  /**
   * Get the prefix for resource naming
   * Returns: <tenant_id>-<env_code>-<region_code>
   */
  getPrefix(): string {
    return `${this.config.tenantId}-${this.config.envCode}-${this.config.regionCode}`;
  }

  /**
   * Get the configuration
   */
  getConfig(): NamingConfig {
    return { ...this.config };
  }
}
