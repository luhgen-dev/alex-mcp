'use strict';

export function reactionSenderCandidates(reactionKey = {}) {
  const values = [
    reactionKey.participantAlt,
    reactionKey.remoteJidAlt,
    reactionKey.participant,
    reactionKey.remoteJid,
  ];
  const seen = new Set();
  const out = [];
  for (const value of values) {
    const jid = String(value || '');
    if (!jid || jid.endsWith('@g.us') || seen.has(jid)) continue;
    seen.add(jid);
    out.push(jid);
  }
  return out;
}

export function reactionConversationJid(isGroup, targetRemoteJid, resolvedSenderJid) {
  if (isGroup) return String(targetRemoteJid || '');
  return String(resolvedSenderJid || targetRemoteJid || '');
}
