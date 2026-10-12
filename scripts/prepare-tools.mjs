import { install, Browser } from '@puppeteer/browsers';
import { cpSync, mkdirSync, copyFileSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { resolve, join } from 'node:path';

const root = fileURLToPath(new URL('..', import.meta.url));
const flag = process.argv.find(arg => arg.startsWith('--out-dir='));
const tools = resolve(flag?.slice('--out-dir='.length) ?? join(root, 'resources/tools'));
mkdirSync(tools, { recursive: true });
copyFileSync(process.execPath, join(tools, 'node'));
const marp = join(tools, 'marp');
mkdirSync(marp, { recursive: true });
// Use the project's lock, including every transitive production dependency.
for (const name of ['package.json', 'package-lock.json']) copyFileSync(join(root, name), join(marp, name));
const result = spawnSync('npm', ['ci', '--omit=dev', '--ignore-scripts'], { cwd: marp, stdio: 'inherit' });
if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status ?? 1);
const found = spawnSync('which', ['opencode'], { encoding: 'utf8' });
if (found.status === 0) copyFileSync(found.stdout.trim(), join(tools, 'opencode'));
const browser = await install({ browser: Browser.CHROMEHEADLESSSHELL,
  buildId: '155.0.8059.39', cacheDir: join(tools, 'browser') });
writeFileSync(join(tools, 'browser.json'), JSON.stringify({ executable: browser.executablePath.slice(tools.length + 1) }));
console.log('Bundled standalone Node, Marp dependencies and headless browser:', tools);
