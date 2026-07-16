/**
 * ChatGPT web session helpers + /backend-api conversation client.
 * Uses cookies from the Chrome profile; does not hardcode bearer/session secrets.
 */

const CHATGPT_ORIGIN = 'https://chatgpt.com';
const CHATGPT_DOMAINS = ['chatgpt.com', '.chatgpt.com'];
const CHATGPT_URLS = [`${CHATGPT_ORIGIN}/`, CHATGPT_ORIGIN];

const DEFAULT_CONVERSATION_PATH = '/backend-api/f/conversation';
const DEFAULT_MODEL = 'gpt-5-5';
const DEFAULT_CLIENT_VERSION = 'prod-81b695379309b67344b31fc2b695f6f307dc34bb';
const DEFAULT_BUILD_NUMBER = '8250038';

const IMPORTANT_COOKIES = [
  '__Secure-next-auth.session-token',
  'oai-did',
  'cf_clearance',
  '_cfuvid',
  '__cf_bm',
  'oai-client-auth-info',
  '__Secure-oai-is',
  'oai-sc',
  '_puid',
];

async function getChatgptCookies() {
  const seen = new Map();
  for (const domain of CHATGPT_DOMAINS) {
    try {
      const list = await chrome.cookies.getAll({ domain });
      for (const c of list || []) {
        seen.set(`${c.name}|${c.domain}|${c.path}`, c);
      }
    } catch {
      /* ignore */
    }
  }
  for (const url of CHATGPT_URLS) {
    try {
      const list = await chrome.cookies.getAll({ url });
      for (const c of list || []) {
        seen.set(`${c.name}|${c.domain}|${c.path}`, c);
      }
    } catch {
      /* ignore */
    }
  }
  return [...seen.values()].sort((a, b) => a.name.localeCompare(b.name));
}

function maskValue(value) {
  const s = String(value || '');
  if (!s) return '';
  if (s.length <= 10) return `${s.slice(0, 2)}…(${s.length})`;
  return `${s.slice(0, 8)}…${s.slice(-4)} (${s.length})`;
}

function pickCookie(cookies, name) {
  return cookies.find((c) => c.name === name) || null;
}

function parseAuthInfo(cookies) {
  const raw = pickCookie(cookies, 'oai-client-auth-info')?.value;
  if (!raw) return null;
  try {
    return JSON.parse(decodeURIComponent(raw));
  } catch {
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }
}

function cookieHeaderFromList(cookies) {
  // Prefer one value per name (newest expiration / longest path wins loosely).
  const byName = new Map();
  for (const c of cookies || []) {
    const prev = byName.get(c.name);
    if (!prev) {
      byName.set(c.name, c);
      continue;
    }
    const prevExp = prev.expirationDate || 0;
    const nextExp = c.expirationDate || 0;
    if (nextExp >= prevExp) byName.set(c.name, c);
  }
  return [...byName.entries()]
    .map(([name, c]) => `${name}=${c.value}`)
    .join('; ');
}

