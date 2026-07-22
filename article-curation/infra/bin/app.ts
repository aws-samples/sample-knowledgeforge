#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import * as yaml from 'js-yaml';
import * as path from 'path';
import * as fs from 'fs';
import { ArticleCurationSharedStack } from '../lib/article-curation-shared-stack';
import { ArticleCurationTenantStack } from '../lib/article-curation-tenant-stack';

// Load global environments config
const globalEnvPath = path.resolve(__dirname, '..', 'config', 'global-environments.yaml');
const globalEnv = yaml.load(fs.readFileSync(globalEnvPath, 'utf-8')) as any;

// Load base defaults (generic, same across all environments)
const basePath = path.resolve(__dirname, '..', 'config', '_defaults', 'base.yaml');
const baseConfig = yaml.load(fs.readFileSync(basePath, 'utf-8')) as any;

// Determine environment from CDK context (required)
const app = new cdk.App();
const envName = app.node.tryGetContext('env');
const regionName = app.node.tryGetContext('region');
const project = globalEnv.project;
if (!project) {
  throw new Error("Missing required 'project' field in global-environments.yaml");
}

if (!envName) {
  throw new Error('Missing required CDK context: -c env=<environment> (e.g. dev, uat, prod)');
}
if (!regionName) {
  throw new Error('Missing required CDK context: -c region=<region> (e.g. eu-west-1)');
}

// Resolve environment config
const envConfig = globalEnv.environments?.[envName];
if (!envConfig) {
  throw new Error(`Environment "${envName}" not found in global-environments.yaml`);
}

const regionConfig = envConfig.regions?.[regionName];
if (!regionConfig) {
  throw new Error(`Region "${regionName}" not found for environment "${envName}"`);
}

const envCode = envName.charAt(0);
const regionCode = computeRegionCode(regionName);
const accountId = regionConfig.account_id;
const sharedPrefix = `${envCode}-${regionCode}`;

// Load environment-specific overrides (e.g. SQS policies, Glue config, bucket names)
// Merge order: base.yaml → env override → tenant override
const envOverridePath = path.resolve(__dirname, '..', 'config', envName, `${regionName}.yaml`);
let envOverrides: any = {};
if (fs.existsSync(envOverridePath)) {
  envOverrides = yaml.load(fs.readFileSync(envOverridePath, 'utf-8')) as any || {};
}
const envMergedConfig = deepMerge(JSON.parse(JSON.stringify(baseConfig)), envOverrides);

// Discover tenants from tenants/ directory
const tenantsDir = path.resolve(__dirname, '..', 'tenants');
const tenantIds = fs.readdirSync(tenantsDir).filter(d =>
  fs.statSync(path.join(tenantsDir, d)).isDirectory()
);

if (tenantIds.length === 0) {
  throw new Error('No tenants found in tenants/ directory');
}

// Load tenant configs (base merged with tenant overrides)
const tenants: Array<{ tenantId: string; config: any; overrides: any }> = [];

// Resolve Bedrock inference profile prefix from region mapping
const inferencePrefix = globalEnv.inference_prefix_mappings?.[regionName] || 'global';
envMergedConfig.models.llm_model_id = `arn:aws:bedrock:${regionName}:${accountId}:inference-profile/${inferencePrefix}.${envMergedConfig.models.llm_model_id}`;

for (const tenantId of tenantIds) {
  const tenantConfigPath = path.join(tenantsDir, tenantId, 'config', envName, `${regionName}.yaml`);

  // Skip tenants that don't have a config for this environment/region
  if (!fs.existsSync(tenantConfigPath)) {
    console.log(`[${tenantId}] No config found at ${tenantConfigPath} — skipping`);
    continue;
  }

  const tenantOverrides = yaml.load(fs.readFileSync(tenantConfigPath, 'utf-8')) as any || {};

  // Deep merge: base + env override + tenant override
  const merged = deepMerge(JSON.parse(JSON.stringify(envMergedConfig)), tenantOverrides);

  // Resolve placeholders
  merged.tenant_id = tenantId;
  merged.environment = envName;
  merged.env_code = envCode;
  merged.region = regionName;
  merged.region_code = regionCode;

  tenants.push({ tenantId, config: merged, overrides: tenantOverrides });
}

