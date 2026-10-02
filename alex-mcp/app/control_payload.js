'use strict';

export function buildControlMessage(payload, to) {
  const targetKey = {
    remoteJid: to,
    id: String(payload.target_message_id || ''),
    fromMe: Boolean(payload.target_from_me),
  };
  if (!targetKey.id) throw new Error('Missing target message id');
  if (payload.target_participant_jid) {
    targetKey.participant = String(payload.target_participant_jid);
  }

  if (payload.kind === 'reaction') {
    return {
      targetKey,
      content: {
        react: { text: String(payload.emoji || ''), key: targetKey },
      },
    };
  }
  if (payload.kind === 'pin') {
    return {
      targetKey,
      content: { pin: targetKey, type: 1, time: 2592000 },
    };
  }
  if (payload.kind === 'unpin') {
    return {
      targetKey,
      content: { pin: targetKey, type: 2 },
    };
  }
  throw new Error('Unsupported control kind');
}