function uuid() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (ch) => {
    const r = (Math.random() * 16) | 0;
    const v = ch === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

function normalizeEndpoint(endpoint) {
  const raw = String(endpoint || '').trim();
  if (!raw) return `${CHATGPT_ORIGIN}${DEFAULT_CONVERSATION_PATH}`;
  if (/^https?:\/\//i.test(raw)) return raw;
  const path = raw.startsWith('/') ? raw : `/${raw}`;
  return `${CHATGPT_ORIGIN}${path}`;
}

async function getChatgptCookieStatus() {
  const cookies = await getChatgptCookies();
  const present = {};
  for (const name of IMPORTANT_COOKIES) {
    present[name] = cookies.some(
      (c) => c.name === name || c.name.startsWith(`${name}.`),
    );
  }

  const sessionCookie =
    pickCookie(cookies, '__Secure-next-auth.session-token')
    || cookies.find((c) => c.name.startsWith('__Secure-next-auth.session-token'));
  const deviceId = pickCookie(cookies, 'oai-did')?.value || null;
  const authInfo = parseAuthInfo(cookies);
  const loggedIn = !!(sessionCookie || authInfo?.user?.email);

  return {
    ok: true,
    loggedIn,
    cookieCount: cookies.length,
    deviceId,
    email: authInfo?.user?.email || null,
    name: authInfo?.user?.name || null,
    present,
    cookies: cookies.map((c) => ({
      name: c.name,
      domain: c.domain,
      path: c.path,
      secure: !!c.secure,
      httpOnly: !!c.httpOnly,
      session: !!c.session,
      expirationDate: c.expirationDate || null,
      valuePreview: maskValue(c.value),
    })),
  };
}

async function openChatgptTab() {
  const tabs = await chrome.tabs.query({
    url: ['https://chatgpt.com/*', 'https://chat.openai.com/*'],
  });
  if (tabs.length) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    if (tabs[0].windowId) {
      await chrome.windows.update(tabs[0].windowId, { focused: true });
    }
    return { ok: true, tabId: tabs[0].id };
  }
  const tab = await chrome.tabs.create({ url: `${CHATGPT_ORIGIN}/`, active: true });
  return { ok: true, tabId: tab?.id };
}

async function chatgptFetch(pathOrUrl, { method = 'GET', headers = {}, body, cookies } = {}) {
  const url = /^https?:\/\//i.test(pathOrUrl)
    ? pathOrUrl
    : `${CHATGPT_ORIGIN}${pathOrUrl.startsWith('/') ? pathOrUrl : `/${pathOrUrl}`}`;

  const cookieList = cookies || await getChatgptCookies();
  const deviceId = pickCookie(cookieList, 'oai-did')?.value || uuid();
  const cookieHeader = cookieHeaderFromList(cookieList);

  const finalHeaders = {
    Accept: 'application/json',
    'oai-language': 'en-US',
    'oai-device-id': deviceId,
    'oai-client-version': DEFAULT_CLIENT_VERSION,
    'oai-client-build-number': DEFAULT_BUILD_NUMBER,
    ...headers,
  };
  if (cookieHeader) finalHeaders.Cookie = cookieHeader;

  const init = { method, headers: finalHeaders, credentials: 'include' };
  if (body !== undefined) {
    init.body = typeof body === 'string' ? body : JSON.stringify(body);
    if (!finalHeaders['Content-Type'] && !finalHeaders['content-type']) {
      finalHeaders['Content-Type'] = 'application/json';
    }
  }

  const resp = await fetch(url, init);
  return { resp, cookieList, deviceId };
}

async function getAccessToken(cookies) {
  const { resp } = await chatgptFetch('/api/auth/session', {
    method: 'GET',
    headers: { Accept: 'application/json' },
    cookies,
  });
  if (!resp.ok) {
    throw new Error(`session_http_${resp.status}`);
  }
  const data = await resp.json().catch(() => ({}));
  const token = data?.accessToken || data?.access_token;
  if (!token) throw new Error('missing_access_token');
  return {
    accessToken: token,
    user: data?.user || null,
    expires: data?.expires || null,
  };
}

async function getChatRequirements(accessToken, cookies, deviceId) {
  const { resp } = await chatgptFetch('/backend-api/sentinel/chat-requirements', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'oai-device-id': deviceId,
    },
    body: {},
    cookies,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = data?.detail || data?.error || `requirements_http_${resp.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

function guessMimeFromName(name) {
  const lower = String(name || '').toLowerCase();
  if (lower.endsWith('.png')) return 'image/png';
  if (lower.endsWith('.webp')) return 'image/webp';
  if (lower.endsWith('.gif')) return 'image/gif';
  if (lower.endsWith('.jpg') || lower.endsWith('.jpeg')) return 'image/jpeg';
  return 'image/jpeg';
}

function decodeDataUrlOrBase64(raw) {
  const s = String(raw || '');
  if (!s) return { bytes: null, mime: null };
  const m = /^data:([^;,]+)?(;base64)?,(.*)$/i.exec(s);
  if (m) {
    const mime = m[1] || 'application/octet-stream';
    const b64 = m[3] || '';
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return { bytes, mime };
  }
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return { bytes, mime: null };
}

async function getImageDimensions(bytes, mime) {
  try {
    const blob = new Blob([bytes], { type: mime || 'image/jpeg' });
    if (typeof createImageBitmap === 'function') {
      const bmp = await createImageBitmap(blob);
      const out = { width: bmp.width || 0, height: bmp.height || 0 };
      bmp.close?.();
      return out;
    }
  } catch {
    /* ignore */
  }
  return { width: 0, height: 0 };
}

/**
 * Upload image via ChatGPT /backend-api/files (3 steps).
 * @param {object} opts
 * @param {string|Uint8Array|ArrayBuffer} opts.data - raw bytes or base64 / data URL
 * @param {string} [opts.fileName]
 * @param {string} [opts.mimeType]
 * @param {string} [opts.accessToken]
 * @param {string} [opts.filesEndpoint] - default /backend-api/files
 */
async function uploadImage(opts = {}) {
  const cookies = await getChatgptCookies();
  const deviceId = pickCookie(cookies, 'oai-did')?.value || uuid();

  let accessToken = opts.accessToken || null;
  if (!accessToken) {
    try {
      accessToken = (await getAccessToken(cookies)).accessToken;
    } catch (e) {
      return { ok: false, error: e?.message || 'auth_session_failed' };
    }
  }

  let bytes;
  let mime = opts.mimeType || null;
  if (opts.data instanceof Uint8Array) {
    bytes = opts.data;
  } else if (opts.data instanceof ArrayBuffer) {
    bytes = new Uint8Array(opts.data);
  } else if (typeof opts.data === 'string') {
    const decoded = decodeDataUrlOrBase64(opts.data);
    bytes = decoded.bytes;
    mime = mime || decoded.mime;
  } else if (Array.isArray(opts.data)) {
    bytes = Uint8Array.from(opts.data);
  } else {
    return { ok: false, error: 'missing_image_data' };
  }
  if (!bytes || !bytes.length) return { ok: false, error: 'empty_image_data' };

  const fileName = String(opts.fileName || `upload-${Date.now()}.jpg`);
  mime = mime || guessMimeFromName(fileName);
  const dims = await getImageDimensions(bytes, mime);
  const filesPath = opts.filesEndpoint || '/backend-api/files';

  const createBody = {
    file_name: fileName,
    file_size: bytes.length,
    use_case: 'multimodal',
    timezone_offset_min: opts.timezoneOffsetMin ?? -new Date().getTimezoneOffset(),
    reset_rate_limits: false,
    supports_direct_azure_multipart: true,
    mime_type: mime,
    entry_surface: 'chat_composer',
    selection_method: opts.selectionMethod || 'drag_drop',
    client_resolved_mime_type: mime,
    mime_resolution_source: 'filename_extension',
    store_in_library: true,
    library_persistence_mode: 'opportunistic',
    ...(opts.extraCreatePayload && typeof opts.extraCreatePayload === 'object'
      ? opts.extraCreatePayload
      : {}),
  };

  let createResp;
  try {
    ({ resp: createResp } = await chatgptFetch(filesPath, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${accessToken}`,
        Accept: 'application/json',
        'Content-Type': 'application/json',
        Origin: CHATGPT_ORIGIN,
        Referer: `${CHATGPT_ORIGIN}/`,
        'oai-device-id': deviceId,
      },
      body: createBody,
      cookies,
    }));
  } catch (e) {
    return { ok: false, error: e?.message || 'files_create_network_error' };
  }

  const createJson = await createResp.json().catch(() => ({}));
  if (!createResp.ok) {
    const detail = createJson?.detail || createJson?.error || `files_create_http_${createResp.status}`;
    return { ok: false, error: typeof detail === 'string' ? detail : JSON.stringify(detail), step: 'create' };
  }
  const fileId = createJson.file_id || createJson.fileId;
  const uploadUrl = createJson.upload_url || createJson.uploadUrl;
  if (!fileId || !uploadUrl) {
    return { ok: false, error: 'missing_file_id_or_upload_url', step: 'create', raw: createJson };
  }

  let putResp;
  try {
    putResp = await fetch(uploadUrl, {
      method: 'PUT',
      headers: {
        'Content-Type': mime,
        'Content-Length': String(bytes.length),
        'x-ms-blob-type': 'BlockBlob',
      },
      body: bytes,
    });
  } catch (e) {
    return { ok: false, error: e?.message || 'azure_put_network_error', step: 'put', fileId };
  }
  if (!putResp.ok) {
    const raw = await putResp.text().catch(() => '');
    return {
      ok: false,
      error: `azure_put_http_${putResp.status}:${raw.slice(0, 200)}`,
      step: 'put',
      fileId,
    };
  }

  // Finalize — retry while status === retry
  const finalizePath = `${filesPath.replace(/\/$/, '')}/${encodeURIComponent(fileId)}/uploaded`;
  let finalizeJson = null;
  for (let i = 0; i < 40; i++) {
    let finResp;
    try {
      ({ resp: finResp } = await chatgptFetch(finalizePath, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${accessToken}`,
          Accept: 'application/json',
          'Content-Type': 'application/json',
          Origin: CHATGPT_ORIGIN,
          Referer: `${CHATGPT_ORIGIN}/`,
          'oai-device-id': deviceId,
        },
        body: {},
        cookies,
      }));
    } catch (e) {
      return { ok: false, error: e?.message || 'files_finalize_network_error', step: 'uploaded', fileId };
    }
    finalizeJson = await finResp.json().catch(() => ({}));
    if (!finResp.ok) {
      const detail = finalizeJson?.detail || finalizeJson?.error || `files_uploaded_http_${finResp.status}`;
      return { ok: false, error: typeof detail === 'string' ? detail : JSON.stringify(detail), step: 'uploaded', fileId };
    }
    const st = String(finalizeJson?.status || '');
    if (st === 'success' || st === 'Success') break;
    if (st === 'retry' || st === 'pending') {
      await new Promise((r) => setTimeout(r, 250));
      continue;
    }
    // Some accounts return success without status field.
    if (finalizeJson?.download_url || finalizeJson?.file_id) break;
    await new Promise((r) => setTimeout(r, 250));
  }

  return {
    ok: true,
    status: finalizeJson?.status || 'success',
    fileId,
    uploadUrl,
    downloadUrl: finalizeJson?.download_url || null,
    libraryFileId:
      finalizeJson?.library_file_id
      || createJson?.library_file_id
      || null,
    fileName,
    mimeType: mime,
    fileSize: bytes.length,
    width: dims.width,
    height: dims.height,
  };
}

function normalizeSystemHints(opts = {}) {
  const hints = [];
  const mode = String(opts.mode || opts.chatMode || opts.chat_mode || '').toLowerCase().trim();
  if (
    mode === 'picture_v2'
    || mode === 'picture'
    || opts.picture === true
    || opts.picture_v2 === true
  ) {
    hints.push('picture_v2');
  }
  const raw = opts.systemHints || opts.system_hints;
  if (Array.isArray(raw)) {
    for (const h of raw) {
      const s = String(h || '').trim();
      if (s && !hints.includes(s)) hints.push(s);
    }
  } else if (typeof raw === 'string' && raw.trim()) {
    const s = raw.trim();
    if (!hints.includes(s)) hints.push(s);
  }
  return hints;
}

function buildConversationPayload(prompt, opts = {}) {
  const messageId = opts.messageId || uuid();
  const parentMessageId = opts.parentMessageId || 'client-created-root';
  const model = opts.model || DEFAULT_MODEL;
  const now = Date.now() / 1000;
  const images = Array.isArray(opts.images) ? opts.images.filter(Boolean) : [];
  const systemHints = normalizeSystemHints(opts);

  let content;
  let metadata = {
    selected_sources: [],
    serialization_metadata: { custom_symbol_offsets: [] },
  };

  if (images.length) {
    const parts = [];
    const attachments = [];
    for (const img of images) {
      const fileId = img.fileId || img.file_id;
      if (!fileId) continue;
      const size = img.fileSize || img.size_bytes || img.size || 0;
      const width = img.width || 0;
      const height = img.height || 0;
      const name = img.fileName || img.name || `${fileId}.jpg`;
      const mime = img.mimeType || img.mime_type || 'image/jpeg';
      parts.push({
        content_type: 'image_asset_pointer',
        asset_pointer: `sediment://${fileId}`,
        size_bytes: size,
        width,
        height,
      });
      const att = {
        id: fileId,
        size,
        name,
        mime_type: mime,
        width,
        height,
        source: 'local',
        is_big_paste: !!img.is_big_paste,
      };
      const libId = img.libraryFileId || img.library_file_id;
      if (libId) att.library_file_id = libId;
      attachments.push(att);
    }
    parts.push(String(prompt || ''));
    content = { content_type: 'multimodal_text', parts };
    metadata = { ...metadata, attachments };
  } else {
    content = {
      content_type: 'text',
      parts: [String(prompt || '')],
    };
  }

  if (systemHints.length) {
    metadata = { ...metadata, system_hints: systemHints };
  }

  const payload = {
    action: 'next',
    messages: [
      {
        id: messageId,
        author: { role: 'user' },
        create_time: now,
        content,
        metadata,
      },
    ],
    parent_message_id: parentMessageId,
    model,
    client_prepare_state: 'none',
    timezone_offset_min: opts.timezoneOffsetMin ?? -new Date().getTimezoneOffset(),
    timezone: opts.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Bangkok',
    conversation_mode: { kind: 'primary_assistant' },
    enable_message_followups: true,
    system_hints: systemHints,
    supports_buffering: true,
    supported_encodings: ['v1'],
    client_contextual_info: {
      is_dark_mode: true,
      time_since_loaded: opts.timeSinceLoaded ?? 110,
      page_height: 945,
      page_width: 1293,
      pixel_ratio: 1,
      screen_height: 1080,
      screen_width: 1920,
      app_name: 'chatgpt.com',
      has_web_push_capabilities: true,
      web_push_notification_permission: 'default',
    },
    paragen_cot_summary_display_override: 'allow',
    force_parallel_switch: 'auto',
    ...(systemHints.includes('picture_v2')
      ? { thinking_effort: opts.thinkingEffort || opts.thinking_effort || 'standard' }
      : {}),
    ...(opts.conversationId ? { conversation_id: opts.conversationId } : {}),
    ...(opts.extraPayload && typeof opts.extraPayload === 'object' ? opts.extraPayload : {}),
  };

  // Message-level system_hints must survive extraPayload shallow merge.
  if (systemHints.length && payload.messages?.[0]?.metadata) {
    payload.messages[0].metadata.system_hints = systemHints;
  }
  if (systemHints.length) {
    payload.system_hints = systemHints;
  }

  return payload;
}

