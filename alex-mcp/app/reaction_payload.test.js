'use strict';

import assert from 'node:assert/strict';
import {
  reactionConversationJid,
  reactionSenderCandidates,
} from './reaction_payload.js';

assert.deepEqual(
  reactionSenderCandidates({
    participantAlt: '60111111111@s.whatsapp.net',
    participant: '12345@lid',
    remoteJid: '120363000000@g.us',
  }),
  ['60111111111@s.whatsapp.net', '12345@lid'],
);

assert.deepEqual(
  reactionSenderCandidates({
    remoteJidAlt: '60111111111@s.whatsapp.net',
    remoteJid: '12345@lid',
  }),
  ['60111111111@s.whatsapp.net', '12345@lid'],
);

assert.equal(
  reactionConversationJid(
    false, '12345@lid', '60111111111@s.whatsapp.net'
  ),
  '60111111111@s.whatsapp.net',
);

assert.equal(
  reactionConversationJid(
    true, '120363000000@g.us', '60111111111@s.whatsapp.net'
  ),
  '120363000000@g.us',
);

console.log('WhatsApp reaction identity contract OK');
