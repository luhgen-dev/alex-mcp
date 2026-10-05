'use strict';

/**
 * Pure quoted-message extraction for the WhatsApp bridge.
 *
 * Authority never comes from these fields.  The authenticated current sender
 * plus an explicit @Alex mention is what authorizes a Family Shared handoff.
 * This module only preserves the text/type WhatsApp already embeds in the
 * reply's contextInfo so Python does not have to look up an ordinary group
 * message that was intentionally never stored.
 */

function unwrapQuotedMessage(message) {
  let m = message || null;
  if (!m) return null;
  if (m.ephemeralMessage && m.ephemeralMessage.message) m = m.ephemeralMessage.message;
  if (m.viewOnceMessageV2 && m.viewOnceMessageV2.message) m = m.viewOnceMessageV2.message;
  if (m.viewOnceMessage && m.viewOnceMessage.message) m = m.viewOnceMessage.message;
  return m;
}

export function quotedHandoffFromContext(contextInfo = null) {
  const ctx = contextInfo || {};
  const q = unwrapQuotedMessage(ctx.quotedMessage);
  const base = {
    quoted_text: '',
    quoted_type: null,
    quoted_participant_jid: String(
      ctx.participantAlt || ctx.participant || ctx.remoteJidAlt || ctx.remoteJid || ''
    ),
    quoted_participant_alt_jid: String(
      ctx.participantAlt || ctx.remoteJidAlt || ''
    ),
  };
  if (!q) return base;

  if (q.conversation) {
    return { ...base, quoted_text: String(q.conversation), quoted_type: 'text' };
  }
  if (q.extendedTextMessage && q.extendedTextMessage.text) {
    return {
      ...base,
      quoted_text: String(q.extendedTextMessage.text),
      quoted_type: 'text',
    };
  }
  if (q.imageMessage) {
    return { ...base, quoted_type: 'image' };
  }
  if (q.audioMessage) {
    return { ...base, quoted_type: 'audio' };
  }
  if (q.documentMessage) {
    const mime = String(q.documentMessage.mimetype || '').toLowerCase();
    return { ...base, quoted_type: mime === 'application/pdf' ? 'pdf' : 'other' };
  }
  return { ...base, quoted_type: 'other' };
}
