const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('PCM worklet clips and encodes little-endian samples, then flushes before acknowledgement', () => {
  let Processor;
  const messages = [];
  class Base {
    constructor() { this.port = { postMessage: event => messages.push(event) }; }
  }
  vm.runInNewContext(readFileSync(path.join(__dirname, '../src/ui/pcm-worklet.js'), 'utf8'), {
    AudioWorkletProcessor: Base,
    registerProcessor: (_name, implementation) => { Processor = implementation; },
  });
  const processor = new Processor();
  processor.process([[new Float32Array([-2, -1, 0, 0.5, 1, 2])]]);
  assert.equal(messages.length, 0);
  processor.port.onmessage({ data: 'flush' });
  const view = new DataView(messages[0].buffer);
  assert.deepEqual(Array.from({ length: 6 }, (_, i) => view.getInt16(i * 2, true)), [-32768, -32768, 0, 16384, 32767, 32767]);
  assert.equal(messages[1].type, 'flushed');
  assert.equal(processor.used, 0);
});