function extractTextFromDeltaOps(ops) {
  if (!Array.isArray(ops)) return '';
  let out = '';
  for (const op of ops) {
    if (!op || typeof op !== 'object') continue;
    if (op.o === 'append' && typeof op.v === 'string') {
      // Prefer message content parts appends
      const p = String(op.p || '');
      if (!p || p.includes('/content/parts/') || p === '/message/content/parts/0') {
        out += op.v;
      }
    } else if (op.o === 'patch' && Array.isArray(op.v)) {
      out += extractTextFromDeltaOps(op.v);
    }
  }
  return out;
}

function extractTextFromEventData(data) {
  if (!data || typeof data === 'string') {
    if (data === '[DONE]') return '';
    return '';
  }
  if (typeof data !== 'object') return '';

  // Ignore control / metadata messages
  if (data.type === 'message_marker'
    || data.type === 'server_ste_metadata'
    || data.type === 'message_stream_complete'
    || data.type === 'conversation_detail_metadata') {
    return '';
  }

  // v1 json-patch style deltas
  if (data.o === 'patch' && Array.isArray(data.v)) {
    return extractTextFromDeltaOps(data.v);
  }
  if (data.o === 'append' && typeof data.v === 'string') {
    return data.v;
  }
  // Compact append: {"v":"chunk"} without o/p
  if (typeof data.v === 'string' && data.o == null && data.p == null && !data.message) {
    return data.v;
  }
  if (typeof data.v === 'string' && typeof data.p === 'string' && data.p.includes('/content/parts/')) {
    return data.v;
  }

  // Standard message envelope (full parts)
  const msg = data.message;
  if (msg?.author?.role === 'assistant' || msg?.author?.role === 'tool') {
    const parts = msg?.content?.parts;
    if (Array.isArray(parts)) {
      return parts.filter((p) => typeof p === 'string').join('');
    }
  }

  if (typeof data.delta === 'string') return data.delta;
  if (typeof data.text === 'string') return data.text;

  return '';
}

