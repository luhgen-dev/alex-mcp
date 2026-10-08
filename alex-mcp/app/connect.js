'use strict';

import fs from 'fs';
import http from 'http';
import path from 'path';
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import pino from 'pino';
import QRCode from 'qrcode';
import { buildControlMessage } from './control_payload.js';
import { reactionConversationJid, reactionSenderCandidates } from './reaction_payload.js';
import { quotedHandoffFromContext } from './quoted_payload.js';

const DATA_DIR = process.env.ALEX_DATA_DIR || '/data';
const AUTH_DIR = path.join(DATA_DIR, 'whatsapp_auth');
const OPTIONS_FILE = path.join(DATA_DIR, 'options.json');
const SELFTEST_FILE = path.join(DATA_DIR, 'selftest.json');
const GROUP_FILE = path.join(DATA_DIR, 'family_group.json');
const RUNTIME_STATUS_FILE = path.join(DATA_DIR, 'runtime_status.json');
const INGRESS_URL = 'http://127.0.0.1:5001/ingress';
const PROVIDER_PROBE_URL = 'http://127.0.0.1:5001/provider-probe';
const USAGE_URL = 'http://127.0.0.1:5001/usage-summary';
const CERT_STATUS_URL = 'http://127.0.0.1:5001/certification-status';
const CERT_RUN_URL = 'http://127.0.0.1:5001/certification-live';
const CERT_REPORT_URL = 'http://127.0.0.1:5001/certification-report';
// v0.5.33 ChatGPT plan sign-in + shadow router report (local Python only).
const CHATGPT_STATUS_URL = 'http://127.0.0.1:5001/chatgpt-status';
const SHADOW_ROUTER_URL = 'http://127.0.0.1:5001/shadow-router-summary';
const CHATGPT_POST_URLS = {
  '/chatgpt-start': 'http://127.0.0.1:5001/chatgpt-signin-start',
  '/chatgpt-finish': 'http://127.0.0.1:5001/chatgpt-signin-finish',
  '/chatgpt-test': 'http://127.0.0.1:5001/chatgpt-test',
  '/chatgpt-signout': 'http://127.0.0.1:5001/chatgpt-signout',
};
const EGRESS_PORT = 5002;
const UI_PORT = 8099;
// If a private-DM turn is genuinely slow (voice transcription, OCR, provider
// recovery, etc.), acknowledge it without interrupting the durable final reply.
// Group messages are excluded because an untracked interim reply would weaken
// quoted-message context binding there.
// 12s: ordinary turns (including one bounded semantic-gateway call plus the
// main model call) finish well inside this; only genuinely slow work acks.
const SLOW_ACK_MS = 12000;
const SLOW_ACK_TEXT = 'Sure, I’m working on that and will get it to you shortly…';
const logger = pino({ level: process.env.ALEX_LOG_LEVEL || 'silent' });

let currentSock = null;
let cachedVersion = null;
let starting = false;
const recentInboundMessages = new Map();
const INBOUND_QUOTE_TTL_MS = 2 * 60 * 60 * 1000;
const INBOUND_QUOTE_MAX = 500;

function inboundQuoteKey(jid, messageId) {
  return String(jid || '') + '|' + String(messageId || '');
}

function rememberInboundForReply(message) {
  const jid = message && message.key ? message.key.remoteJid : null;
  const id = message && message.key ? message.key.id : null;
  if (!jid || !id) return;
  const now = Date.now();
  recentInboundMessages.set(inboundQuoteKey(jid, id), { message, storedAt: now });
  for (const [key, entry] of recentInboundMessages) {
    if (now - entry.storedAt > INBOUND_QUOTE_TTL_MS) recentInboundMessages.delete(key);
  }
  while (recentInboundMessages.size > INBOUND_QUOTE_MAX) {
    const first = recentInboundMessages.keys().next().value;
    recentInboundMessages.delete(first);
  }
}

function rememberedInboundForReply(jid, messageId) {
  if (!jid || !messageId) return undefined;
  const key = inboundQuoteKey(jid, messageId);
  const entry = recentInboundMessages.get(key);
  if (!entry) return undefined;
  if (Date.now() - entry.storedAt > INBOUND_QUOTE_TTL_MS) {
    recentInboundMessages.delete(key);
    return undefined;
  }
  return entry.message;
}
let manualReset = false;
let pairing = {
  status: 'starting',
  qrDataUrl: null,
  linkedUser: null,
  lastError: null,
  updatedAt: new Date().toISOString(),
};

function touch(next) {
  pairing = Object.assign({}, pairing, next, { updatedAt: new Date().toISOString() });
}

function readOptions() {
  try {
    return JSON.parse(fs.readFileSync(OPTIONS_FILE, 'utf8'));
  } catch (_err) {
    return {};
  }
}

function cleanNumber(value) {
  return String(value || '').replace(/\D/g, '');
}

function allowedNumbers() {
  const opts = readOptions();
  return [cleanNumber(opts.husband_phone), cleanNumber(opts.wife_phone)].filter(Boolean);
}

function isWhitelisted(phone) {
  const clean = cleanNumber(phone);
  return clean.length > 0 && allowedNumbers().includes(clean);
}

function getFamilyGroupJid() {
  try {
    const data = JSON.parse(fs.readFileSync(GROUP_FILE, 'utf8'));
    return data.group_jid || null;
  } catch (_err) {
    return null;
  }
}

function setFamilyGroupJid(groupJid) {
  fs.writeFileSync(GROUP_FILE, JSON.stringify({
    group_jid: groupJid,
    paired_at: new Date().toISOString(),
  }, null, 2), 'utf8');
}

function unwrapMessage(message) {
  let m = message && message.message;
  if (!m) return null;
  if (m.ephemeralMessage && m.ephemeralMessage.message) m = m.ephemeralMessage.message;
  if (m.viewOnceMessageV2 && m.viewOnceMessageV2.message) m = m.viewOnceMessageV2.message;
  if (m.viewOnceMessage && m.viewOnceMessage.message) m = m.viewOnceMessage.message;
  return m;
}

function extractText(message) {
  const m = unwrapMessage(message);
  if (!m) return '';
  return (
    m.conversation ||
    (m.extendedTextMessage && m.extendedTextMessage.text) ||
    (m.imageMessage && m.imageMessage.caption) ||
    (m.documentMessage && m.documentMessage.caption) ||
    ''
  );
}

