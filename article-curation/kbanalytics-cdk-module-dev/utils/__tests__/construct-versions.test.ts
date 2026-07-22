/**
 * Tests for construct version utilities
 */

import * as fs from 'fs';
import {
  getConstructVersions,
  getConstructVersion,
  getPackageVersion,
  meetsMinimumVersion,
  getVersionSummary,
} from '../construct-versions';

// Mock fs module
jest.mock('fs');
const mockFs = fs as jest.Mocked<typeof fs>;

describe('Construct Version Utilities', () => {
  const mockVersionData = {
    packageVersion: '1.2.3',
    constructs: {
      lambda: {
        version: '1.1.0',
        lastUpdated: '2026-02-23T10:00:00.000Z',
        changelog: 'Added timeout configuration',
      },
      lex: {
        version: '1.0.5',
        lastUpdated: '2026-02-20T10:00:00.000Z',
        changelog: 'Fixed slot type bug',
      },
      connect: {
        version: '2.0.0',
        lastUpdated: '2026-02-22T10:00:00.000Z',
        changelog: 'Breaking: Changed API structure',
      },
    },
  };

  beforeEach(() => {
    jest.clearAllMocks();
  });

  describe('getConstructVersions', () => {
    it('should return version data when file exists', () => {
      mockFs.readFileSync.mockReturnValue(JSON.stringify(mockVersionData));

      const result = getConstructVersions();

      expect(result).toEqual(mockVersionData);
      expect(mockFs.readFileSync).toHaveBeenCalledWith(
        expect.stringContaining('construct-versions.json'),
        'utf-8'
      );
    });

    it('should return default data when file does not exist', () => {
      mockFs.readFileSync.mockImplementation(() => {
        throw new Error('File not found');
      });

      const result = getConstructVersions();

      expect(result).toEqual({
        packageVersion: 'unknown',
        constructs: {},
      });
    });

    it('should return default data when JSON is invalid', () => {
      mockFs.readFileSync.mockReturnValue('invalid json');

      const result = getConstructVersions();

      expect(result).toEqual({
        packageVersion: 'unknown',
        constructs: {},
      });
    });
  });

  describe('getConstructVersion', () => {
    beforeEach(() => {
      mockFs.readFileSync.mockReturnValue(JSON.stringify(mockVersionData));
    });

    it('should return version info for existing construct', () => {
      const result = getConstructVersion('lambda');

      expect(result).toEqual({
        version: '1.1.0',
        lastUpdated: '2026-02-23T10:00:00.000Z',
        changelog: 'Added timeout configuration',
      });
    });

    it('should return null for non-existing construct', () => {
      const result = getConstructVersion('nonexistent');

      expect(result).toBeNull();
    });

    it('should return null when file cannot be read', () => {
      mockFs.readFileSync.mockImplementation(() => {
        throw new Error('File not found');
      });

      const result = getConstructVersion('lambda');

      expect(result).toBeNull();
    });
  });

  describe('getPackageVersion', () => {
    it('should return package version when file exists', () => {
      mockFs.readFileSync.mockReturnValue(JSON.stringify(mockVersionData));

      const result = getPackageVersion();

      expect(result).toBe('1.2.3');
    });

    it('should return "unknown" when file does not exist', () => {
      mockFs.readFileSync.mockImplementation(() => {
        throw new Error('File not found');
      });

      const result = getPackageVersion();

      expect(result).toBe('unknown');
    });
  });

  describe('meetsMinimumVersion', () => {
    beforeEach(() => {
      mockFs.readFileSync.mockReturnValue(JSON.stringify(mockVersionData));
    });

    it('should return true when current version equals minimum', () => {
      const result = meetsMinimumVersion('lambda', '1.1.0');
      expect(result).toBe(true);
    });

    it('should return true when current version is higher (major)', () => {
      const result = meetsMinimumVersion('connect', '1.0.0');
      expect(result).toBe(true);
    });

    it('should return true when current version is higher (minor)', () => {
      const result = meetsMinimumVersion('lambda', '1.0.0');
      expect(result).toBe(true);
    });

    it('should return true when current version is higher (patch)', () => {
      const result = meetsMinimumVersion('lex', '1.0.0');
      expect(result).toBe(true);
    });

    it('should return false when current version is lower (major)', () => {
      const result = meetsMinimumVersion('lambda', '2.0.0');
      expect(result).toBe(false);
    });

    it('should return false when current version is lower (minor)', () => {
      const result = meetsMinimumVersion('lambda', '1.2.0');
      expect(result).toBe(false);
    });

    it('should return false when current version is lower (patch)', () => {
      const result = meetsMinimumVersion('lambda', '1.1.5');
      expect(result).toBe(false);
    });

    it('should return false when construct does not exist', () => {
      const result = meetsMinimumVersion('nonexistent', '1.0.0');
      expect(result).toBe(false);
    });

    it('should handle edge case versions correctly', () => {
      expect(meetsMinimumVersion('lambda', '0.0.0')).toBe(true);
      expect(meetsMinimumVersion('lambda', '999.999.999')).toBe(false);
    });
  });

  describe('getVersionSummary', () => {
    it('should return formatted summary when file exists', () => {
      mockFs.readFileSync.mockReturnValue(JSON.stringify(mockVersionData));

      const result = getVersionSummary();

      expect(result).toContain('Package Version: 1.2.3');
      expect(result).toContain('Constructs:');
      expect(result).toContain('lambda: v1.1.0');
      expect(result).toContain('Added timeout configuration');
      expect(result).toContain('lex: v1.0.5');
      expect(result).toContain('Fixed slot type bug');
      expect(result).toContain('connect: v2.0.0');
      expect(result).toContain('Breaking: Changed API structure');
    });

    it('should return default summary when file does not exist', () => {
      mockFs.readFileSync.mockImplementation(() => {
        throw new Error('File not found');
      });

      const result = getVersionSummary();

      expect(result).toContain('Package Version: unknown');
      expect(result).toContain('Constructs:');
    });

    it('should handle empty constructs object', () => {
      mockFs.readFileSync.mockReturnValue(
        JSON.stringify({
          packageVersion: '1.0.0',
          constructs: {},
        })
      );

      const result = getVersionSummary();

      expect(result).toContain('Package Version: 1.0.0');
      expect(result).toContain('Constructs:');
    });
  });

  describe('Version comparison edge cases', () => {
    beforeEach(() => {
      mockFs.readFileSync.mockReturnValue(JSON.stringify(mockVersionData));
    });

    it('should handle same major, different minor', () => {
      expect(meetsMinimumVersion('lambda', '1.0.0')).toBe(true);
      expect(meetsMinimumVersion('lambda', '1.1.0')).toBe(true);
      expect(meetsMinimumVersion('lambda', '1.2.0')).toBe(false);
    });

    it('should handle same major and minor, different patch', () => {
      expect(meetsMinimumVersion('lex', '1.0.0')).toBe(true);
      expect(meetsMinimumVersion('lex', '1.0.5')).toBe(true);
      expect(meetsMinimumVersion('lex', '1.0.6')).toBe(false);
    });

    it('should handle major version differences correctly', () => {
      expect(meetsMinimumVersion('connect', '1.9.9')).toBe(true);
      expect(meetsMinimumVersion('connect', '2.0.0')).toBe(true);
      expect(meetsMinimumVersion('connect', '2.0.1')).toBe(false);
      expect(meetsMinimumVersion('connect', '3.0.0')).toBe(false);
    });
  });

  describe('Integration scenarios', () => {
    it('should handle complete workflow', () => {
      mockFs.readFileSync.mockReturnValue(JSON.stringify(mockVersionData));

      // Get all versions
      const allVersions = getConstructVersions();
      expect(allVersions.packageVersion).toBe('1.2.3');
      expect(Object.keys(allVersions.constructs)).toHaveLength(3);

      // Get specific version
      const lambdaVersion = getConstructVersion('lambda');
      expect(lambdaVersion?.version).toBe('1.1.0');

      // Check minimum version
      const meetsMin = meetsMinimumVersion('lambda', '1.0.0');
      expect(meetsMin).toBe(true);

      // Get summary
      const summary = getVersionSummary();
      expect(summary).toContain('lambda');
      expect(summary).toContain('lex');
      expect(summary).toContain('connect');
    });
  });
});