const ASSET_POINTER_RE = /^(?:file-service|sediment):\/\/(.+)$/i;
const SANDBOX_LINK_RE = /\[([^\]]*)\]\((sandbox:\/mnt\/data\/[^)\s]+)\)/gi;
const SANDBOX_PATH_RE = /sandbox:(\/mnt\/data\/[^\s)'"]+)/gi;
const IMAGE_MIME_RE = /^image\//i;
const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|bmp|svg)$/i;
const MAX_ASSET_BASE64_BYTES = 8 * 1024 * 1024;

function parseAssetPointer(pointer) {
  const m = ASSET_POINTER_RE.exec(String(pointer || '').trim());
  return m ? m[1] : null;
}

function guessKindFromNameMime(name, mime) {
  if (mime && IMAGE_MIME_RE.test(mime)) return 'image';
  if (name && IMAGE_EXT_RE.test(name)) return 'image';
  return 'file';
}

function basenameFromPath(path) {
  const s = String(path || '');
  const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
  return i >= 0 ? s.slice(i + 1) : s;
}

function collectAssetRefsFromPart(part, into, seen) {
  if (!part || typeof part !== 'object') return;
  const ctype = String(part.content_type || part.contentType || '');
  const pointer = part.asset_pointer || part.assetPointer || '';
  const fileId = parseAssetPointer(pointer) || part.file_id || part.fileId || part.id || null;
  if (!fileId && !pointer) return;

  const key = fileId || pointer;
  if (seen.has(key)) return;
  seen.add(key);

  const name =
    part.file_name
    || part.filename
    || part.name
    || (part.metadata?.dalle ? 'dalle_image.png' : null)
    || (ctype.includes('image') ? 'image.png' : 'file');
  const mime = part.mime_type || part.mimeType || (ctype.includes('image') ? 'image/png' : null);
  const kind = ctype.includes('image') || guessKindFromNameMime(name, mime) === 'image'
    ? 'image'
    : 'file';

  into.push({
    fileId: fileId || null,
    fileName: name,
    mimeType: mime,
    kind,
    width: part.width || 0,
    height: part.height || 0,
    sizeBytes: part.size_bytes || part.sizeBytes || part.size || 0,
    assetPointer: pointer || (fileId ? `sediment://${fileId}` : null),
    source: 'part',
  });
}

function collectAssetRefsFromMessage(msg, into, seen, { includeUser = false } = {}) {
  if (!msg || typeof msg !== 'object') return;
  const role = msg?.author?.role || msg?.role || '';
  if (!includeUser && role === 'user') return;

  const parts = msg?.content?.parts;
  if (Array.isArray(parts)) {
    for (const part of parts) collectAssetRefsFromPart(part, into, seen);
  }

  const meta = msg.metadata || {};
  for (const att of meta.attachments || []) {
    const fid = att?.id || att?.file_id || att?.fileId;
    if (!fid || seen.has(fid)) continue;
    seen.add(fid);
    const name = att.name || att.file_name || att.filename || 'attachment';
    const mime = att.mime_type || att.mimeType || null;
    into.push({
      fileId: fid,
      fileName: name,
      mimeType: mime,
      kind: guessKindFromNameMime(name, mime),
      width: att.width || 0,
      height: att.height || 0,
      sizeBytes: att.size || att.size_bytes || 0,
      assetPointer: `sediment://${fid}`,
      source: 'attachment',
    });
  }

  for (const cit of meta.citations || []) {
    const fid = cit?.metadata?.file_id || cit?.file_id || cit?.fileId;
    if (!fid || seen.has(fid)) continue;
    seen.add(fid);
    const name = cit?.metadata?.title || cit?.title || 'citation';
    into.push({
      fileId: fid,
      fileName: name,
      mimeType: null,
      kind: guessKindFromNameMime(name, null),
      width: 0,
      height: 0,
      sizeBytes: 0,
      assetPointer: `sediment://${fid}`,
      source: 'citation',
    });
  }

  for (const fid of meta.file_ids || meta.gizmo_file_ids || []) {
    if (!fid || seen.has(fid)) continue;
    seen.add(fid);
    into.push({
      fileId: fid,
      fileName: fid,
      mimeType: null,
      kind: 'file',
      width: 0,
      height: 0,
      sizeBytes: 0,
      assetPointer: `sediment://${fid}`,
      source: 'file_ids',
    });
  }
}

function collectSandboxRefsFromText(text, into, seen) {
  const s = String(text || '');
  if (!s) return;

  SANDBOX_LINK_RE.lastIndex = 0;
  let m;
  while ((m = SANDBOX_LINK_RE.exec(s))) {
    const label = (m[1] || '').trim();
    const path = String(m[2] || '').replace(/^sandbox:/i, '');
    const key = `sandbox:${path}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const fileName = label || basenameFromPath(path) || 'file';
    into.push({
      fileId: null,
      fileName,
      mimeType: null,
      kind: guessKindFromNameMime(fileName, null),
      width: 0,
      height: 0,
      sizeBytes: 0,
      assetPointer: null,
      sandboxPath: path,
      source: 'sandbox_markdown',
    });
  }

  SANDBOX_PATH_RE.lastIndex = 0;
  while ((m = SANDBOX_PATH_RE.exec(s))) {
    const path = m[1];
    const key = `sandbox:${path}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const fileName = basenameFromPath(path) || 'file';
    into.push({
      fileId: null,
      fileName,
      mimeType: null,
      kind: guessKindFromNameMime(fileName, null),
      width: 0,
      height: 0,
      sizeBytes: 0,
      assetPointer: null,
      sandboxPath: path,
      source: 'sandbox_path',
    });
  }
}

