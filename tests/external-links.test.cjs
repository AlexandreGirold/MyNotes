const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

test('opens HTTPS source links externally and refuses credentials or executable schemes', () => {
  const source = readFileSync('electron/main.cjs', 'utf8');
  const section = source.slice(source.indexOf('win.webContents.setWindowOpenHandler('), source.indexOf('const trusted ='));
  const opened = []; let handler;
  vm.runInNewContext(section, { URL, console, shell: { openExternal: async url => opened.push(url) },
    win: { webContents: { setWindowOpenHandler: fn => { handler = fn; } } } });
  assert.equal(handler({ url: 'https://example.com/source' }).action, 'deny');
  for (const url of ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:password@example.com', 'not a URL']) {
    assert.equal(handler({ url }).action, 'deny');
  }
  assert.deepEqual(opened, ['https://example.com/source']);
});
