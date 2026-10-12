import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
const root = fileURLToPath(new URL('..', import.meta.url));
const result = spawnSync('uv', ['run', '--locked', '--extra', 'dev', 'python', '-m', 'PyInstaller',
  '--noconfirm', '--clean', '--distpath', 'resources/backend', '--workpath', 'build/backend',
  'mynotes-backend.spec'], { cwd: root, stdio: 'inherit' });
if (result.error) throw result.error;
if (result.status !== 0 || !existsSync(resolve(root, 'resources/backend/mynotes/mynotes'))) process.exit(1);
