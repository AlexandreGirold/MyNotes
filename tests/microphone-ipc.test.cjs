const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

function handler(executeFile) {
  const source = readFileSync('electron/main.cjs', 'utf8');
  const section = source.slice(source.indexOf('async function microphoneVolume()'), source.indexOf("ipcMain.handle('selectDirectories'"));
  const handlers = {};
  vm.runInNewContext(section, { executeFile, process: { platform: 'linux' }, setTimeout,
    checkSender() {}, ipcMain: { handle: (name, fn) => { handlers[name] = fn; } } });
  return handlers.setMicrophoneVolume;
}

test('pins the microphone ID and verifies delayed volume readback', async () => {
  const calls = []; let reads = 0;
  const set = handler(async (tool, args) => {
    calls.push([tool, args]);
    if (args[0] === 'inspect') return { stdout: 'id 53, type PipeWire:Interface:Node\n node.name = microphone\n' };
    if (args[0] === 'get-volume') return { stdout: `Volume: ${++reads === 1 ? '0.27' : '1.20'}\n` };
    return { stdout: '' };
  });
  assert.equal((await set({}, 120)).volume, 120);
  assert.deepEqual(Array.from(calls[1][1]), ['set-volume', '-l', '1.2', '53', '120%']);
  assert.ok(calls.filter(c => c[1][0] === 'get-volume').every(c => c[1][1] === '53'));
  await assert.rejects(set({}, 121), /between 0 and 120/);
});

test('rejects actual mismatch rather than claiming requested volume was saved', async () => {
  const set = handler(async (_, args) => ({ stdout: args[0] === 'inspect' ? 'id 53, type PipeWire:Interface:Node\n' : 'Volume: 0.27\n' }));
  await assert.rejects(set({}, 80), /did not update/);
});