// Tags from global config
const tags = globalEnv.tags || {};


// Shared stack
const sharedStack = new ArticleCurationSharedStack(app, `shared-${envCode}-${regionCode}-kbcuration`, {
  env: { account: accountId, region: regionName },
  project,
  envName,
  envCode,
  regionCode,
  sharedPrefix,
  baseConfig: envMergedConfig,
  tags,
  tenants,
});

// Per-tenant stacks (one per connect region)
for (const tenant of tenants) {
  // The tenant config file is now a list under 'stacks', each entry keyed by connect_region.
  // Stack name format: <tenant>-<envCode>-<regionCode>-<connectRegion>-kbcuration
  // e.g. acme-d-euw1-us-east-1-kbcuration
  const stackEntries: any[] = tenant.config.stacks;
  if (!stackEntries || !Array.isArray(stackEntries) || stackEntries.length === 0) {
    console.log(`[${tenant.tenantId}] No 'stacks' list in config for ${envName}/${regionName} — skipping`);
    continue;
  }

  for (const stackEntry of stackEntries) {
    const connectRegion = stackEntry.connect_region;
    if (!connectRegion) {
      throw new Error(
        `Tenant "${tenant.tenantId}" has a stack entry without 'connect_region' ` +
        `in tenants/${tenant.tenantId}/config/${envName}/${regionName}.yaml`
      );
    }

    // Merge: base config + per-stack overrides (stack entry values override base)
    const stackConfig = deepMerge(JSON.parse(JSON.stringify(tenant.config)), stackEntry);
    // Remove the top-level 'stacks' array from the merged config (not needed downstream)
    delete stackConfig.stacks;

    const stackName = `${tenant.tenantId}-${envCode}-${regionCode}-${connectRegion}-kbcuration`;

    new ArticleCurationTenantStack(app, stackName, {
      env: { account: accountId, region: regionName },
      project,
      envName,
      envCode,
      regionCode,
      tenantId: tenant.tenantId,
      tenantConfig: stackConfig,
      tenantOverrides: stackEntry,
      sharedPrefix,
      tags,
    });
  }
}

// Deep merge utility
function deepMerge(base: any, override: any): any {
  const result = { ...base };
  for (const key of Object.keys(override)) {
    if (override[key] && typeof override[key] === 'object' && !Array.isArray(override[key])
        && base[key] && typeof base[key] === 'object' && !Array.isArray(base[key])) {
      result[key] = deepMerge(base[key], override[key]);
    } else {
      result[key] = override[key];
    }
  }
  return result;
}

/**
 * Compute a short region code from a full AWS region name.
 *
 * Formula: <first 2 chars><direction_short><number>
 *   Direction mappings:
 *     north → n, south → s, east → e, west → w
 *     northeast → ne, southeast → se, northwest → nw, southwest → sw
 *     central → c
 */
function computeRegionCode(region: string): string {
  const directionMap: Record<string, string> = {
    northeast: 'ne',
    northwest: 'nw',
    southeast: 'se',
    southwest: 'sw',
    north: 'n',
    south: 's',
    east: 'e',
    west: 'w',
    central: 'c',
  };

  // Parse: <geo>-<direction>-<number>  e.g. "eu-west-1", "ap-southeast-2"
  const parts = region.split('-');
  if (parts.length < 3) {
    throw new Error(`Cannot compute region code from "${region}" — expected format: <geo>-<direction>-<number>`);
  }

  const geo = parts[0]; // eu, us, ap, ca, me, af, sa, il
  const number = parts[parts.length - 1]; // 1, 2, 3...
  // Direction is everything between geo and number
  const direction = parts.slice(1, -1).join('');

  const dirShort = directionMap[direction];
  if (!dirShort) {
    throw new Error(`Unknown direction "${direction}" in region "${region}". Known: ${Object.keys(directionMap).join(', ')}`);
  }

  return `${geo}${dirShort}${number}`;
}