function extractContextInfo(message) {
  const m = unwrapMessage(message);
  if (!m) return null;
  return (
    (m.extendedTextMessage && m.extendedTextMessage.contextInfo) ||
    (m.imageMessage && m.imageMessage.contextInfo) ||
    (m.audioMessage && m.audioMessage.contextInfo) ||
    (m.documentMessage && m.documentMessage.contextInfo) ||
    null
  );
}

function extractQuotedId(message) {
  const ctx = extractContextInfo(message);
  return ctx && ctx.stanzaId ? ctx.stanzaId : null;
}

function jidUser(value) {
  return cleanNumber(String(value || '').split('@')[0].split(':')[0]);
}

// v0.4.4: WhatsApp groups increasingly identify people by LID
// ("12345@lid") instead of phone JIDs. Alex's own identity therefore has two
// forms, and a mention/reply may use either. This only affects the group WAKE
// gate; privacy spaces are still decided later by Python resolve_actor.
function selfIdentityUsers() {
  const ids = new Set();
  const user = currentSock && currentSock.user ? currentSock.user : null;
  if (!user) return ids;
  const pn = jidUser(user.id || '');
  const lid = jidUser(user.lid || '');
  if (pn) ids.add(pn);
  if (lid) ids.add(lid);
  return ids;
}

async function jidMatchesSelf(jid, selfIds) {
  if (!jid) return false;
  if (selfIds.has(jidUser(jid))) return true;
  if (String(jid).endsWith('@lid') && currentSock && currentSock.signalRepository
      && currentSock.signalRepository.lidMapping) {
    try {
      const pn = await currentSock.signalRepository.lidMapping.getPNForLID(jid);
      if (pn && selfIds.has(jidUser(pn))) return true;
    } catch (_err) {}
  }
  return false;
}

function messageTimestampMs(message) {
  const ts = message ? message.messageTimestamp : null;
  if (ts === null || ts === undefined) return null;
  let seconds = null;
  if (typeof ts === 'number') seconds = ts;
  else if (typeof ts === 'bigint') seconds = Number(ts);
  else if (typeof ts === 'object' && typeof ts.toNumber === 'function') seconds = ts.toNumber();
  else seconds = Number(ts);
  return Number.isFinite(seconds) && seconds > 0 ? Math.round(seconds * 1000) : null;
}

function maskId(value) {
  const text = jidUser(value);
  if (text.length <= 4) return '***';
  return text.slice(0, 2) + '***' + text.slice(-2) + (String(value).endsWith('@lid') ? '@lid' : '');
}

async function isAlexMentioned(message) {
  const ctx = extractContextInfo(message);
  const selfIds = selfIdentityUsers();
  if (!ctx || !selfIds.size || !Array.isArray(ctx.mentionedJid) || !ctx.mentionedJid.length) return false;
  for (const jid of ctx.mentionedJid) {
    if (await jidMatchesSelf(jid, selfIds)) return true;
  }
  // Masked evidence for diagnosing identity-format mismatches (no content).
  console.log('[Alex MCP] Group mention not matched: mentioned=' +
    ctx.mentionedJid.map(maskId).join(',') + ' self=' + Array.from(selfIds).map(maskId).join(','));
  return false;
}

async function isReplyToAlex(message) {
  const ctx = extractContextInfo(message);
  const selfIds = selfIdentityUsers();
  if (!ctx || !selfIds.size || !ctx.stanzaId || !ctx.quotedMessage) return false;
  return jidMatchesSelf(ctx.participant || ctx.remoteJid || '', selfIds);
}

async function stripAlexMentionText(message, text) {
  // The @Alex token is a wake signal, not semantic user content. Remove only
  // mentions that resolve to Alex itself; spouse/other-user mentions remain.
  const ctx = extractContextInfo(message);
  const selfIds = selfIdentityUsers();
  let value = String(text || '');
  if (!ctx || !selfIds.size || !Array.isArray(ctx.mentionedJid)) return value.trim();
  for (const jid of ctx.mentionedJid) {
    if (!(await jidMatchesSelf(jid, selfIds))) continue;
    const token = '@' + jidUser(jid);
    if (token.length > 1) value = value.split(token).join(' ');
  }
  return value.replace(/\s{2,}/g, ' ').trim();
}

function detectMedia(message) {
  const m = unwrapMessage(message);
  if (!m) return { type: null, node: null };
  if (m.imageMessage) return { type: 'image', node: m.imageMessage };
  if (m.audioMessage) return { type: 'audio', node: m.audioMessage };
  if (m.documentMessage && m.documentMessage.mimetype === 'application/pdf') {
    return { type: 'pdf', node: m.documentMessage };
  }
  return { type: null, node: null };
}

async function downloadMedia(message, mediaType) {
  try {
    const buffer = await downloadMediaMessage(
      message,
      'buffer',
      {},
      { logger: logger, reuploadRequest: currentSock ? currentSock.updateMediaMessage : undefined }
    );
    const m = unwrapMessage(message);
    let mime = 'application/octet-stream';
    if (mediaType === 'image') mime = (m.imageMessage && m.imageMessage.mimetype) || 'image/jpeg';
    if (mediaType === 'audio') mime = (m.audioMessage && m.audioMessage.mimetype) || 'audio/ogg';
    if (mediaType === 'pdf') mime = 'application/pdf';
    return { data: buffer.toString('base64'), mimeType: mime };
  } catch (err) {
    console.error('[Alex MCP] Media download failed:', err.message);
    return null;
  }
}

async function resolveSenderJid(message, remoteJid) {
  let jid = remoteJid;
  if (jid.endsWith('@lid') && currentSock && currentSock.signalRepository && currentSock.signalRepository.lidMapping) {
    try {
      const pn = await currentSock.signalRepository.lidMapping.getPNForLID(jid);
      if (pn) jid = pn;
    } catch (_err) {}
  }
  return jid;
}

