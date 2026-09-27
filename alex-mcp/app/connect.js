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

const DATA_DIR = process.env.ALEX_DATA_DIR || '/data';
const AUTH_DIR = path.join(DATA_DIR, 'whatsapp_auth');
const OPTIONS_FILE = path.join(DATA_DIR, 'options.json');
const SELFTEST_FILE = path.join(DATA_DIR, 'selftest.json');
const GROUP_FILE = path.join(DATA_DIR, 'family_group.json');
const INGRESS_URL = 'http://127.0.0.1:5001/ingress';
const EGRESS_PORT = 5002;
const UI_PORT = 8099;
const logger = pino({ level: process.env.ALEX_LOG_LEVEL || 'silent' });

let currentSock = null;
let cachedVersion = null;
let starting = false;
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

function extractQuotedId(message) {
  const m = unwrapMessage(message);
  if (!m) return null;
  const ctx =
    (m.extendedTextMessage && m.extendedTextMessage.contextInfo) ||
    (m.imageMessage && m.imageMessage.contextInfo) ||
    (m.audioMessage && m.audioMessage.contextInfo) ||
    (m.documentMessage && m.documentMessage.contextInfo) ||
    null;
  return ctx && ctx.stanzaId ? ctx.stanzaId : null;
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

  const rawText = extractText(message).trim();

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
  }

  const media = detectMedia(message);
  if (!rawText && !media.type) return;

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
    text: rawText,
    quoted_message_id: extractQuotedId(message),
    image_data: media.type === 'image' && mediaData ? mediaData.data : null,
    image_mime_type: media.type === 'image' && mediaData ? mediaData.mimeType : null,
    audio_data: media.type === 'audio' && mediaData ? mediaData.data : null,
    audio_mime_type: media.type === 'audio' && mediaData ? mediaData.mimeType : null,
    pdf_data: media.type === 'pdf' && mediaData ? mediaData.data : null,
  };

  try {
    await forwardToPython(payload);
  } catch (err) {
    console.error('[Alex MCP] Ingress forwarding failed:', err.message);
    try {
      if (currentSock) {
        await currentSock.sendMessage(remoteJid, { text: 'Alex is temporarily unavailable. Please try that message once more.' });
      }
    } catch (_err) {}
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

function safeStatus() {
  const opts = readOptions();
  const provider = opts.ai_provider || 'grok';
  let keyPresent = false;
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
    },
    selftest: readSelftest(),
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
'<div class="card"><h2>Core diagnostics</h2><div id="diag">Checking…</div></div>',
'<script>',
'async function load(){try{const r=await fetch("./status");const s=await r.json();',
'const w=s.whatsapp||{};const q=document.getElementById("qr");',
'document.getElementById("wa").innerHTML="<b>Status:</b> "+esc(w.status||"unknown")+(w.linked_user?"<br><span class=ok>Connected as "+esc(w.linked_user)+"</span>":"")+(w.last_error?"<br><span class=bad>"+esc(w.last_error)+"</span>":"");',
'if(w.qr_data_url){q.src=w.qr_data_url;q.style.display="block"}else{q.style.display="none"}',
'const c=s.setup||{};document.getElementById("cfg").innerHTML="<b>AI:</b> "+esc(c.ai_provider||"")+(c.api_key_present?" <span class=ok>✓ key present</span>":" <span class=warn>— API key not set</span>")+"<br><b>Household numbers configured:</b> "+esc(String(c.configured_numbers||0))+"/2<br><b>Family group:</b> "+(c.family_group_paired?"<span class=ok>paired ✓</span>":"<span class=warn>not paired</span>");',
'const d=s.selftest;if(d){document.getElementById("diag").innerHTML="<span class="+(d.failed===0?"ok":"bad")+">"+d.passed+" passed, "+d.failed+" failed</span>"}else{document.getElementById("diag").textContent="Not run yet"}',
'}catch(e){document.getElementById("wa").textContent="Status unavailable: "+e}}',
'function esc(x){const e=document.createElement("div");e.textContent=String(x);return e.innerHTML}',
'async function resetPairing(){if(!confirm("Reset WhatsApp pairing and generate a new QR?"))return;await fetch("./reset",{method:"POST"});setTimeout(load,800)}',
'load();setInterval(load,2000);',
'</script></main></body></html>'
].join('');

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
    if (req.method === 'POST' && url.endsWith('/reset')) {
      await resetPairing();
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
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
        if (payload.kind === 'text') {
          sent = await currentSock.sendMessage(to, { text: payload.text || '' });
        } else if (payload.kind === 'image') {
          const buf = Buffer.from(payload.file_b64 || '', 'base64');
          sent = await currentSock.sendMessage(to, { image: buf, mimetype: payload.mimetype || 'image/jpeg', caption: payload.caption || undefined });
        } else if (payload.kind === 'document') {
          const buf = Buffer.from(payload.file_b64 || '', 'base64');
          sent = await currentSock.sendMessage(to, { document: buf, mimetype: payload.mimetype || 'application/octet-stream', fileName: payload.filename || 'file', caption: payload.caption || undefined });
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
