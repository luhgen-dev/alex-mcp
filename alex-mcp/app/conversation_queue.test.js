'use strict';

import assert from 'node:assert/strict';
import { ConversationQueue } from './conversation_queue.js';

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function testSameConversationIsFifo() {
  const q = new ConversationQueue();
  const seen = [];
  const first = q.enqueue('dm-a', async () => {
    seen.push('first-start');
    await sleep(35);
    seen.push('first-end');
  });
  const second = q.enqueue('dm-a', async () => {
    seen.push('second-start');
    await sleep(1);
    seen.push('second-end');
  });
  await Promise.all([first, second]);
  assert.deepEqual(seen, [
    'first-start', 'first-end', 'second-start', 'second-end',
  ]);
}

async function testDifferentConversationsCanOverlap() {
  const q = new ConversationQueue();
  const seen = [];
  let releaseA;
  const gateA = new Promise(resolve => { releaseA = resolve; });
  const a = q.enqueue('dm-a', async () => {
    seen.push('a-start');
    await gateA;
    seen.push('a-end');
  });
  const b = q.enqueue('dm-b', async () => {
    seen.push('b-start');
    seen.push('b-end');
  });
  await b;
  assert.deepEqual(seen.slice(0, 3), ['a-start', 'b-start', 'b-end']);
  releaseA();
  await a;
  assert.equal(seen.at(-1), 'a-end');
}

async function testFailureDoesNotBlockLane() {
  const q = new ConversationQueue();
  const seen = [];
  const first = q.enqueue('dm-a', async () => {
    seen.push('bad');
    throw new Error('expected');
  }).catch(() => undefined);
  const second = q.enqueue('dm-a', async () => {
    seen.push('good');
  });
  await Promise.all([first, second]);
  assert.deepEqual(seen, ['bad', 'good']);
  await Promise.resolve();
  assert.equal(q.pendingLanes(), 0);
}

await testSameConversationIsFifo();
await testDifferentConversationsCanOverlap();
await testFailureDoesNotBlockLane();
console.log('conversation queue contract OK');
