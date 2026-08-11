/**
 * Utility to access construct version information at runtime
 */

import * as fs from 'fs';
import * as path from 'path';

export interface ConstructVersionInfo {
  version: string;
  lastUpdated: string;
  changelog: string;
}

export interface ConstructVersions {
  packageVersion: string;
  constructs: Record<string, ConstructVersionInfo>;
}

/**
 * Get version information for all constructs
 */
export function getConstructVersions(): ConstructVersions {
  try {
    const versionsPath = path.join(__dirname, '..', 'construct-versions.json');
    const versionsData = fs.readFileSync(versionsPath, 'utf-8');
    return JSON.parse(versionsData);
  } catch (error) {
    console.warn('Could not load construct versions:', error);
    return {
      packageVersion: 'unknown',
      constructs: {},
    };
  }
}

/**
 * Get version information for a specific construct
 */
export function getConstructVersion(constructName: string): ConstructVersionInfo | null {
  const versions = getConstructVersions();
  return versions.constructs[constructName] || null;
}

/**
 * Get the overall package version
 */
export function getPackageVersion(): string {
  const versions = getConstructVersions();
  return versions.packageVersion;
}

/**
 * Check if a construct version meets a minimum requirement
 */
export function meetsMinimumVersion(constructName: string, minVersion: string): boolean {
  const constructInfo = getConstructVersion(constructName);
  if (!constructInfo) return false;

  const [currentMajor, currentMinor, currentPatch] = constructInfo.version.split('.').map(Number);
  const [minMajor, minMinor, minPatch] = minVersion.split('.').map(Number);

  if (currentMajor > minMajor) return true;
  if (currentMajor < minMajor) return false;

  if (currentMinor > minMinor) return true;
  if (currentMinor < minMinor) return false;

  return currentPatch >= minPatch;
}

/**
 * Get all constructs with their versions as a formatted string
 */
export function getVersionSummary(): string {
  const versions = getConstructVersions();
  const lines = [`Package Version: ${versions.packageVersion}`, '', 'Constructs:'];

  Object.entries(versions.constructs).forEach(([name, info]) => {
    lines.push(`  ${name}: v${info.version} (${info.lastUpdated})`);
    lines.push(`    ${info.changelog}`);
  });

  return lines.join('\n');
}
