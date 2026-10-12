const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const config = require('../forge.config.cjs');
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json')));
assert.equal(pkg.main, 'electron/main.cjs');
assert.equal(config.packagerConfig.asar, true);
assert.deepEqual(config.packagerConfig.extraResource, ['resources/backend', 'resources/tools']);
assert(config.makers.some(maker => maker.name === '@electron-forge/maker-zip'));
for (const file of ['electron/main.cjs', 'electron/preload.cjs', 'scripts/build-backend.mjs', 'scripts/prepare-tools.mjs', 'mynotes-backend.spec']) {
  assert(fs.existsSync(path.join(root, file)), `Missing packaging source: ${file}`);
}
console.log('Packaging configuration checks passed. Run the packaged desktop smoke for runtime acceptance.');
