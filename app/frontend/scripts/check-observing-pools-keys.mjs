#!/usr/bin/env node
// Guard against dead / mismatched observingPools.* i18n keys.
//
// tsc already enforces that `zhCN: Record<TranslationKey, string>` covers every `en`
// key, but nothing stops a key from being defined-and-never-used (translation rot).
// This checker fails (exit 1) if any `observingPools.*` key defined in `en` is never
// referenced from a component, or if the EN/zhCN observingPools key sets diverge.
//
// Run via `npm run check-i18n`.

import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = join(__dirname, '..', 'src');
const TRANSLATIONS = join(SRC, 'i18n', 'translations.ts');
const KEY_RE = /observingPools\.[A-Za-z0-9_]+/g;

function block(text, startMarker, endMarker) {
  const start = text.indexOf(startMarker);
  if (start === -1) throw new Error(`marker not found: ${startMarker}`);
  const end = text.indexOf(endMarker, start + startMarker.length);
  if (end === -1) throw new Error(`end marker not found: ${endMarker} after ${startMarker}`);
  return text.slice(start, end);
}

function keysIn(text) {
  return new Set(text.match(KEY_RE) ?? []);
}

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(p));
    else if (/\.tsx?$/.test(entry.name)) out.push(p);
  }
  return out;
}

const tr = readFileSync(TRANSLATIONS, 'utf8');
const enKeys = keysIn(block(tr, 'export const en = {', '} as const;'));
const zhKeys = keysIn(block(tr, 'export const zhCN', '\n};'));

// Referenced keys = every observingPools.* literal outside the translations file.
const used = new Set();
for (const file of walk(SRC)) {
  if (file === TRANSLATIONS) continue;
  for (const m of readFileSync(file, 'utf8').match(KEY_RE) ?? []) used.add(m);
}

const errors = [];

const dead = [...enKeys].filter((k) => !used.has(k)).sort();
if (dead.length) {
  errors.push(
    `Dead observingPools i18n key(s) — defined but never referenced from a component:\n` +
      dead.map((k) => `  - ${k}`).join('\n') +
      `\nRemove them from both \`en\` and \`zhCN\` in ${relative(process.cwd(), TRANSLATIONS)}.`,
  );
}

const onlyEn = [...enKeys].filter((k) => !zhKeys.has(k)).sort();
const onlyZh = [...zhKeys].filter((k) => !enKeys.has(k)).sort();
if (onlyEn.length || onlyZh.length) {
  errors.push(
    `EN/zhCN observingPools key sets diverge:\n` +
      (onlyEn.length ? `  only in en:   ${onlyEn.join(', ')}\n` : '') +
      (onlyZh.length ? `  only in zhCN: ${onlyZh.join(', ')}` : ''),
  );
}

if (errors.length) {
  console.error(`✗ check-observing-pools-keys: ${errors.length} problem(s)\n`);
  console.error(errors.join('\n\n'));
  process.exit(1);
}

console.log(`✓ check-observing-pools-keys: ${enKeys.size} observingPools keys, all referenced and EN/zhCN in sync.`);