async function forwardToPython(payload) {
  let lastError = null;
  for (let attempt = 1; attempt <= 4; attempt += 1) {
    try {
      const response = await fetch(INGRESS_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (response.ok) return;
      const body = await response.text();
      lastError = new Error('Python ingress returned ' + response.status + ': ' + body.slice(0, 300));
    } catch (err) {
      lastError = err;
    }
    await new Promise(resolve => setTimeout(resolve, attempt * 400));
  }
  throw lastError || new Error('Python ingress unavailable');
}

async function forwardReactionEvent(targetKey, reaction) {
  const remoteJid = (targetKey && targetKey.remoteJid) || '';
  if (!remoteJid) return;
  const isGroup = remoteJid.endsWith('@g.us');
  if (isGroup) {
    const familyGroup = getFamilyGroupJid();
    if (!familyGroup || familyGroup !== remoteJid) return;
  }

  const reactionKey = reaction && reaction.key ? reaction.key : {};
  if (reactionKey.fromMe) return;
  let rawSenderJid = '';
  let senderJid = '';
  let senderPhone = '';
  const candidates = reactionSenderCandidates(reactionKey);
  for (const candidate of candidates) {
    const resolved = await resolveSenderJid(null, candidate);
    const phone = cleanNumber(resolved.split('@')[0].split(':')[0]);
    if (phone && isWhitelisted(phone)) {
      rawSenderJid = candidate;
      senderJid = resolved;
      senderPhone = phone;
      break;
    }
  }
  if (!senderPhone) {
    console.log('[Alex MCP] Reaction sender did not resolve: ' + candidates.map(maskId).join(','));
    return;
  }

  const targetMessageId = targetKey && targetKey.id ? String(targetKey.id) : '';
  if (!targetMessageId) return;
  const reactionText = String((reaction && reaction.text) || '');
  const eventId = String(
    reactionKey.id ||
    ('REACTION:' + targetMessageId + ':' + senderPhone + ':' +
      String((reaction && reaction.senderTimestampMs) || '0') + ':' + reactionText)
  );

  await forwardToPython({
    event_kind: 'REACTION',
    message_id: eventId,
    provider: 'WHATSAPP',
    conversation_id: reactionConversationJid(isGroup, remoteJid, senderJid),
    conversation_type: isGroup ? 'GROUP' : 'DIRECT_DM',
    sender_phone: '+' + senderPhone,
    sender_provider_jid: rawSenderJid,
    text: '',
    reaction_target_message_id: targetMessageId,
    reaction_text: reactionText,
    reaction_removed: !reactionText,
    sent_at_ms: reaction && reaction.senderTimestampMs
      ? Number(reaction.senderTimestampMs)
      : Date.now(),
  });
}


async function handleIncoming(message) {
  if (!message || !message.key || message.key.fromMe) return;
  const remoteJid = message.key.remoteJid || '';
  if (!remoteJid) return;
  const isGroup = remoteJid.endsWith('@g.us');

  let rawSenderJid = isGroup ? (message.key.participant || '') : remoteJid;
  if (!rawSenderJid) return;
  const senderJid = await resolveSenderJid(message, rawSenderJid);
  const senderPhone = cleanNumber(senderJid.split('@')[0].split(':')[0]);
  if (!isWhitelisted(senderPhone)) return;

  let rawText = extractText(message).trim();
  let alexMentioned = false;
  let replyToAlex = false;

  if (isGroup && /^alex\s+set\s+family\s+group$/i.test(rawText)) {
    setFamilyGroupJid(remoteJid);
    if (currentSock) {
      await currentSock.sendMessage(remoteJid, {
        text: '✅ This is now Alex’s Family Shared group. Private DM data will stay private unless you explicitly share it.'
      });
    }
    return;
  }

  if (isGroup) {
    const familyGroup = getFamilyGroupJid();
    if (!familyGroup || familyGroup !== remoteJid) return;
    // Family Shared is intentionally opt-in per message: Alex responds only
    // when explicitly @mentioned or when someone swipe-replies to Alex.
    alexMentioned = await isAlexMentioned(message);
    replyToAlex = await isReplyToAlex(message);
    if (!alexMentioned && !replyToAlex) return;
    rawText = await stripAlexMentionText(message, rawText);
  }

  const media = detectMedia(message);
  const quotedMessageId = extractQuotedId(message);
  const quoteContext = extractContextInfo(message);
  let quotedHandoff = null;
  // Only an explicit @Alex mention may hand an ordinary Family Shared message
  // to Alex. Replying to Alex itself continues to use durable object binding
  // from outbound_messages; do not replace that exact context with client text.
  if (
    isGroup && alexMentioned && quotedMessageId && !replyToAlex
    && quoteContext && quoteContext.quotedMessage
  ) {
    quotedHandoff = quotedHandoffFromContext(quoteContext);
    if (quotedHandoff.quoted_participant_jid) {
      const resolvedQuotedJid = await resolveSenderJid(
        null, quotedHandoff.quoted_participant_jid
      );
      quotedHandoff.quoted_participant_phone = cleanNumber(
        String(resolvedQuotedJid || '').split('@')[0].split(':')[0]
      );
    } else {
      quotedHandoff.quoted_participant_phone = '';
    }
  }
  // A mention-only swipe reply is a valid handoff: "@Alex" means "act on the
  // message I am replying to". Preserve that quoted id even after stripping
  // the mention leaves no visible command text.
  const mentionOnlyQuotedContext = Boolean(
    isGroup && alexMentioned && quotedMessageId
  );
  if (!rawText && !media.type && !mentionOnlyQuotedContext) return;

  // Keep the exact inbound WAMessage briefly so the durable final response can
  // render as a real WhatsApp reply to the user's original message. If Alex is
  // restarted before delivery, the response still sends normally without the
  // quote rather than fabricating a partial WAMessage.
  rememberInboundForReply(message);

  try {
    if (currentSock) await currentSock.sendPresenceUpdate('composing', remoteJid);
  } catch (_err) {}

  let mediaData = null;
  if (media.type) mediaData = await downloadMedia(message, media.type);

  const payload = {
    message_id: message.key.id,
    provider: 'WHATSAPP',
    conversation_id: remoteJid,
    conversation_type: isGroup ? 'GROUP' : 'DIRECT_DM',
    sender_phone: '+' + senderPhone,
    // Preserve the provider participant key for durable react/pin cleanup after
    // a Python or Node restart. This is transport metadata, never identity/ACL.
    sender_provider_jid: isGroup ? (message.key.participant || rawSenderJid) : remoteJid,
    text: rawText,
    quoted_message_id: quotedMessageId,
    quoted_text: quotedHandoff ? quotedHandoff.quoted_text : '',
    quoted_type: quotedHandoff ? quotedHandoff.quoted_type : null,
    quoted_participant_jid: quotedHandoff
      ? quotedHandoff.quoted_participant_jid : null,
    quoted_participant_alt_jid: quotedHandoff
      ? quotedHandoff.quoted_participant_alt_jid : null,
    quoted_participant_phone: quotedHandoff
      ? quotedHandoff.quoted_participant_phone : null,
    remote_jid_alt: message.key.remoteJidAlt || null,
    participant_alt: message.key.participantAlt || null,
    alex_mentioned: alexMentioned,
    reply_to_alex: replyToAlex,
    sent_at_ms: messageTimestampMs(message),
    image_data: media.type === 'image' && mediaData ? mediaData.data : null,
    image_mime_type: media.type === 'image' && mediaData ? mediaData.mimeType : null,
    audio_data: media.type === 'audio' && mediaData ? mediaData.data : null,
    audio_mime_type: media.type === 'audio' && mediaData ? mediaData.mimeType : null,
    pdf_data: media.type === 'pdf' && mediaData ? mediaData.data : null,
    media_failed: Boolean(media.type && !mediaData),
    media_failed_type: media.type || null,
  };

  let slowAckTimer = null;
  if (!isGroup) {
    slowAckTimer = setTimeout(async function() {
      try {
        if (currentSock) {
          await currentSock.sendMessage(
            remoteJid,
            { text: SLOW_ACK_TEXT },
            { quoted: message }
          );
          // Keep the visible working state after the interim acknowledgement.
          try { await currentSock.sendPresenceUpdate('composing', remoteJid); } catch (_err) {}
        }
      } catch (err) {
        console.error('[Alex MCP] Slow acknowledgement failed:', err.message);
      }
    }, SLOW_ACK_MS);
  }

  try {
    await forwardToPython(payload);
  } catch (err) {
    console.error('[Alex MCP] Ingress forwarding failed:', err.message);
    try {
      if (currentSock) {
        await currentSock.sendMessage(remoteJid, { text: 'Alex is temporarily unavailable. Please try that message once more.' });
      }
    } catch (_err) {}
  } finally {
    if (slowAckTimer) clearTimeout(slowAckTimer);
  }
}

async function startWhatsApp() {
  if (starting) return;
  starting = true;
  try {
    fs.mkdirSync(AUTH_DIR, { recursive: true });
    const auth = await useMultiFileAuthState(AUTH_DIR);
    if (!cachedVersion) {
      const latest = await fetchLatestBaileysVersion();
      cachedVersion = latest.version;
    }

    touch({ status: 'connecting', qrDataUrl: null, lastError: null });
    const sock = makeWASocket({
      version: cachedVersion,
      auth: auth.state,
      logger: logger,
      printQRInTerminal: false,
    });
    currentSock = sock;
    sock.ev.on('creds.update', auth.saveCreds);

    sock.ev.on('connection.update', async function(update) {
      const connection = update.connection;
      const qr = update.qr;
      const lastDisconnect = update.lastDisconnect;

      if (qr) {
        try {
          const dataUrl = await QRCode.toDataURL(qr, { width: 360, margin: 2 });
          touch({ status: 'waiting_for_scan', qrDataUrl: dataUrl, lastError: null });
          console.log('[Alex MCP] WhatsApp QR is ready in the app Web UI.');
        } catch (err) {
          touch({ status: 'pairing_error', lastError: err.message });
        }
      }

      if (connection === 'open') {
        touch({
          status: 'connected',
          qrDataUrl: null,
          linkedUser: sock.user && sock.user.id ? sock.user.id : null,
          lastError: null,
        });
        console.log('[Alex MCP] WhatsApp connected.');
      }

      if (connection === 'close') {
        if (currentSock === sock) currentSock = null;
        const code = lastDisconnect && lastDisconnect.error && lastDisconnect.error.output
          ? lastDisconnect.error.output.statusCode
          : null;
        const loggedOut = code === DisconnectReason.loggedOut;
        touch({
          status: loggedOut ? 'not_paired' : 'reconnecting',
          qrDataUrl: null,
          linkedUser: loggedOut ? null : pairing.linkedUser,
          lastError: lastDisconnect && lastDisconnect.error ? String(lastDisconnect.error).slice(0, 300) : null,
        });
        if (!manualReset) {
          setTimeout(async function() {
            if (loggedOut) {
              try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch (_err) {}
            }
            starting = false;
            await startWhatsApp();
          }, 1500);
        }
      }
    });

    sock.ev.on('messages.upsert', async function(event) {
      if (event.type !== 'notify') return;
      for (const message of event.messages || []) {
        try {
          await handleIncoming(message);
        } catch (err) {
          console.error('[Alex MCP] Incoming message error:', err.message);
        }
      }
    });

    // Baileys emits reaction updates separately from ordinary messages.
    // A reaction is a deterministic household signal, so it bypasses the
    // @mention wake gate but still requires the paired Family Shared group
    // and an authorized household sender.
    sock.ev.on('messages.reaction', async function(events) {
      for (const entry of events || []) {
        try {
          await forwardReactionEvent(entry.key, entry.reaction);
        } catch (err) {
          console.error('[Alex MCP] Incoming reaction error:', err.message);
        }
      }
    });
  } catch (err) {
    currentSock = null;
    touch({ status: 'error', lastError: err.message });
    console.error('[Alex MCP] WhatsApp startup failed:', err.message);
    setTimeout(async function() {
      starting = false;
      await startWhatsApp();
    }, 5000);
  } finally {
    if (currentSock) starting = false;
  }
}

async function resetPairing() {
  manualReset = true;
  touch({ status: 'resetting', qrDataUrl: null, linkedUser: null, lastError: null });
  try {
    if (currentSock) {
      try { currentSock.end(new Error('Pairing reset requested')); } catch (_err) {}
      currentSock = null;
    }
    fs.rmSync(AUTH_DIR, { recursive: true, force: true });
  } catch (_err) {}
  starting = false;
  manualReset = false;
  await startWhatsApp();
}

function readSelftest() {
  try {
    return JSON.parse(fs.readFileSync(SELFTEST_FILE, 'utf8'));
  } catch (_err) {
    return null;
  }
}

function readRuntimeStatus() {
  try {
    return JSON.parse(fs.readFileSync(RUNTIME_STATUS_FILE, 'utf8'));
  } catch (_err) {
    return {};
  }
}

function safeStatus() {
  const opts = readOptions();
  const provider = opts.ai_provider || 'auto';
  let keyPresent = false;
  if (provider === 'auto') keyPresent = Boolean(opts.gemini_api_key || opts.xai_api_key || opts.openai_api_key);
  if (provider === 'grok') keyPresent = Boolean(opts.xai_api_key);
  if (provider === 'gemini') keyPresent = Boolean(opts.gemini_api_key);
  if (provider === 'openai') keyPresent = Boolean(opts.openai_api_key);
  return {
    whatsapp: {
      status: pairing.status,
      qr_data_url: pairing.qrDataUrl,
      linked_user: pairing.linkedUser,
      last_error: pairing.lastError,
      updated_at: pairing.updatedAt,
    },
    setup: {
      configured_numbers: allowedNumbers().length,
      ai_provider: provider,
      api_key_present: keyPresent,
      family_group_paired: Boolean(getFamilyGroupJid()),
      reasoning_effort: opts.reasoning_effort || 'low',
      gemini_ready: Boolean(opts.gemini_api_key),
      grok_ready: Boolean(opts.xai_api_key),
      openai_ready: Boolean(opts.openai_api_key),
      auto_route: provider === 'auto'
        ? 'Gemini Flash-Lite → Gemini 3.8 Flash → Grok → OpenAI'
        : null,
      auto_grok_fallback_budget_usd: Number(opts.auto_grok_fallback_budget_usd ?? 0.5),
    },
    selftest: readSelftest(),
    runtime: readRuntimeStatus(),
  };
}

const UI_HTML = [
'<!doctype html>',
'<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
'<title>Alex MCP</title>',
'<style>',
'body{font-family:system-ui,-apple-system,sans-serif;background:#111;color:#eee;margin:0;padding:24px}main{max-width:680px;margin:auto}',
'.card{background:#1c1c1e;border:1px solid #343438;border-radius:18px;padding:20px;margin:14px 0}',
'h1{font-size:28px;margin:0 0 6px}.muted{color:#aaa}.ok{color:#63d38b}.warn{color:#ffcc66}.bad{color:#ff7b7b}',
'#qr{max-width:320px;width:100%;background:white;border-radius:12px;padding:8px;margin-top:12px}',
'button{background:#2f80ed;color:white;border:0;border-radius:12px;padding:12px 16px;font-weight:700}code{word-break:break-all;color:#b8d8ff}',
'</style></head><body><main>',
'<h1>Alex MCP</h1><div class="muted">Setup and WhatsApp pairing</div>',
'<div class="card"><h2>WhatsApp</h2><div id="wa">Checking…</div><img id="qr" style="display:none"><p class="muted">On your phone: WhatsApp → Linked devices → Link a device, then scan the QR.</p><button onclick="resetPairing()">Reset / pair again</button></div>',
'<div class="card"><h2>Configuration</h2><div id="cfg">Checking…</div><p class="muted">API keys and phone numbers are entered in the Home Assistant Configuration tab. To bind the shared family group, send <code>alex set family group</code> once from that group. No source-code editing is required.</p></div>',
'<div class="card"><h2>AI connection</h2><div id="ai">Not tested yet</div><p><button onclick="testAi()">Test AI now</button></p></div>',
'<div class="card"><h2>ChatGPT plan</h2><div id="gpt">Checking…</div>',
'<div id="gptSignin" style="display:none;margin-top:12px"><p class="muted">1. Tap the link below and sign in to ChatGPT. Allow Alex to use your plan.<br>2. Your browser then shows a page that cannot load (127.0.0.1). That is expected.<br>3. Copy the whole address from the address bar, paste it here and tap Finish. The link works for 15 minutes.</p>',
'<p><a id="gptLink" href="#" target="_blank" rel="noopener noreferrer" style="color:#8ab4f8">Open ChatGPT sign-in</a></p>',
'<textarea id="gptPaste" rows="3" style="width:100%;box-sizing:border-box;background:#111;color:#eee;border:1px solid #444;border-radius:10px;padding:8px" placeholder="http://127.0.0.1:1455/auth/callback?code=…"></textarea>',
'<p><button onclick="gptFinish()">Finish sign-in</button></p></div>',
'<div id="gptMsg" class="muted"></div>',
'<p><button onclick="gptStart()">Start sign-in</button> <button onclick="gptTest()">Test ChatGPT</button> <button onclick="gptSignout()" style="background:#444">Sign out</button></p>',
'<p class="muted">Off by default. Choose the mode in Configuration → ChatGPT plan mode: <b>shadow_only</b> only compares tool choices below (no reply changes); <b>primary</b> lets ChatGPT answer text messages first, with your other providers as fallback.</p></div>',
'<div class="card"><h2>AI router comparison — last 7 days</h2><div id="shadow">Checking…</div><p class="muted">Log only. Compares Alex\'s keyword tool routing with an AI choosing from all tools, using your ChatGPT plan. It never changes a reply.</p></div>',
'<div class="card"><h2>AI usage — last 24h</h2><div id="usage">Checking…</div><p class="muted">Local telemetry only. Refreshing this card does not call the AI provider.</p></div>',
'<div class="card"><h2>Live certification</h2><div id="cert">Checking…</div><p><button id="certBtn" onclick="runCert()">Run live benchmark — max $3</button></p><p class="muted">Uses Alex\'s configured API keys, but all household state is synthetic and Home Assistant is mocked. No WhatsApp messages are sent.</p><p id="certReport" style="display:none"><a href="./certification-report" target="_blank" style="color:#8ab4f8">Open full benchmark report</a></p></div>',
'<div class="card"><h2>Core diagnostics</h2><div id="diag">Checking…</div></div>',
'<script>',
'async function load(){try{const r=await fetch("./status");const s=await r.json();',
'const w=s.whatsapp||{};const q=document.getElementById("qr");',
'document.getElementById("wa").innerHTML="<b>Status:</b> "+esc(w.status||"unknown")+(w.linked_user?"<br><span class=ok>Connected as "+esc(w.linked_user)+"</span>":"")+(w.last_error?"<br><span class=bad>"+esc(w.last_error)+"</span>":"");',
'if(w.qr_data_url){q.src=w.qr_data_url;q.style.display="block"}else{q.style.display="none"}',
'const c=s.setup||{};let route=c.auto_route?"<br><b>Route:</b> "+esc(c.auto_route):"";let keys=c.ai_provider==="auto"?"<br><b>Ready:</b> Gemini "+(c.gemini_ready?"✓":"—")+" · Grok "+(c.grok_ready?"✓":"—")+" · OpenAI "+(c.openai_ready?"✓":"—")+"<br><b>Auto Grok cap:</b> $"+esc(Number(c.auto_grok_fallback_budget_usd||0).toFixed(2))+"/month":"";document.getElementById("cfg").innerHTML="<b>AI mode:</b> "+esc(c.ai_provider||"")+(c.api_key_present?" <span class=ok>✓</span>":" <span class=warn>— API key not set</span>")+route+keys+"<br><b>Reasoning:</b> "+esc(c.reasoning_effort||"low")+"<br><b>Household numbers configured:</b> "+esc(String(c.configured_numbers||0))+"/2<br><b>Family group:</b> "+(c.family_group_paired?"<span class=ok>paired ✓</span>":"<span class=warn>not paired</span>");',
'const rt=s.runtime||{};const p=rt.provider_probe||{};let ai="Not tested yet";if(p.status==="ok"){let role=p.route_role?" · "+esc(p.route_role):"";ai="<span class=ok>✓ "+esc(p.provider)+" / "+esc(p.model)+" responding</span>"+role+"<br><span class=muted>"+esc(String(p.latency_ms||0))+" ms</span>"}else if(p.status==="error"){ai="<span class=bad>✗ "+esc(p.category||"provider error")+"</span><br><span class=muted>"+esc(p.message||"")+"</span>"}document.getElementById("ai").innerHTML=ai;',
'const d=s.selftest;if(d){document.getElementById("diag").innerHTML="<span class="+(d.failed===0?"ok":"bad")+">"+d.passed+" passed, "+d.failed+" failed</span>"}else{document.getElementById("diag").textContent="Not run yet"}',
'}catch(e){document.getElementById("wa").textContent="Status unavailable: "+e}}',
'async function loadUsage(){try{const r=await fetch("./usage");const u=await r.json();const cost=Number(u.estimated_ai_cost_usd||0).toFixed(6);const rows=(u.by_provider||[]).map(x=>"<br><span class=muted>"+esc(x.provider)+"/"+esc(x.model)+": "+esc(x.model_calls)+" calls · $"+esc(Number(x.cost_usd||0).toFixed(6))+"</span>").join("");document.getElementById("usage").innerHTML="<b>Interactions:</b> "+esc(u.interactions||0)+" &nbsp; <b>Model calls:</b> "+esc(u.model_calls||0)+"<br><b>Input:</b> "+esc(u.input_tokens||0)+" &nbsp; <b>Cached:</b> "+esc(u.cached_input_tokens||0)+" ("+esc(u.cache_ratio_pct||0)+"%)<br><b>Output:</b> "+esc(u.output_tokens||0)+" &nbsp; <b>Reasoning:</b> "+esc(u.reasoning_tokens||0)+"<br><b>Average latency:</b> "+esc(u.average_ai_latency_ms||0)+" ms<br><b>Total API cost:</b> $"+esc(cost)+rows}catch(e){document.getElementById("usage").textContent="Usage unavailable: "+e}}',
'async function loadCert(){const el=document.getElementById("cert");const btn=document.getElementById("certBtn");const report=document.getElementById("certReport");try{const r=await fetch("./certification");const s=await r.json();const state=s.state||"idle";btn.disabled=state==="running";if(state==="running"){el.innerHTML="<span class=warn>Running…</span><br><span class=muted>Budget cap: $"+esc(Number(s.budget_usd||3).toFixed(2))+"</span>";report.style.display="none";return}if(state==="completed"){const q=s.summary||{};const cost=Number(q.estimated_cost_usd||0).toFixed(6);el.innerHTML="<b>Result:</b> <span class="+(s.result_status==="PASS"?"ok":"bad")+">"+esc(s.result_status||"unknown")+"</span><br><b>Prompt runs:</b> "+esc(q.prompt_runs||0)+" · <b>Conversations:</b> "+esc(q.conversation_runs||0)+"<br><b>Failures:</b> "+esc(q.failures||0)+" · <b>Cost:</b> $"+esc(cost)+"<br><span class=muted>Offline failures known before spend: "+esc(q.offline_failures??"—")+" · paid contracts skipped: "+esc(q.skipped_paid_contracts||0)+"</span>";report.style.display=s.report_available?"block":"none";return}if(state==="error"){el.innerHTML="<span class=bad>Benchmark ended with an internal error.</span><br><span class=muted>"+esc(s.message||"Check add-on logs.")+"</span>";report.style.display=s.report_available?"block":"none";return}el.innerHTML="Ready. This will run the cost-aware real-provider benchmark in a disposable sandbox.";report.style.display=s.report_available?"block":"none"}catch(e){el.textContent="Certification status unavailable: "+e}}',
'async function runCert(){if(!confirm("Run the real-provider Alex benchmark now? Maximum configured test spend is about $3. The test uses synthetic household state and will not send WhatsApp messages or control real Home Assistant devices."))return;const btn=document.getElementById("certBtn");btn.disabled=true;document.getElementById("cert").innerHTML="<span class=warn>Starting…</span>";try{const r=await fetch("./certify-live",{method:"POST"});const s=await r.json();if(!r.ok)throw new Error(s.error||"Unable to start benchmark")}catch(e){document.getElementById("cert").innerHTML="<span class=bad>"+esc(e.message||e)+"</span>"}setTimeout(loadCert,700)}',
'function esc(x){const e=document.createElement("div");e.textContent=String(x);return e.innerHTML}',
'async function testAi(){const el=document.getElementById("ai");el.textContent="Testing…";try{await fetch("./test-ai",{method:"POST"});}catch(e){}setTimeout(load,500)}',
'async function resetPairing(){if(!confirm("Reset WhatsApp pairing and generate a new QR?"))return;await fetch("./reset",{method:"POST"});setTimeout(load,800)}',
'async function gptPost(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});let s={};try{s=await r.json()}catch(e){}return {ok:r.ok,s:s}}',
'async function loadGpt(){const el=document.getElementById("gpt");try{const r=await fetch("./chatgpt-state");const s=await r.json();let h="<b>Mode:</b> "+esc(s.mode||"off");if(s.signed_in){h+="<br><span class=ok>Signed in"+(s.account_email?" as "+esc(s.account_email):"")+" ✓</span>";h+="<br><b>Model:</b> "+esc(s.model_in_use||"—")}else if(s.needs_sign_in){h+="<br><span class=warn>Sign-in expired — please sign in again</span>"}else{h+="<br><span class=muted>Not signed in</span>"}if(s.models&&s.models.length){h+="<br><span class=muted>Available: "+esc(s.models.slice(0,8).join(", "))+"</span>"}const cd=(s.cooldowns||{}).chatgpt;if(cd){h+="<br><span class=warn>Paused "+esc(cd.seconds_left)+"s after "+esc(cd.category)+"</span>"}el.innerHTML=h}catch(e){el.textContent="Status unavailable: "+e}}',
'async function gptStart(){const m=document.getElementById("gptMsg");m.textContent="Preparing…";try{const x=await gptPost("./chatgpt-start");if(!x.ok||!x.s.authorize_url)throw new Error(x.s.error||"Could not start sign-in");document.getElementById("gptLink").href=x.s.authorize_url;document.getElementById("gptSignin").style.display="block";document.getElementById("gptPaste").value="";m.textContent=""}catch(e){m.innerHTML="<span class=bad>"+esc(e.message||e)+"</span>"}}',
'async function gptFinish(){const m=document.getElementById("gptMsg");const v=document.getElementById("gptPaste").value.trim();if(!v){m.innerHTML="<span class=warn>Paste the address first.</span>";return}m.textContent="Finishing…";try{const x=await gptPost("./chatgpt-finish",{redirect:v});if(!x.ok)throw new Error(x.s.error||"Sign-in failed");document.getElementById("gptSignin").style.display="none";document.getElementById("gptPaste").value="";m.innerHTML="<span class=ok>Signed in. Tap Test ChatGPT to check it.</span>";loadGpt()}catch(e){m.innerHTML="<span class=bad>"+esc(e.message||e)+"</span>"}}',
'async function gptTest(){const m=document.getElementById("gptMsg");m.textContent="Testing…";try{const x=await gptPost("./chatgpt-test");if(x.s.status==="ok"){m.innerHTML="<span class=ok>✓ "+esc(x.s.model)+" responding · "+esc(x.s.latency_ms)+" ms</span>"}else{m.innerHTML="<span class=bad>✗ "+esc(x.s.message||x.s.code||"failed")+"</span>"}}catch(e){m.innerHTML="<span class=bad>"+esc(e.message||e)+"</span>"}loadGpt()}',
'async function gptSignout(){if(!confirm("Sign Alex out of ChatGPT on this box? Your other AI providers keep working."))return;await gptPost("./chatgpt-signout");document.getElementById("gptMsg").textContent="Signed out.";loadGpt()}',
'async function loadShadow(){const el=document.getElementById("shadow");try{const r=await fetch("./shadow-router");const s=await r.json();if(!s.compared&&!s.errors){el.innerHTML=s.enabled?"Waiting for the next messages…":"<span class=muted>Off. Set ChatGPT plan mode to shadow_only or primary and sign in.</span>";return}let h="<b>Compared:</b> "+esc(s.compared)+(s.errors?" · <span class=warn>errors: "+esc(s.errors)+"</span>":"")+"<br><b>AI router also picked the tools Alex used:</b> "+esc(s.ai_router_also_picked_what_alex_used)+"/"+esc(s.turns_where_alex_used_tools)+"<br><b>AI router wanted a tool the keywords hid:</b> "+esc(s.ai_router_wanted_a_tool_keywords_hid)+"<br><b>Alex answered without tools, AI router picked some:</b> "+esc(s.alex_answered_without_tools_but_ai_router_picked_some)+(s.median_latency_ms!=null?"<br><span class=muted>Median router time: "+esc(s.median_latency_ms)+" ms</span>":"");(s.recent_disagreements||[]).forEach(function(d){h+="<hr style=\\"border-color:#333\\"><span class=muted>"+esc(d.message)+"</span><br>Keywords showed: "+esc((d.keyword_tools||[]).join(", ")||"none")+"<br>Alex used: "+esc((d.alex_used||[]).join(", ")||"none")+"<br>AI router: "+esc((d.ai_router_picked||[]).join(", ")||"none")});el.innerHTML=h}catch(e){el.textContent="Comparison unavailable: "+e}}',
'load();loadUsage();loadCert();loadGpt();loadShadow();setInterval(load,2000);setInterval(loadUsage,10000);setInterval(loadCert,3000);setInterval(loadGpt,10000);setInterval(loadShadow,30000);',
'</script></main></body></html>'
].join('');

function readSmallBody(req, limit) {
  return new Promise(function(resolve, reject) {
    let size = 0;
    const chunks = [];
    req.on('data', function(chunk) {
      size += chunk.length;
      if (size > limit) {
        reject(new Error('body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', function() { resolve(Buffer.concat(chunks).toString('utf8')); });
    req.on('error', reject);
  });
}

function ingressAllowed(req) {
  const ip = String(req.socket.remoteAddress || '');
  return ip.endsWith('172.30.32.2') || ip === '127.0.0.1' || ip === '::1';
}

function startPairingUi() {
  const server = http.createServer(async function(req, res) {
    if (!ingressAllowed(req)) {
      res.writeHead(403);
      res.end('Forbidden');
      return;
    }
    const url = String(req.url || '/').split('?')[0];
    if (req.method === 'GET' && (url === '/' || url.endsWith('/'))) {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(UI_HTML);
      return;
    }
    if (req.method === 'GET' && url.endsWith('/status')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(safeStatus()));
      return;
    }
    if (req.method === 'GET' && url.endsWith('/usage')) {
      try {
        const usage = await fetch(USAGE_URL);
        const body = await usage.text();
        res.writeHead(usage.ok ? 200 : 503, { 'Content-Type': 'application/json' });
        res.end(body);
      } catch (err) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'local_usage_unavailable' }));
      }
      return;
    }
    if (req.method === 'GET' && url.endsWith('/certification')) {
      try {
        const result = await fetch(CERT_STATUS_URL);
        const body = await result.text();
        res.writeHead(result.ok ? 200 : 503, { 'Content-Type': 'application/json' });
        res.end(body);
      } catch (err) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ state: 'error', message: 'Local certification status unavailable' }));
      }
      return;
    }
    if (req.method === 'GET' && url.endsWith('/certification-report')) {
      try {
        const result = await fetch(CERT_REPORT_URL);
        const body = await result.text();
        res.writeHead(result.ok ? 200 : 404, {
          'Content-Type': 'application/json; charset=utf-8',
          'Content-Disposition': 'inline; filename="alex-live-benchmark.json"',
        });
        res.end(body);
      } catch (err) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'certification_report_unavailable' }));
      }
      return;
    }
    if (req.method === 'POST' && url.endsWith('/certify-live')) {
      try {
        const result = await fetch(CERT_RUN_URL, { method: 'POST' });
        const body = await result.text();
        res.writeHead(result.ok ? 202 : 503, { 'Content-Type': 'application/json' });
        res.end(body);
      } catch (err) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'local_certification_unavailable' }));
      }
      return;
    }
    if (req.method === 'POST' && url.endsWith('/test-ai')) {
      try {
        const probe = await fetch(PROVIDER_PROBE_URL, { method: 'POST' });
        const body = await probe.text();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(body);
      } catch (err) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'error', category: 'local_probe_unavailable', message: err.message }));
      }
      return;
    }
    if (req.method === 'POST' && url.endsWith('/reset')) {
      await resetPairing();
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
      return;
    }
    if (req.method === 'GET' && (url.endsWith('/chatgpt-state') || url.endsWith('/shadow-router'))) {
      const target = url.endsWith('/chatgpt-state') ? CHATGPT_STATUS_URL : SHADOW_ROUTER_URL;
      try {
        const result = await fetch(target);
        const body = await result.text();
        res.writeHead(result.ok ? 200 : 503, { 'Content-Type': 'application/json' });
        res.end(body);
      } catch (err) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'local_status_unavailable' }));
      }
      return;
    }
    const chatgptKey = Object.keys(CHATGPT_POST_URLS).find(function(key) { return url.endsWith(key); });
    if (req.method === 'POST' && chatgptKey) {
      try {
        const body = await readSmallBody(req, 16 * 1024);
        const result = await fetch(CHATGPT_POST_URLS[chatgptKey], {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: body || '{}',
        });
        const text = await result.text();
        res.writeHead(result.status, { 'Content-Type': 'application/json' });
        res.end(text);
      } catch (err) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'Alex could not reach its local service. Try again.' }));
      }
      return;
    }
    res.writeHead(404);
    res.end('Not found');
  });
  server.listen(UI_PORT, '0.0.0.0', function() {
    console.log('[Alex MCP] Pairing UI ready on ingress port ' + UI_PORT);
  });
}

