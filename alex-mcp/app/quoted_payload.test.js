'use strict';

import assert from 'node:assert/strict';
import { quotedHandoffFromContext } from './quoted_payload.js';

// Realistic Baileys group reply to an ordinary spouse text.
const spouseText = quotedHandoffFromContext({
  stanzaId: '3EB0ORDINARY',
  participant: '123456789@lid',
  participantAlt: '60222222222@s.whatsapp.net',
  quotedMessage: {
    conversation: 'Remind us to pick up the rubbish later',
  },
});
assert.deepEqual(spouseText, {
  quoted_text: 'Remind us to pick up the rubbish later',
  quoted_type: 'text',
  quoted_participant_jid: '60222222222@s.whatsapp.net',
  quoted_participant_alt_jid: '60222222222@s.whatsapp.net',
});

// Slot-only answer handed to Alex by swipe reply + mention.
const slot = quotedHandoffFromContext({
  stanzaId: '3EB0TIME',
  participant: '60111111111@s.whatsapp.net',
  quotedMessage: {
    extendedTextMessage: { text: '6.45pm' },
  },
});
assert.equal(slot.quoted_text, '6.45pm');
assert.equal(slot.quoted_type, 'text');

// Media is typed explicitly but never converted to fake text.
const image = quotedHandoffFromContext({
  quotedMessage: { imageMessage: { caption: 'receipt' } },
});
assert.equal(image.quoted_type, 'image');
assert.equal(image.quoted_text, '');

const pdf = quotedHandoffFromContext({
  quotedMessage: {
    documentMessage: { mimetype: 'application/pdf', caption: 'statement' },
  },
});
assert.equal(pdf.quoted_type, 'pdf');
assert.equal(pdf.quoted_text, '');

const audio = quotedHandoffFromContext({
  quotedMessage: { audioMessage: { mimetype: 'audio/ogg' } },
});
assert.equal(audio.quoted_type, 'audio');
assert.equal(audio.quoted_text, '');

// Wrapped quoted messages are also handled.
const ephemeral = quotedHandoffFromContext({
  quotedMessage: {
    ephemeralMessage: {
      message: { conversation: 'Check the parcel later' },
    },
  },
});
assert.equal(ephemeral.quoted_type, 'text');
assert.equal(ephemeral.quoted_text, 'Check the parcel later');

console.log('WhatsApp quoted-context payload contract OK');
