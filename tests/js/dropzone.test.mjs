import test from 'node:test';
import assert from 'node:assert/strict';

import { setupFileDropzone } from '../../frontend/dropzone.js';

function fixture() {
  const listeners = new Map();
  const active = new Set();
  const zone = {
    classList: { toggle(name, enabled) { enabled ? active.add(name) : active.delete(name); } },
    addEventListener(name, handler) { listeners.set(name, handler); },
    removeEventListener() {},
  };
  const host = { addEventListener() {}, removeEventListener() {} };
  return { listeners, active, zone, host };
}

function fileEvent(files = []) {
  return {
    dataTransfer: { types: ['Files'], files, dropEffect: 'none' },
    preventDefault() {},
    stopPropagation() {},
  };
}

test('nested drag events keep the highlight stable', () => {
  const { listeners, active, zone, host } = fixture();
  setupFileDropzone(zone, () => {}, { host });
  listeners.get('dragenter')(fileEvent());
  listeners.get('dragenter')(fileEvent());
  listeners.get('dragleave')(fileEvent());
  assert.ok(active.has('dragging'));
  listeners.get('dragleave')(fileEvent());
  assert.equal(active.has('dragging'), false);
});

test('drop clears highlight and forwards files once', async () => {
  const { listeners, active, zone, host } = fixture();
  const received = [];
  setupFileDropzone(zone, (files) => received.push(files), { host });
  const event = fileEvent([{ name: 'mod.zip' }]);
  listeners.get('dragenter')(event);
  listeners.get('dragover')(event);
  assert.equal(event.dataTransfer.dropEffect, 'copy');
  await listeners.get('drop')(event);
  assert.equal(active.has('dragging'), false);
  assert.equal(received.length, 1);
});

test('empty WebView drop uses explicit fallback feedback', async () => {
  const { listeners, zone, host } = fixture();
  let empty = 0;
  const onEmpty = () => { empty += 1; };
  setupFileDropzone(zone, () => assert.fail('file handler ran'), { host, onEmpty });
  await listeners.get('drop')(fileEvent());
  assert.equal(empty, 1);
});