function startEgress() {
  const server = http.createServer(function(req, res) {
    if (req.method !== 'POST' || req.url !== '/send') {
      res.writeHead(404);
      res.end();
      return;
    }
    let body = '';
    req.on('data', function(chunk) {
      body += chunk;
      if (body.length > 60 * 1024 * 1024) req.destroy();
    });
    req.on('end', async function() {
      try {
        const payload = JSON.parse(body);
        if (!currentSock) throw new Error('WhatsApp is not connected');
        const to = payload.to;
        if (!to) throw new Error('Missing target conversation');

        let sent;
        let quoted = rememberedInboundForReply(to, payload.reply_to_message_id);
        // Durable fallback after a Node/add-on restart: the in-memory quote
        // cache may be gone, but Python persisted the original provider id,
        // sender participant and trusted user text. Reconstruct only the
        // minimum quoted WAMessage needed for a real anchored reply.
        if (!quoted && payload.reply_to_message_id) {
          const quotedKey = {
            remoteJid: to,
            id: String(payload.reply_to_message_id),
            fromMe: false,
          };
          if (payload.reply_to_participant_jid) {
            quotedKey.participant = String(payload.reply_to_participant_jid);
          }
          quoted = {
            key: quotedKey,
            message: { conversation: String(payload.reply_to_text || '') },
          };
        }
        const sendOptions = {};
        if (payload.message_id) sendOptions.messageId = String(payload.message_id);
        if (quoted) sendOptions.quoted = quoted;
        if (payload.kind === 'text') {
          sent = await currentSock.sendMessage(to, { text: payload.text || '' }, sendOptions);
        } else if (payload.kind === 'reaction' || payload.kind === 'pin' || payload.kind === 'unpin') {
          const control = buildControlMessage(payload, to);
          sent = await currentSock.sendMessage(to, control.content);
        } else if (payload.kind === 'image') {
          const buf = Buffer.from(payload.file_b64 || '', 'base64');
          sent = await currentSock.sendMessage(to, { image: buf, mimetype: payload.mimetype || 'image/jpeg', caption: payload.caption || undefined }, sendOptions);
        } else if (payload.kind === 'document') {
          const buf = Buffer.from(payload.file_b64 || '', 'base64');
          sent = await currentSock.sendMessage(to, { document: buf, mimetype: payload.mimetype || 'application/octet-stream', fileName: payload.filename || 'file', caption: payload.caption || undefined }, sendOptions);
        } else if (payload.kind === 'audio') {
          const buf = Buffer.from(payload.file_b64 || '', 'base64');
          const mime = String(payload.mimetype || 'audio/ogg').toLowerCase();
          const ptt = mime.includes('audio/ogg') || mime.includes('audio/opus');
          sent = await currentSock.sendMessage(to, { audio: buf, mimetype: payload.mimetype || 'audio/ogg', ptt }, sendOptions);
        } else {
          throw new Error('Unsupported outbound kind');
        }

        try { await currentSock.sendPresenceUpdate('paused', to); } catch (_err) {}
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, message_id: sent && sent.key ? sent.key.id : null }));
      } catch (err) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    });
  });
  server.listen(EGRESS_PORT, '127.0.0.1', function() {
    console.log('[Alex MCP] Egress ready on 127.0.0.1:' + EGRESS_PORT);
  });
}

startPairingUi();
startEgress();
startWhatsApp();