function collectAssetRefsFromEvents(events, text) {
  const refs = [];
  const seen = new Set();
  for (const data of events || []) {
    if (!data || typeof data !== 'object') continue;
    if (data.message) collectAssetRefsFromMessage(data.message, refs, seen);
    // Patch ops may insert full image parts as objects
    if (data.o === 'patch' && Array.isArray(data.v)) {
      for (const op of data.v) {
        if (op && typeof op.v === 'object') collectAssetRefsFromPart(op.v, refs, seen);
      }
    }
    if (data.o === 'add' && data.v && typeof data.v === 'object') {
      collectAssetRefsFromPart(data.v, refs, seen);
    }
  }
  collectSandboxRefsFromText(text, refs, seen);
  return refs;
}

async function fetchConversationDetail(conversationId, accessToken, cookies) {
  if (!conversationId) return null;
  try {
    const { resp } = await chatgptFetch(`/backend-api/conversation/${conversationId}`, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${accessToken}`,
        Accept: 'application/json',
      },
      cookies,
    });
    if (!resp.ok) return null;
    return await resp.json().catch(() => null);
  } catch {
    return null;
  }
}

function extractAssistantTextFromConversation(convo) {
  const mapping = convo?.mapping || {};
  const messages = [];
  for (const node of Object.values(mapping)) {
    const msg = node?.message;
    if (!msg) continue;
    messages.push(msg);
  }
  messages.sort((a, b) => (Number(a.create_time) || 0) - (Number(b.create_time) || 0));
  let lastUserIdx = -1;
  for (let i = 0; i < messages.length; i++) {
    if (messages[i]?.author?.role === 'user') lastUserIdx = i;
  }
  const slice = lastUserIdx >= 0 ? messages.slice(lastUserIdx + 1) : messages.slice(-8);
  const chunks = [];
  for (const msg of slice) {
    const role = msg?.author?.role;
    if (role !== 'assistant' && role !== 'tool') continue;
    const parts = msg?.content?.parts;
    if (!Array.isArray(parts)) continue;
    for (const p of parts) {
      if (typeof p === 'string' && p.trim()) chunks.push(p);
    }
  }
  return chunks.join('');
}

function conversationImageRefsReady(refs) {
  return (refs || []).some((r) => {
    if (!r) return false;
    if (r.kind === 'image') return true;
    const mime = String(r.mimeType || r.mime_type || '').toLowerCase();
    if (mime.startsWith('image/')) return true;
    const name = String(r.fileName || r.file_name || '');
    return /\.(png|jpe?g|webp|gif)$/i.test(name);
  });
}

/**
 * picture_v2 SSE often finishes before images exist (ghostrider async).
 * Poll conversation detail until image asset pointers appear.
 */
async function waitForConversationImages(
  conversationId,
  accessToken,
  cookies,
  {
    excludeFileIds = null,
    maxAttempts = 36,
    intervalMs = 2500,
  } = {},
) {
  let lastRefs = [];
  let lastText = '';
  for (let i = 0; i < maxAttempts; i++) {
    if (i > 0) {
      await new Promise((r) => setTimeout(r, intervalMs));
    }
    const convo = await fetchConversationDetail(conversationId, accessToken, cookies);
    if (!convo) continue;
    lastRefs = collectAssetRefsFromConversation(convo, { excludeFileIds });
    lastText = extractAssistantTextFromConversation(convo) || lastText;
    if (conversationImageRefsReady(lastRefs)) {
      return { refs: lastRefs, text: lastText, attempts: i + 1 };
    }
  }
  return { refs: lastRefs, text: lastText, attempts: maxAttempts };
}

function collectAssetRefsFromConversation(convo, { excludeFileIds = null } = {}) {
  const refs = [];
  const seen = new Set();
  const mapping = convo?.mapping || {};
  const messages = [];
  for (const node of Object.values(mapping)) {
    const msg = node?.message;
    if (!msg) continue;
    messages.push(msg);
  }
  messages.sort((a, b) => (Number(a.create_time) || 0) - (Number(b.create_time) || 0));

  // Only assets from the latest turn (messages after the last user message).
  let lastUserIdx = -1;
  for (let i = 0; i < messages.length; i++) {
    if (messages[i]?.author?.role === 'user') lastUserIdx = i;
  }
  const slice = lastUserIdx >= 0 ? messages.slice(lastUserIdx + 1) : messages.slice(-8);

  for (const msg of slice) {
    collectAssetRefsFromMessage(msg, refs, seen, { includeUser: false });
    const parts = msg?.content?.parts;
    if (Array.isArray(parts)) {
      for (const p of parts) {
        if (typeof p === 'string') collectSandboxRefsFromText(p, refs, seen);
      }
    }
  }

  if (excludeFileIds && excludeFileIds.size) {
    return refs.filter((r) => !r.fileId || !excludeFileIds.has(r.fileId));
  }
  return refs;
}

function mergeAssetRefs(...lists) {
  const out = [];
  const seen = new Set();
  for (const list of lists) {
    for (const ref of list || []) {
      const key = ref.fileId
        || (ref.sandboxPath ? `sandbox:${ref.sandboxPath}` : null)
        || ref.assetPointer
        || `${ref.fileName}:${ref.source}`;
      if (!key || seen.has(key)) {
        // Prefer filling missing fileId onto sandbox entry
        if (ref.fileId && ref.sandboxPath) {
          const existing = out.find((x) => x.sandboxPath === ref.sandboxPath && !x.fileId);
          if (existing) existing.fileId = ref.fileId;
        }
        continue;
      }
      seen.add(key);
      out.push({ ...ref });
    }
  }
  // Match sandbox files to attachment fileIds by filename
  for (const ref of out) {
    if (ref.fileId || !ref.sandboxPath) continue;
    const want = (ref.fileName || basenameFromPath(ref.sandboxPath) || '').toLowerCase();
    if (!want) continue;
    const match = out.find((x) => x.fileId && String(x.fileName || '').toLowerCase() === want);
    if (match) ref.fileId = match.fileId;
  }
  return out;
}

async function resolveFileDownloadMeta(fileId, accessToken, cookies) {
  const { resp } = await chatgptFetch(`/backend-api/files/download/${encodeURIComponent(fileId)}`, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      Accept: 'application/json',
    },
    cookies,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = data?.detail || data?.error || `files_download_http_${resp.status}`;
    return { ok: false, error: typeof detail === 'string' ? detail : JSON.stringify(detail) };
  }
  return {
    ok: true,
    downloadUrl: data.download_url || data.downloadUrl || null,
    fileName: data.file_name || data.fileName || null,
    status: data.status || null,
    mimeType: data.mime_type || data.mimeType || data.content_type || null,
    raw: data,
  };
}

function bytesToBase64(bytes) {
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

async function fetchBinaryAsDataUrl(url, accessToken, cookies, mimeHint) {
  const { resp } = await chatgptFetch(url, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      Accept: '*/*',
    },
    cookies,
  });
  if (!resp.ok) return { ok: false, error: `binary_http_${resp.status}` };
  const buf = new Uint8Array(await resp.arrayBuffer());
  if (buf.byteLength > MAX_ASSET_BASE64_BYTES) {
    return {
      ok: true,
      tooLarge: true,
      sizeBytes: buf.byteLength,
      mimeType: resp.headers.get('content-type') || mimeHint || 'application/octet-stream',
    };
  }
  const mime = (resp.headers.get('content-type') || mimeHint || 'application/octet-stream')
    .split(';')[0]
    .trim();
  return {
    ok: true,
    sizeBytes: buf.byteLength,
    mimeType: mime,
    data: `data:${mime};base64,${bytesToBase64(buf)}`,
  };
}

async function tryDownloadSandboxFile(conversationId, sandboxPath, accessToken, cookies) {
  if (!conversationId || !sandboxPath) return { ok: false, error: 'missing_sandbox_context' };
  const path = sandboxPath.startsWith('/') ? sandboxPath : `/${sandboxPath}`;
  const candidates = [
    `/backend-api/conversation/${conversationId}/interpreter/download?path=${encodeURIComponent(path)}`,
    `/backend-api/conversation/${conversationId}/download?file_path=${encodeURIComponent(path)}`,
    `/backend-api/conversation/${conversationId}/attachment?path=${encodeURIComponent(path)}`,
  ];
  for (const url of candidates) {
    try {
      const { resp } = await chatgptFetch(url, {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${accessToken}`,
          Accept: '*/*',
        },
        cookies,
      });
      if (!resp.ok) continue;
      const ctype = (resp.headers.get('content-type') || '').toLowerCase();
      if (ctype.includes('application/json')) {
        const data = await resp.json().catch(() => ({}));
        const downloadUrl = data.download_url || data.downloadUrl || data.url;
        if (downloadUrl) {
          return { ok: true, downloadUrl, fileName: data.file_name || data.fileName || null, mimeType: data.mime_type || null };
        }
        if (data.file_id || data.fileId) {
          return { ok: true, fileId: data.file_id || data.fileId };
        }
        continue;
      }
      const buf = new Uint8Array(await resp.arrayBuffer());
      if (!buf.byteLength) continue;
      const mime = (resp.headers.get('content-type') || 'application/octet-stream').split(';')[0].trim();
      if (buf.byteLength > MAX_ASSET_BASE64_BYTES) {
        return { ok: true, tooLarge: true, sizeBytes: buf.byteLength, mimeType: mime };
      }
      return {
        ok: true,
        sizeBytes: buf.byteLength,
        mimeType: mime,
        data: `data:${mime};base64,${bytesToBase64(buf)}`,
      };
    } catch {
      /* try next */
    }
  }
  return { ok: false, error: 'sandbox_download_failed' };
}

