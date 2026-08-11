#!/usr/bin/env node
/**
 * Post-build script: copies non-TS asset directories (e.g. lambda/)
 * from constructs/ into lib/constructs/ so that path.join(__dirname, 'lambda')
 * resolves correctly when the package is consumed from node_modules.
 */
const fs = require('fs');
const path = require('path');

const SRC_ROOT = path.resolve(__dirname, '..', 'constructs');
const OUT_ROOT = path.resolve(__dirname, '..', 'lib', 'constructs');

// Directory names that contain non-TS assets to copy
const ASSET_DIRS = ['lambda'];

function copyDirSync(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDirSync(srcPath, destPath);
    } else {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

let copied = 0;

function hasNonTsFiles(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === '__tests__' || entry.name === 'node_modules') continue;
    if (entry.isDirectory()) {
      if (hasNonTsFiles(path.join(dir, entry.name))) return true;
    } else if (!entry.name.endsWith('.ts') && !entry.name.endsWith('.d.ts')) {
      return true;
    }
  }
  return false;
}

function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const full = path.join(dir, entry.name);
    if (ASSET_DIRS.includes(entry.name) && hasNonTsFiles(full)) {
      const rel = path.relative(SRC_ROOT, full);
      const dest = path.join(OUT_ROOT, rel);
      copyDirSync(full, dest);
      copied++;
      console.log(`  copied: constructs/${rel} → lib/constructs/${rel}`);
    } else {
      walk(full);
    }
  }
}

console.log('Copying lambda assets into lib/ ...');
walk(SRC_ROOT);
console.log(`Done — ${copied} asset director${copied === 1 ? 'y' : 'ies'} copied.`);
