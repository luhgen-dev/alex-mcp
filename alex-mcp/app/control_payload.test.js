'use strict';

import assert from 'node:assert/strict';
import { buildControlMessage } from './control_payload.js';

const to = '60111111111@s.whatsapp.net';

const pin = buildControlMessage({
  kind: 'pin',
  target_message_id: 'wa-own-message',
  target_from_me: true,
}, to);
assert.deepEqual(pin.content, {
  pin: { remoteJid: to, id: 'wa-own-message', fromMe: true },
  type: 1,
  time: 2592000,
});

const unpin = buildControlMessage({
  kind: 'unpin',
  target_message_id: 'wa-own-message',
  target_from_me: true,
}, to);
assert.deepEqual(unpin.content, {
  pin: { remoteJid: to, id: 'wa-own-message', fromMe: true },
  type: 2,
});
assert.equal('time' in unpin.content, false);

const inboundUnpin = buildControlMessage({
  kind: 'unpin',
  target_message_id: 'wa-user-message',
  target_from_me: false,
}, to);
assert.equal(inboundUnpin.targetKey.fromMe, false);

const reaction = buildControlMessage({
  kind: 'reaction',
  target_message_id: 'wa-user-message',
  target_from_me: false,
  emoji: '⏳',
}, to);
assert.deepEqual(reaction.content.react, {
  text: '⏳',
  key: { remoteJid: to, id: 'wa-user-message', fromMe: false },
});

console.log('WhatsApp control payload contract OK');