async function resolveAssetRefs(refs, {
  accessToken,
  cookies,
  downloadAssets = true,
  conversationId = null,
} = {}) {
  const images = [];
  const files = [];

  for (const ref of refs || []) {
    const entry = {
      file_id: ref.fileId || null,
      file_name: ref.fileName || null,
      mime_type: ref.mimeType || null,
      kind: ref.kind || 'file',
      width: ref.width || 0,
      height: ref.height || 0,
      size_bytes: ref.sizeBytes || 0,
      asset_pointer: ref.assetPointer || null,
      sandbox_path: ref.sandboxPath || null,
      source: ref.source || null,
      download_url: null,
      data: null,
      error: null,
    };

    if (ref.fileId) {
      try {
        const meta = await resolveFileDownloadMeta(ref.fileId, accessToken, cookies);
        if (meta.ok) {
          entry.download_url = meta.downloadUrl;
          if (meta.fileName) entry.file_name = meta.fileName;
          if (meta.mimeType) entry.mime_type = meta.mimeType;
          entry.kind = guessKindFromNameMime(entry.file_name, entry.mime_type);
          if (downloadAssets && meta.downloadUrl) {
            const bin = await fetchBinaryAsDataUrl(
              meta.downloadUrl,
              accessToken,
              cookies,
              entry.mime_type,
            );
            if (bin.ok && bin.data) {
              entry.data = bin.data;
              entry.mime_type = bin.mimeType || entry.mime_type;
              entry.size_bytes = bin.sizeBytes || entry.size_bytes;
              entry.kind = guessKindFromNameMime(entry.file_name, entry.mime_type);
            } else if (bin.ok && bin.tooLarge) {
              entry.size_bytes = bin.sizeBytes;
              entry.mime_type = bin.mimeType || entry.mime_type;
              entry.error = 'file_too_large_for_base64';
            } else if (!bin.ok) {
              entry.error = bin.error;
            }
          }
        } else {
          entry.error = meta.error;
        }
      } catch (e) {
        entry.error = e?.message || 'resolve_failed';
      }
    } else if (ref.sandboxPath) {
      try {
        const sand = await tryDownloadSandboxFile(
          conversationId,
          ref.sandboxPath,
          accessToken,
          cookies,
        );
        if (sand.ok && sand.fileId) {
          entry.file_id = sand.fileId;
          const meta = await resolveFileDownloadMeta(sand.fileId, accessToken, cookies);
          if (meta.ok) {
            entry.download_url = meta.downloadUrl;
            if (meta.fileName) entry.file_name = meta.fileName;
            if (meta.mimeType) entry.mime_type = meta.mimeType;
          }
        }
        if (sand.ok && sand.downloadUrl) entry.download_url = sand.downloadUrl;
        if (sand.ok && sand.data) {
          entry.data = sand.data;
          entry.mime_type = sand.mimeType || entry.mime_type;
          entry.size_bytes = sand.sizeBytes || entry.size_bytes;
        } else if (sand.ok && sand.tooLarge) {
          entry.size_bytes = sand.sizeBytes;
          entry.mime_type = sand.mimeType || entry.mime_type;
          entry.error = 'file_too_large_for_base64';
        } else if (!entry.download_url && !entry.data) {
          entry.error = sand.error || 'sandbox_needs_file_id';
        }
        entry.kind = guessKindFromNameMime(entry.file_name, entry.mime_type);
      } catch (e) {
        entry.error = e?.message || 'sandbox_resolve_failed';
      }
    }

    if (entry.kind === 'image') images.push(entry);
    else files.push(entry);
  }

  return { images, files };
}

async function parseConversationSse(resp) {
  const reader = resp.body?.getReader?.();
  if (!reader) {
    const text = await resp.text();
    return { text, conversationId: null, messageId: null, raw: text, assetRefs: [] };
  }

  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let assistantText = '';
  let conversationId = null;
  let messageId = null;
  const events = [];

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep;
    while ((sep = buffer.indexOf('\n\n')) >= 0) {
      const chunk = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const lines = chunk.split('\n');
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload || payload === '[DONE]') continue;
        let data;
        try {
          data = JSON.parse(payload);
        } catch {
          continue;
        }
        events.push(data);
        if (data.conversation_id) conversationId = data.conversation_id;
        if (data.message_id) messageId = data.message_id;
        if (data.message?.id) messageId = data.message.id;

        // Full message parts replace accumulated text; deltas append.
        if (data.message?.content?.parts && Array.isArray(data.message.content.parts)) {
          const full = data.message.content.parts
            .filter((p) => typeof p === 'string')
            .join('');
          if (full) assistantText = full;
          continue;
        }
        const piece = extractTextFromEventData(data);
        if (piece) assistantText += piece;
      }
    }
  }

  const assetRefs = collectAssetRefsFromEvents(events, assistantText);
  return { text: assistantText, conversationId, messageId, events, assetRefs };
}

/**
 * Call ChatGPT web conversation API (SSE).
 *
 * @param {object} opts
 * @param {string} opts.prompt - User message
 * @param {string} [opts.endpoint] - Full URL or path (e.g. /backend-api/f/conversation)
 * @param {string} [opts.model]
 * @param {string} [opts.accessToken] - Override bearer (else fetched from /api/auth/session)
 * @param {string} [opts.requirementsToken]
 * @param {string} [opts.proofToken]
 * @param {string} [opts.turnstileToken]
 * @param {string} [opts.conduitToken]
 * @param {object} [opts.extraHeaders]
 * @param {object} [opts.extraPayload]
 * @param {string[]|string} [opts.systemHints] - e.g. ["picture_v2"] for Conversation image
 * @param {string} [opts.mode] - "picture_v2" / "picture"
 */
async function sendConversation(opts = {}) {
  const prompt = String(opts.prompt || '').trim();
  const rawImages = Array.isArray(opts.images) ? opts.images : [];
  if (!prompt && !rawImages.length) return { ok: false, error: 'empty_prompt' };

  const endpoint = normalizeEndpoint(opts.endpoint || DEFAULT_CONVERSATION_PATH);
  const cookies = await getChatgptCookies();
  const deviceId = pickCookie(cookies, 'oai-did')?.value || uuid();
  const sessionId = opts.sessionId || uuid();

  let accessToken = opts.accessToken || null;
  if (!accessToken) {
    try {
      const session = await getAccessToken(cookies);
      accessToken = session.accessToken;
    } catch (e) {
      return { ok: false, error: e?.message || 'auth_session_failed' };
    }
  }

  const uploadedImages = [];
  for (const img of rawImages) {
    if (img?.fileId || img?.file_id) {
      uploadedImages.push({
        fileId: img.fileId || img.file_id,
        fileName: img.fileName || img.name,
        mimeType: img.mimeType || img.mime_type,
        fileSize: img.fileSize || img.size_bytes || img.size,
        width: img.width || 0,
        height: img.height || 0,
        libraryFileId: img.libraryFileId || img.library_file_id,
        is_big_paste: !!img.is_big_paste,
      });
      continue;
    }
    const up = await uploadImage({
      data: img?.data || img?.base64 || img,
      fileName: img?.fileName || img?.name,
      mimeType: img?.mimeType || img?.mime_type,
      accessToken,
      filesEndpoint: opts.filesEndpoint,
      selectionMethod: img?.selectionMethod,
    });
    if (!up.ok) {
      return { ok: false, error: up.error || 'image_upload_failed', upload: up };
    }
    uploadedImages.push(up);
  }

  let requirementsToken = opts.requirementsToken || null;
  if (!requirementsToken) {
    try {
      const req = await getChatRequirements(accessToken, cookies, deviceId);
      requirementsToken = req?.token || req?.chat_requirements?.token || null;
      if (!opts.proofToken && req?.proofofwork?.seed) {
        // Keep seed available for debugging; actual proof must be solved client-side.
      }
    } catch (e) {
      requirementsToken = null;
      var requirementsError = e?.message || String(e);
    }
  }

  const payload = buildConversationPayload(prompt, {
    model: opts.model,
    conversationId: opts.conversationId,
    parentMessageId: opts.parentMessageId,
    messageId: opts.messageId,
    timezone: opts.timezone,
    timezoneOffsetMin: opts.timezoneOffsetMin,
    extraPayload: opts.extraPayload,
    images: uploadedImages,
    systemHints: opts.systemHints || opts.system_hints,
    mode: opts.mode || opts.chatMode || opts.chat_mode,
    picture: opts.picture,
    picture_v2: opts.picture_v2,
    thinkingEffort: opts.thinkingEffort || opts.thinking_effort,
  });
  const systemHints = normalizeSystemHints({
    systemHints: payload.system_hints,
    mode: opts.mode || opts.chatMode || opts.chat_mode,
    picture: opts.picture,
    picture_v2: opts.picture_v2,
  });
  const isPictureMode = systemHints.includes('picture_v2');

  const headers = {
    Accept: 'text/event-stream',
    'Content-Type': 'application/json',
    Authorization: `Bearer ${accessToken}`,
    Origin: CHATGPT_ORIGIN,
    Referer: `${CHATGPT_ORIGIN}/`,
    'oai-device-id': deviceId,
    'oai-language': 'en-US',
    'oai-session-id': sessionId,
    'oai-client-version': opts.clientVersion || DEFAULT_CLIENT_VERSION,
    'oai-client-build-number': opts.buildNumber || DEFAULT_BUILD_NUMBER,
    'x-openai-target-path': new URL(endpoint).pathname,
    'x-openai-target-route': new URL(endpoint).pathname,
    ...(requirementsToken
      ? { 'openai-sentinel-chat-requirements-token': requirementsToken }
      : {}),
    ...(opts.proofToken ? { 'openai-sentinel-proof-token': opts.proofToken } : {}),
    ...(opts.turnstileToken
      ? { 'openai-sentinel-turnstile-token': opts.turnstileToken }
      : {}),
    ...(opts.conduitToken ? { 'x-conduit-token': opts.conduitToken } : {}),
    ...(opts.extraHeaders && typeof opts.extraHeaders === 'object' ? opts.extraHeaders : {}),
  };

  let resp;
  try {
    ({ resp } = await chatgptFetch(endpoint, {
      method: 'POST',
      headers,
      body: payload,
      cookies,
    }));
  } catch (e) {
    return { ok: false, error: e?.message || 'network_error', endpoint };
  }

  if (!resp.ok) {
    const raw = await resp.text().catch(() => '');
    let detail = raw.slice(0, 500);
    try {
      const j = JSON.parse(raw);
      detail = j?.detail || j?.error?.message || j?.error || detail;
      if (typeof detail !== 'string') detail = JSON.stringify(detail);
    } catch {
      /* keep text */
    }
    return {
      ok: false,
      error: detail || `http_${resp.status}`,
      status: resp.status,
      endpoint,
      uploadedImages,
      images: [],
      files: [],
      requirementsError: typeof requirementsError !== 'undefined' ? requirementsError : null,
    };
  }

  try {
    const parsed = await parseConversationSse(resp);
    let assetRefs = parsed.assetRefs || [];
    let assistantText = parsed.text || '';

    // Conversation detail often has richer file/image pointers than the SSE alone.
    if (parsed.conversationId) {
      const excludeFileIds = new Set(
        uploadedImages.map((u) => u.fileId || u.file_id).filter(Boolean),
      );
      const convo = await fetchConversationDetail(parsed.conversationId, accessToken, cookies);
      if (convo) {
        assetRefs = mergeAssetRefs(
          assetRefs.filter((r) => !r.fileId || !excludeFileIds.has(r.fileId)),
          collectAssetRefsFromConversation(convo, { excludeFileIds }),
        );
        const detailText = extractAssistantTextFromConversation(convo);
        if (detailText) assistantText = detailText;
      } else {
        assetRefs = assetRefs.filter((r) => !r.fileId || !excludeFileIds.has(r.fileId));
      }

      // picture_v2: SSE ends early; poll until generated images land in the conversation.
      if (isPictureMode && !conversationImageRefsReady(assetRefs)) {
        const waited = await waitForConversationImages(
          parsed.conversationId,
          accessToken,
          cookies,
          {
            excludeFileIds,
            maxAttempts: Number(opts.picturePollAttempts) > 0
              ? Number(opts.picturePollAttempts)
              : 36,
            intervalMs: Number(opts.picturePollIntervalMs) > 0
              ? Number(opts.picturePollIntervalMs)
              : 2500,
          },
        );
        assetRefs = mergeAssetRefs(assetRefs, waited.refs || []);
        if (waited.text) assistantText = waited.text;
      }
    }

    const downloadAssets = opts.downloadAssets !== false;
    const resolved = await resolveAssetRefs(assetRefs, {
      accessToken,
      cookies,
      downloadAssets,
      conversationId: parsed.conversationId,
    });

    return {
      ok: true,
      text: assistantText || '',
      conversationId: parsed.conversationId,
      messageId: parsed.messageId,
      endpoint,
      model: payload.model,
      systemHints,
      uploadedImages,
      images: resolved.images,
      files: resolved.files,
      requirementsError: typeof requirementsError !== 'undefined' ? requirementsError : null,
    };
  } catch (e) {
    return {
      ok: false,
      error: e?.message || 'sse_parse_failed',
      endpoint,
      uploadedImages,
      images: [],
      files: [],
    };
  }
}

const CHATGPT_AGENT_BASE = 'http://127.0.0.1:1994';
const CHATGPT_POLL_TIMEOUT_S = 25;
const CHATGPT_POLL_ALARM = 'f2api-chatgpt-poll';

let chatgptPollStop = false;
let chatgptPollRunning = false;
let chatgptWorkerId = '';

async function chatgptGetWorkerId() {
  if (chatgptWorkerId) return chatgptWorkerId;
  const data = await chrome.storage.local.get(['profileId', 'chatgptWorkerId']);
  chatgptWorkerId =
    data.chatgptWorkerId
    || data.profileId
    || (crypto?.randomUUID?.() || `cgpt-${Date.now()}`);
  await chrome.storage.local.set({ chatgptWorkerId });
  return chatgptWorkerId;
}

async function chatgptPostResult(jobId, result) {
  await fetch(`${CHATGPT_AGENT_BASE}/api/internal/chatgpt/result`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jobId,
      result: result && typeof result === 'object' ? result : { ok: false, error: 'empty_result' },
      error: result?.ok ? null : (result?.error || null),
    }),
  });
}

async function chatgptHandleJobs(jobs) {
  for (const job of jobs || []) {
    const jobId = job?.jobId;
    if (!jobId) continue;
    try {
      const result = await sendConversation(job.params || {});
      await chatgptPostResult(jobId, result);
    } catch (e) {
      try {
        await chatgptPostResult(jobId, { ok: false, error: e?.message || String(e) });
      } catch {
        /* ignore */
      }
    }
  }
}

async function chatgptPollOnce() {
  const workerId = await chatgptGetWorkerId();
  const label = 'chatgpt-ext';
  const url =
    `${CHATGPT_AGENT_BASE}/api/internal/chatgpt/poll`
    + `?workerId=${encodeURIComponent(workerId)}`
    + `&label=${encodeURIComponent(label)}`
    + `&timeout=${CHATGPT_POLL_TIMEOUT_S}`;
  const resp = await fetch(url, { method: 'GET' });
  if (!resp.ok) {
    throw new Error(`chatgpt_poll_http_${resp.status}`);
  }
  const data = await resp.json().catch(() => ({}));
  const jobs = Array.isArray(data?.jobs) ? data.jobs : [];
  if (jobs.length) await chatgptHandleJobs(jobs);
}

async function chatgptPollLoop() {
  if (chatgptPollRunning) return;
  chatgptPollRunning = true;
  chatgptPollStop = false;
  let errors = 0;
  while (!chatgptPollStop) {
    try {
      await chatgptPollOnce();
      errors = 0;
    } catch (e) {
      errors += 1;
      const delay = Math.min(10_000, 500 * (2 ** Math.min(errors, 4)));
      console.warn('[Flow2API] chatgpt poll error:', e?.message || e, `retry in ${delay}ms`);
      await new Promise((r) => setTimeout(r, delay));
    }
  }
  chatgptPollRunning = false;
}

function startPollLoop() {
  chatgptPollStop = false;
  if (!chatgptPollRunning) {
    chatgptPollLoop().catch((e) => console.warn('[Flow2API] chatgpt poll loop died', e));
  }
  try {
    chrome.alarms.create(CHATGPT_POLL_ALARM, { periodInMinutes: 0.5 });
  } catch {
    /* ignore */
  }
}

function stopPollLoop() {
  chatgptPollStop = true;
  try {
    chrome.alarms.clear(CHATGPT_POLL_ALARM);
  } catch {
    /* ignore */
  }
}

self.ChatGPTExt = {
  getChatgptCookies,
  getChatgptCookieStatus,
  openChatgptTab,
  getAccessToken,
  getChatRequirements,
  uploadImage,
  buildConversationPayload,
  normalizeSystemHints,
  sendConversation,
  startPollLoop,
  stopPollLoop,
  DEFAULT_CONVERSATION_PATH,
  CHATGPT_ORIGIN,
};
