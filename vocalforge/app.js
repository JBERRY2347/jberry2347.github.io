/* VocalForge — lyrics in, sung vocals out.
   Everything runs in the browser. The only network calls go to the ElevenLabs Music API
   (or a relay you configure) using the key you paste into Settings. */
'use strict';

const API_DEFAULT = 'https://api.elevenlabs.io';
const OUTPUT_FORMAT = 'mp3_44100_128';
const LS = { key: 'vf.apiKey', base: 'vf.apiBase', draft: 'vf.draft' };

// Limits documented for the ElevenLabs Music API.
const LIMITS = {
  chunkMinMs: 3000,
  chunkMaxMs: 120000,
  totalMinMs: 3000,
  totalMaxMs: 600000,
  maxChunks: 30,
  maxLinesPerChunk: 30,
  maxLineChars: 200,
  maxPromptChars: 4100,
  maxStyles: 50,
};

const VOICES = {
  female: 'female lead vocal',
  male: 'male lead vocal',
  androgynous: 'androgynous lead vocal',
  duet: 'male and female duet vocals',
  choir: 'group vocals, choir',
  child: 'youthful, child-like lead vocal',
  'rap-male': 'male rap vocal, rhythmic delivery',
  'rap-female': 'female rap vocal, rhythmic delivery',
};
const OUTPUT_LABEL = { song: 'full song', acappella: 'vocals only', instrumental: 'instrumental' };
const TEMPOS = {
  slow: 'slow tempo, around 70 BPM',
  mid: 'mid tempo, around 95 BPM',
  upbeat: 'upbeat tempo, around 120 BPM',
  fast: 'fast tempo, around 140 BPM',
};

// ---------- Lyric parsing ----------

const TAG_RE = /^\s*\[([^\]]+)\]\s*$/;

/** "[Chorus 18s]" → { name: "Chorus", ms: 18000 }. Also "0:18", "(18s)", "Chorus: 18 sec". */
function parseTag(raw) {
  let name = raw.trim();
  let ms = null;
  const m = name.match(/^(.*?)[\s:(\-–]*(?:(\d+):(\d{2})|(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?)\s*\)?$/i);
  if (m) {
    name = m[1].trim() || 'Section';
    ms = m[2] !== undefined
      ? (parseInt(m[2], 10) * 60 + parseInt(m[3], 10)) * 1000
      : Math.round(parseFloat(m[4]) * 1000);
  }
  return { name, ms };
}

function kindOf(name) {
  const n = name.toLowerCase();
  if (n.includes('pre')) return 'pre-chorus';
  if (n.includes('chorus') || n.includes('hook') || n.includes('refrain')) return 'chorus';
  if (n.includes('verse')) return 'verse';
  if (n.includes('bridge')) return 'bridge';
  if (n.includes('intro')) return 'intro';
  if (n.includes('outro') || n.includes('ending')) return 'outro';
  if (n.includes('drop')) return 'drop';
  if (n.includes('solo') || n.includes('break') || n.includes('instrumental')) return 'instrumental';
  return 'section';
}

/** Lyrics text → [{ name, kind, explicitMs, lines }]. Blank lines are ignored; untagged lines open a Verse. */
function parseLyrics(text) {
  const sections = [];
  let cur = null;
  for (const rawLine of String(text || '').split(/\r?\n/)) {
    const line = rawLine.trim();
    const tag = line.match(TAG_RE);
    if (tag) {
      const t = parseTag(tag[1]);
      cur = { name: t.name, kind: kindOf(t.name), explicitMs: t.ms, lines: [] };
      sections.push(cur);
      continue;
    }
    if (!line) continue;
    if (!cur) {
      cur = { name: 'Verse', kind: 'verse', explicitMs: null, lines: [] };
      sections.push(cur);
    }
    cur.lines.push(line);
  }
  return sections;
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Give every section a duration. Pinned sections keep theirs; the rest share what is left of totalMs by line count. */
function allocate(sections, totalMs) {
  const out = sections.map(s => ({ ...s }));
  const fixed = out.filter(s => s.explicitMs != null);
  const free = out.filter(s => s.explicitMs == null);
  fixed.forEach(s => { s.ms = clamp(Math.round(s.explicitMs / 100) * 100, LIMITS.chunkMinMs, LIMITS.chunkMaxMs); });
  const fixedSum = fixed.reduce((a, s) => a + s.ms, 0);
  const remaining = Math.max(0, totalMs - fixedSum);
  const weights = free.map(s => s.lines.length || (s.kind === 'intro' || s.kind === 'outro' ? 1.5 : 2));
  const wsum = weights.reduce((a, b) => a + b, 0) || 1;
  free.forEach((s, i) => {
    s.ms = clamp(Math.round(remaining * weights[i] / wsum / 100) * 100, LIMITS.chunkMinMs, LIMITS.chunkMaxMs);
  });
  return out;
}

function validateSections(sections) {
  const problems = [];
  if (!sections.length) problems.push('Write some lyrics first.');
  if (sections.length > LIMITS.maxChunks) problems.push(`Too many sections (${sections.length}); the API takes at most ${LIMITS.maxChunks}.`);
  for (const s of sections) {
    if (s.lines.length > LIMITS.maxLinesPerChunk) problems.push(`[${s.name}] has ${s.lines.length} lines; the limit is ${LIMITS.maxLinesPerChunk} per section. Split it with another tag.`);
    const long = s.lines.find(l => l.length > LIMITS.maxLineChars);
    if (long) problems.push(`A line in [${s.name}] is longer than ${LIMITS.maxLineChars} characters: “${long.slice(0, 40)}…”`);
    if (s.explicitMs != null && (s.explicitMs < LIMITS.chunkMinMs || s.explicitMs > LIMITS.chunkMaxMs)) {
      problems.push(`[${s.name}] is pinned to ${fmtSec(s.explicitMs)}; sections must be between 3s and 2:00.`);
    }
  }
  const total = sections.reduce((a, s) => a + (s.ms || 0), 0);
  if (sections.length && total > LIMITS.totalMaxMs) problems.push(`Total length ${fmtSec(total)} is over the 10 minute limit.`);
  return problems;
}

// ---------- Style building ----------

const splitList = s => String(s || '').split(/[,;\n]+/).map(x => x.trim()).filter(Boolean);
const uniq = arr => [...new Set(arr.map(x => x.trim()).filter(Boolean))];

function globalStyles(o) {
  const pos = [...splitList(o.genre), ...(o.output === 'instrumental' ? [] : [VOICES[o.voice] || VOICES.female]), ...o.moods];
  if (TEMPOS[o.tempo]) pos.push(TEMPOS[o.tempo]);
  pos.push(...splitList(o.extra));
  const neg = [...splitList(o.avoid)];
  if (o.output === 'acappella') {
    pos.push('a cappella', 'vocals only', 'no instruments', 'dry, clean lead vocal');
    neg.push('instruments', 'drums', 'bass', 'guitar', 'piano', 'synths', 'backing track', 'beat');
  } else if (o.output === 'instrumental') {
    pos.push('instrumental', 'no vocals');
    neg.push('vocals', 'singing', 'lyrics');
  } else {
    pos.push('sung vocals with full backing music', 'clear, intelligible lyrics');
  }
  return { pos: uniq(pos).slice(0, LIMITS.maxStyles), neg: uniq(neg).slice(0, LIMITS.maxStyles) };
}

function localStyles(s, acappella) {
  const empty = s.lines.length === 0;
  switch (s.kind) {
    case 'chorus': return ['chorus', 'big memorable hook', acappella ? 'stacked vocal harmonies' : 'full arrangement'];
    case 'pre-chorus': return ['pre-chorus', 'building tension'];
    case 'verse': return ['verse', 'storytelling delivery'];
    case 'bridge': return ['bridge', 'contrast', 'emotional lift'];
    case 'drop': return ['drop', 'high energy'];
    case 'intro': return empty ? ['intro', acappella ? 'wordless vocal humming, no lyrics' : 'instrumental intro, no vocals'] : ['intro', 'sparse opening'];
    case 'outro': return empty ? ['outro', acappella ? 'wordless vocal fade out' : 'instrumental outro, no vocals'] : ['outro', 'fading ending'];
    case 'instrumental': return empty ? [acappella ? 'wordless vocalising, no lyrics' : 'instrumental break, no vocals'] : ['break'];
    default: return empty ? [acappella ? 'wordless vocalising' : 'instrumental passage'] : [];
  }
}

// ---------- Request building ----------

/** Build the JSON body for POST /v1/music from the form state. */
function buildRequest(o) {
  const sections = allocate(parseLyrics(o.lyrics), o.lengthSec * 1000);
  const problems = validateSections(sections);
  const g = globalStyles(o);
  const totalMs = sections.reduce((a, s) => a + s.ms, 0);

  if (o.output === 'instrumental') {
    const lengthMs = clamp(o.lengthSec * 1000, LIMITS.totalMinMs, LIMITS.totalMaxMs);
    const lines = [`Instrumental track. Style: ${g.pos.join(', ')}.`];
    if (g.neg.length) lines.push(`Avoid: ${g.neg.join(', ')}.`);
    if (o.lyrics.trim()) lines.push('Mood and theme reference (do not sing these words):', '', o.lyrics.trim());
    let prompt = lines.join('\n');
    if (prompt.length > LIMITS.maxPromptChars) prompt = prompt.slice(0, LIMITS.maxPromptChars);
    return {
      problems: g.pos.length ? [] : ['Describe the style you want first.'],
      totalMs: lengthMs,
      body: { model_id: o.model, prompt, music_length_ms: lengthMs, force_instrumental: true },
    };
  }

  if (o.mode === 'freeform') {
    const lines = [];
    lines.push(`Style: ${g.pos.join(', ')}.`);
    if (g.neg.length) lines.push(`Avoid: ${g.neg.join(', ')}.`);
    lines.push('Sing these lyrics exactly as written, keeping the section order:', '', o.lyrics.trim());
    const prompt = lines.join('\n');
    const lengthMs = clamp(o.lengthSec * 1000, LIMITS.totalMinMs, LIMITS.totalMaxMs);
    const probs = [];
    if (!o.lyrics.trim()) probs.push('Write some lyrics first.');
    if (prompt.length > LIMITS.maxPromptChars) probs.push(`The prompt is ${prompt.length} characters; the limit is ${LIMITS.maxPromptChars}. Shorten the lyrics or use Structured mode.`);
    return {
      problems: probs,
      totalMs: lengthMs,
      body: { model_id: o.model, prompt, music_length_ms: lengthMs, force_instrumental: false },
    };
  }

  let plan;
  if (o.model === 'music_v1') {
    plan = {
      positive_global_styles: g.pos,
      negative_global_styles: g.neg,
      sections: sections.map(s => ({
        section_name: s.name,
        positive_local_styles: localStyles(s, o.output === 'acappella'),
        negative_local_styles: [],
        duration_ms: s.ms,
        lines: s.lines,
      })),
    };
  } else {
    plan = {
      chunks: sections.map(s => ({
        text: s.lines.join('\n'),
        duration_ms: s.ms,
        positive_styles: uniq([...localStyles(s, o.output === 'acappella'), ...g.pos]).slice(0, LIMITS.maxStyles),
        negative_styles: g.neg,
        context_adherence: 'high',
      })),
    };
  }
  const body = { model_id: o.model, composition_plan: plan, force_instrumental: false };
  if (o.model === 'music_v1') body.respect_sections_durations = true;
  const summary = sections.map((s, i) => ({
    name: s.name, lines: s.lines, ms: s.ms,
    pos: plan.chunks ? plan.chunks[i].positive_styles : uniq([...plan.sections[i].positive_local_styles, ...g.pos]),
    neg: plan.chunks ? plan.chunks[i].negative_styles : g.neg,
  }));
  return { problems, totalMs, body, sections: summary };
}

// ---------- API ----------

function apiBase() {
  return (localStorage.getItem(LS.base) || API_DEFAULT).replace(/\/+$/, '');
}
function apiKey() {
  return (localStorage.getItem(LS.key) || '').trim();
}

async function apiError(res) {
  let msg = `${res.status} ${res.statusText}`;
  let text = '';
  try { text = await res.text(); } catch { /* ignore */ }
  try {
    const j = JSON.parse(text);
    const d = j.detail ?? j.error ?? j.message ?? j;
    if (typeof d === 'string') msg += `: ${d}`;
    else if (Array.isArray(d)) msg += ': ' + d.map(e => `${(e.loc || []).join('.')} ${e.msg || JSON.stringify(e)}`).join('; ');
    else if (d && typeof d === 'object') msg += `: ${d.message || d.msg || JSON.stringify(d)}` + (d.status ? ` (${d.status})` : '');
  } catch {
    if (text) msg += `: ${text.slice(0, 300)}`;
  }
  if (res.status === 401) msg += '\nThe API key was rejected. Check it in Settings.';
  if (res.status === 402 || /quota|credit|limit/i.test(msg)) msg += '\nYour ElevenLabs plan may be out of credits, or music is not included in it.';
  return new Error(msg);
}

function networkError(err) {
  if (err.name === 'AbortError') return new Error('Cancelled.');
  if (err instanceof TypeError) {
    return new Error(`Could not reach ${apiBase()}. If you are online, the browser refused the cross-origin call. ` +
      'Point the API host in Settings at your own relay (see the README) and try again.');
  }
  return err;
}

async function callApi(path, init) {
  const key = apiKey();
  if (!key) throw new Error('Add your ElevenLabs API key in Settings first.');
  let res;
  try {
    res = await fetch(apiBase() + path, {
      ...init,
      headers: { 'xi-api-key': key, ...(init.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...(init.headers || {}) },
    });
  } catch (err) {
    throw networkError(err);
  }
  if (!res.ok) throw await apiError(res);
  return res;
}

/** Generate audio. Returns { blob, songId, storeNote }.
    With `store`, the detailed endpoint is asked to keep the song on ElevenLabs (store_for_inpainting) so
    single sections can be rewritten later; that gives back a song_id. If ElevenLabs refuses that request
    with a 4xx that is not about the key or credits, we fall back to the plain endpoint and say so. */
async function compose(body, signal, store) {
  let storeNote = null;
  if (store) {
    try {
      const res = await callApi(`/v1/music/detailed?output_format=${OUTPUT_FORMAT}`, {
        method: 'POST', body: JSON.stringify({ ...body, store_for_inpainting: true }),
        headers: { Accept: 'multipart/mixed, application/json' }, signal,
      });
      const { json, blob } = await parseDetailed(res);
      if (!blob || !blob.size) throw new Error('The API returned no audio in its detailed response.');
      const songId = findKey(json, 'song_id');
      return { blob, songId: typeof songId === 'string' ? songId : null, detail: json, storeNote: songId ? null : 'ElevenLabs did not return a song id, so this take cannot be rewritten by section.' };
    } catch (err) {
      const code = (err.message || '').match(/^(\d{3}) /);
      if (!code || ['401', '402', '429'].includes(code[1])) throw err;
      storeNote = `Not stored for rewrites: ElevenLabs refused the stored generation (${err.message.split('\n')[0]}). Generated without storing instead.`;
    }
  }
  const res = await callApi(`/v1/music?output_format=${OUTPUT_FORMAT}`, {
    method: 'POST', body: JSON.stringify(body), headers: { Accept: 'audio/mpeg' }, signal,
  });
  const blob = await res.blob();
  if (!blob.size) throw new Error('The API returned an empty audio file.');
  return { blob, songId: null, detail: null, storeNote };
}

/** Upload an existing take so ElevenLabs can use it as inpainting source. Returns its song_id. */
async function uploadForInpainting(blob) {
  const fd = new FormData();
  fd.append('file', blob, 'take.mp3');
  const res = await callApi('/v1/music/upload', { method: 'POST', body: fd });
  const json = await res.json();
  const songId = findKey(json, 'song_id');
  if (typeof songId !== 'string') throw new Error('The upload succeeded but no song id came back: ' + JSON.stringify(json).slice(0, 200));
  return songId;
}

/** Depth-first search for a key in nested JSON. */
function findKey(obj, key) {
  if (!obj || typeof obj !== 'object') return undefined;
  if (Object.prototype.hasOwnProperty.call(obj, key)) return obj[key];
  for (const v of Object.values(obj)) {
    const hit = findKey(v, key);
    if (hit !== undefined) return hit;
  }
  return undefined;
}

const TEXT = new TextDecoder();
function indexOfBytes(hay, needle, from) {
  outer: for (let i = from; i <= hay.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++) if (hay[i + j] !== needle[j]) continue outer;
    return i;
  }
  return -1;
}

/** Split the detailed endpoint's multipart/mixed (or JSON) response into { json, blob }. */
async function parseDetailed(res) {
  const ct = res.headers.get('content-type') || '';
  const buf = new Uint8Array(await res.arrayBuffer());
  const bm = ct.match(/boundary="?([^";]+)"?/i);
  if (!bm) {
    if (/json/i.test(ct)) {
      const json = JSON.parse(TEXT.decode(buf));
      const b64 = findKey(json, 'audio_base64') ?? findKey(json, 'audio_base_64') ?? findKey(json, 'audio');
      if (typeof b64 === 'string') {
        const bin = atob(b64.replace(/^data:[^,]*,/, ''));
        const bytes = Uint8Array.from(bin, ch => ch.charCodeAt(0));
        return { json, blob: new Blob([bytes], { type: 'audio/mpeg' }) };
      }
      throw new Error('The detailed response had no audio in it.');
    }
    throw new Error('Unexpected response type from the detailed endpoint: ' + ct);
  }
  const delim = new TextEncoder().encode('--' + bm[1]);
  let json = null, blob = null;
  let at = indexOfBytes(buf, delim, 0);
  while (at !== -1) {
    const start = at + delim.length;
    if (buf[start] === 0x2d && buf[start + 1] === 0x2d) break; // closing delimiter
    const next = indexOfBytes(buf, delim, start);
    let part = buf.subarray(start, next === -1 ? buf.length : next);
    let s = 0; if (part[0] === 13 && part[1] === 10) s = 2; else if (part[0] === 10) s = 1;
    let e = part.length; if (part[e - 2] === 13 && part[e - 1] === 10) e -= 2; else if (part[e - 1] === 10) e -= 1;
    part = part.subarray(s, e);
    let hdrEnd = indexOfBytes(part, [13, 10, 13, 10], 0), skip = 4;
    if (hdrEnd === -1) { hdrEnd = indexOfBytes(part, [10, 10], 0); skip = 2; }
    const headers = hdrEnd === -1 ? '' : TEXT.decode(part.subarray(0, hdrEnd)).toLowerCase();
    const body = hdrEnd === -1 ? part : part.subarray(hdrEnd + skip);
    const type = (headers.match(/content-type:\s*([^;\r\n]+)/) || [])[1] || '';
    if (/json/.test(type) || (!type && body[0] === 0x7b)) {
      try { json = JSON.parse(TEXT.decode(body)); } catch { /* not JSON after all */ }
    } else if (/audio|octet/.test(type) || (!type && body.length > 64)) {
      blob = new Blob([body], { type: type.startsWith('audio') ? type : 'audio/mpeg' });
    }
    at = next;
  }
  return { json, blob };
}

async function draftPlan(prompt, lengthMs, model) {
  const res = await callApi('/v1/music/plan', {
    method: 'POST', body: JSON.stringify({ prompt, music_length_ms: lengthMs, model_id: model }),
  });
  return res.json();
}

/** Turn a plan from /v1/music/plan (v1 sections or v2 chunks) back into tagged lyrics text. */
function planToLyrics(plan) {
  const p = plan && plan.composition_plan ? plan.composition_plan : plan;
  const parts = [];
  if (p && Array.isArray(p.sections)) {
    for (const s of p.sections) {
      parts.push(`[${s.section_name || 'Section'} ${fmtSec(s.duration_ms || 0)}]`, ...(s.lines || []), '');
    }
  } else if (p && Array.isArray(p.chunks)) {
    p.chunks.forEach((c, i) => {
      const text = c.text || (c.lines ? c.lines.join('\n') : '');
      parts.push(`[Part ${i + 1} ${fmtSec(c.duration_ms || 0)}]`, ...(text ? text.split(/\r?\n/) : []), '');
    });
  } else {
    throw new Error('The plan came back in a shape I do not recognise: ' + JSON.stringify(plan).slice(0, 200));
  }
  return parts.join('\n').trim() + '\n';
}

// ---------- Storage (IndexedDB for takes) ----------

const db = {
  _p: null,
  open() {
    if (this._p) return this._p;
    this._p = new Promise((resolve, reject) => {
      const req = indexedDB.open('vocalforge', 1);
      req.onupgradeneeded = () => {
        const store = req.result.createObjectStore('takes', { keyPath: 'id' });
        store.createIndex('createdAt', 'createdAt');
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return this._p;
  },
  async tx(mode, fn) {
    const d = await this.open();
    return new Promise((resolve, reject) => {
      const t = d.transaction('takes', mode);
      const r = fn(t.objectStore('takes'));
      t.oncomplete = () => resolve(r && r.result !== undefined ? r.result : undefined);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error);
    });
  },
  add(take) { return this.tx('readwrite', s => s.put(take)); },
  del(id) { return this.tx('readwrite', s => s.delete(id)); },
  async all() {
    const d = await this.open();
    return new Promise((resolve, reject) => {
      const req = d.transaction('takes').objectStore('takes').getAll();
      req.onsuccess = () => resolve(req.result.sort((a, b) => b.createdAt - a.createdAt));
      req.onerror = () => reject(req.error);
    });
  },
};

// ---------- UI ----------

const $ = id => document.getElementById(id);
const fmtSec = ms => {
  const s = Math.round(ms / 1000);
  return s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : `${s}s`;
};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const el = {
  lyrics: $('lyrics'), structure: $('structure'), genre: $('genre'), voice: $('voice'), tempo: $('tempo'),
  moods: $('moods'), extra: $('extra'), avoid: $('avoid'), length: $('length'), lengthLabel: $('length-label'),
  lengthHint: $('length-hint'), model: $('model'), mode: $('mode'), generate: $('generate'), cancel: $('cancel'),
  status: $('status'), error: $('error'), takes: $('takes'), takesEmpty: $('takes-empty'), takesCount: $('takes-count'),
  settings: $('settings'), apiKey: $('api-key'), apiBase: $('api-base'), keyCheck: $('key-check'),
  banner: $('setup-banner'), topic: $('topic'), draftBtn: $('draft-btn'), store: $('store'),
};

function formState() {
  return {
    lyrics: el.lyrics.value,
    genre: el.genre.value,
    voice: el.voice.value,
    tempo: el.tempo.value,
    moods: [...el.moods.querySelectorAll('[aria-pressed="true"]')].map(b => b.dataset.mood),
    extra: el.extra.value,
    avoid: el.avoid.value,
    output: document.querySelector('input[name=output]:checked').value,
    lengthSec: parseInt(el.length.value, 10),
    model: el.model.value,
    mode: el.mode.value,
    store: el.store.checked,
  };
}

function saveDraft() {
  try { localStorage.setItem(LS.draft, JSON.stringify(formState())); } catch { /* storage full or blocked */ }
}
function loadDraft() {
  let d;
  try { d = JSON.parse(localStorage.getItem(LS.draft) || 'null'); } catch { d = null; }
  if (!d) return;
  el.lyrics.value = d.lyrics || '';
  el.genre.value = d.genre || '';
  if (d.voice) el.voice.value = d.voice;
  el.tempo.value = d.tempo || '';
  el.extra.value = d.extra || '';
  el.avoid.value = d.avoid || '';
  if (d.lengthSec) el.length.value = d.lengthSec;
  if (d.model) el.model.value = d.model;
  if (d.mode) el.mode.value = d.mode;
  if (typeof d.store === 'boolean') el.store.checked = d.store;
  for (const b of el.moods.querySelectorAll('.chip')) b.setAttribute('aria-pressed', String((d.moods || []).includes(b.dataset.mood)));
  const out = document.querySelector(`input[name=output][value=${['song', 'acappella', 'instrumental'].includes(d.output) ? d.output : 'song'}]`);
  if (out) out.checked = true;
}

function renderStructure() {
  const o = formState();
  el.lengthLabel.textContent = fmtSec(o.lengthSec * 1000);
  const sections = allocate(parseLyrics(o.lyrics), o.lengthSec * 1000);
  const problems = validateSections(sections);
  if (!sections.length) {
    el.structure.innerHTML = '';
    el.lengthHint.textContent = 'Sections share this length unless you pin them with a tag like [Chorus 18s].';
    return;
  }
  const total = sections.reduce((a, s) => a + s.ms, 0);
  el.structure.innerHTML = sections.map(s =>
    `<span class="sec${s.explicitMs != null ? ' pinned' : ''}${s.lines.length ? '' : ' instr'}"><b>${esc(s.name)}</b> · ${s.lines.length ? s.lines.length + (s.lines.length === 1 ? ' line' : ' lines') : 'no words'} · ${fmtSec(s.ms)}</span>`
  ).join('') +
    `<span class="total">= ${fmtSec(total)} over ${sections.length} section${sections.length === 1 ? '' : 's'}</span>` +
    problems.map(p => `<span class="problem">${esc(p)}</span>`).join('');
  el.lengthHint.textContent = o.output === 'instrumental'
    ? 'Instrumental tracks are generated from the style description at this length; lyrics are only used as a theme hint.'
    : o.mode === 'freeform'
    ? 'In freeform mode the model picks the structure and aims for this total length.'
    : (total !== o.lengthSec * 1000 ? `Pinned sections change the total to ${fmtSec(total)}.` : 'Sections share this length unless you pin them with a tag like [Chorus 18s].');
}

function showError(msg) {
  el.error.textContent = msg;
  el.error.hidden = !msg;
}
function setStatus(msg, cls = '') {
  el.status.textContent = msg;
  el.status.className = 'status ' + cls;
}

let running = null;
async function generate() {
  showError('');
  if (!apiKey()) { openSettings(); return; }
  const o = formState();
  const req = buildRequest(o);
  if (req.problems.length) { showError(req.problems.join('\n')); return; }

  const ctrl = new AbortController();
  running = ctrl;
  el.generate.disabled = true;
  el.cancel.hidden = false;
  const started = Date.now();
  const tick = setInterval(() => setStatus(`Generating ${fmtSec(req.totalMs)} of audio… ${Math.round((Date.now() - started) / 1000)}s`, 'busy'), 1000);
  setStatus(`Generating ${fmtSec(req.totalMs)} of audio…`, 'busy');
  try {
    const store = el.store.checked && o.output !== 'instrumental';
    const { blob, songId, storeNote } = await compose(req.body, ctrl.signal, store);
    const firstLine = parseLyrics(o.lyrics).flatMap(s => s.lines)[0] || '';
    const take = {
      id: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now() + Math.random())),
      createdAt: Date.now(),
      title: firstLine.slice(0, 60) || 'Untitled take',
      model: o.model,
      mode: o.mode,
      output: o.output,
      lyrics: o.lyrics,
      sections: req.sections || null,
      songId: songId || null,
      plannedMs: req.totalMs,
      request: req.body,
      mime: blob.type || 'audio/mpeg',
      blob,
    };
    await db.add(take);
    await renderTakes();
    setStatus(`Done in ${Math.round((Date.now() - started) / 1000)}s. Your take is on the right.` + (storeNote ? ' ' + storeNote : ''), storeNote ? '' : 'ok');
  } catch (err) {
    setStatus('');
    showError(err.message || String(err));
  } finally {
    clearInterval(tick);
    running = null;
    el.generate.disabled = false;
    el.cancel.hidden = true;
  }
}

const urls = new Map();
function urlFor(take) {
  if (!urls.has(take.id)) urls.set(take.id, URL.createObjectURL(take.blob));
  return urls.get(take.id);
}

async function renderTakes() {
  let takes = [];
  try { takes = await db.all(); } catch (err) { el.takes.innerHTML = `<p class="error">Could not open local storage: ${esc(err.message)}</p>`; return; }
  el.takesEmpty.hidden = takes.length > 0;
  el.takesCount.textContent = takes.length ? `(${takes.length})` : '';
  el.takes.innerHTML = takes.map(t => `
    <article class="take" data-id="${esc(t.id)}">
      <div class="head">
        <h3>${esc(t.title)}</h3>
        <span class="meta">${esc(OUTPUT_LABEL[t.output] || t.output)} · ${esc(t.model)} · ${fmtSec(t.plannedMs)} · ${new Date(t.createdAt).toLocaleString()}${t.songId ? ' · <span class="badge">editable</span>' : ''}</span>
      </div>
      <audio controls preload="metadata" src="${urlFor(t)}"></audio>
      <div class="tools">
        <a class="btn ghost small" href="${urlFor(t)}" download="${esc(fileName(t))}">Download MP3</a>
        ${canRewrite(t) ? '<button type="button" class="btn ghost small" data-act="rewrite">Rewrite a section</button>' : ''}
        <button type="button" class="btn ghost small" data-act="reuse">Load settings</button>
        <button type="button" class="btn ghost small danger" data-act="delete">Delete</button>
      </div>
      <details><summary>Request sent to the API</summary><pre>${esc(JSON.stringify(t.request, null, 2))}</pre></details>
    </article>`).join('');
}

function fileName(t) {
  const slug = t.title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'take';
  const d = new Date(t.createdAt);
  const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}-${String(d.getHours()).padStart(2, '0')}${String(d.getMinutes()).padStart(2, '0')}`;
  return `vocalforge-${slug}-${stamp}.mp3`;
}

el.takes.addEventListener('click', async e => {
  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  const art = btn.closest('.take');
  const id = art.dataset.id;
  if (btn.dataset.act === 'delete') {
    if (!confirm('Delete this take? This cannot be undone.')) return;
    await db.del(id);
    if (urls.has(id)) { URL.revokeObjectURL(urls.get(id)); urls.delete(id); }
    renderTakes();
  } else if (btn.dataset.act === 'rewrite') {
    const takes = await db.all();
    const t = takes.find(x => x.id === id);
    if (t) openRewrite(t);
  } else if (btn.dataset.act === 'reuse') {
    const takes = await db.all();
    const t = takes.find(x => x.id === id);
    if (!t) return;
    el.model.value = t.model;
    el.mode.value = t.mode;
    document.querySelector(`input[name=output][value=${OUTPUT_LABEL[t.output] ? t.output : 'song'}]`).checked = true;
    const req = t.request;
    if (t.lyrics != null) {
      el.lyrics.value = t.lyrics;
    } else if (req.force_instrumental) {
      /* nothing to restore */
    } else if (req.prompt) {
      const i = req.prompt.indexOf('\n\n');
      if (i > -1) el.lyrics.value = req.prompt.slice(i + 2);
    } else if (req.composition_plan) {
      el.lyrics.value = planToLyrics(req.composition_plan);
    }
    renderStructure();
    saveDraft();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
});

// Section tag chips insert at the cursor.
$('tag-chips').addEventListener('click', e => {
  const b = e.target.closest('[data-tag]');
  if (!b) return;
  const ta = el.lyrics;
  const before = ta.value.slice(0, ta.selectionStart);
  const after = ta.value.slice(ta.selectionEnd);
  const prefix = before && !before.endsWith('\n') ? (before.endsWith('\n\n') ? '' : '\n') + (before.endsWith('\n') ? '' : '\n') : (before.endsWith('\n') && !before.endsWith('\n\n') ? '\n' : '');
  const insert = prefix + b.dataset.tag + '\n';
  ta.value = before + insert + after;
  const pos = before.length + insert.length;
  ta.setSelectionRange(pos, pos);
  ta.focus();
  renderStructure();
  saveDraft();
});

el.moods.addEventListener('click', e => {
  const b = e.target.closest('.chip');
  if (!b) return;
  b.setAttribute('aria-pressed', String(b.getAttribute('aria-pressed') !== 'true'));
  saveDraft();
});

for (const input of [el.lyrics, el.length, el.mode]) input.addEventListener('input', () => { renderStructure(); saveDraft(); });
for (const input of [el.genre, el.voice, el.tempo, el.extra, el.avoid, el.model]) input.addEventListener('input', saveDraft);
el.store.addEventListener('change', saveDraft);
for (const r of document.querySelectorAll('input[name=output]')) r.addEventListener('change', () => { renderStructure(); saveDraft(); });

el.generate.addEventListener('click', generate);
el.cancel.addEventListener('click', () => running && running.abort());

el.draftBtn.addEventListener('click', async () => {
  showError('');
  if (!apiKey()) { openSettings(); return; }
  const o = formState();
  const topic = el.topic.value.trim();
  if (!topic) { el.topic.focus(); return; }
  if (el.lyrics.value.trim() && !confirm('Replace the lyrics in the editor with a fresh draft?')) return;
  const g = globalStyles(o);
  const prompt = `${g.pos.join(', ')}. A song about ${topic}.` + (g.neg.length ? ` Avoid: ${g.neg.join(', ')}.` : '');
  el.draftBtn.disabled = true;
  setStatus('Drafting lyrics…', 'busy');
  try {
    const plan = await draftPlan(prompt, clamp(o.lengthSec * 1000, LIMITS.totalMinMs, LIMITS.totalMaxMs), o.model);
    el.lyrics.value = planToLyrics(plan);
    renderStructure();
    saveDraft();
    setStatus('Draft loaded. Edit anything, then generate.', 'ok');
  } catch (err) {
    setStatus('');
    showError(err.message || String(err));
  } finally {
    el.draftBtn.disabled = false;
  }
});

// ---------- Rewriting sections (inpainting) ----------

/** Sections of a take with their lengths and styles, derived for takes made before this was stored. */
function sectionsOf(t) {
  if (Array.isArray(t.sections) && t.sections.length) return t.sections;
  const chunks = t.request && t.request.composition_plan && t.request.composition_plan.chunks;
  if (!chunks || t.lyrics == null) return null;
  const parsed = parseLyrics(t.lyrics);
  if (parsed.length !== chunks.length) return null;
  return parsed.map((s, i) => ({ name: s.name, lines: s.lines, ms: chunks[i].duration_ms, pos: chunks[i].positive_styles || [], neg: chunks[i].negative_styles || [] }));
}

function canRewrite(t) {
  return t.output !== 'instrumental' && t.model !== 'music_v1' && !!sectionsOf(t);
}

function sectionsToLyrics(sections) {
  return sections.map(s => `[${s.name} ${fmtSec(s.ms)}]\n${s.lines.join('\n')}`).join('\n\n') + '\n';
}

let rwTake = null;
const rw = {
  dialog: $('rewrite'), sections: $('rw-sections'), cost: $('rw-cost'), status: $('rw-status'), error: $('rw-error'),
  upload: $('rw-upload'), title: $('rw-title'), go: $('rw-go'), cancel: $('rw-cancel'),
};

function openRewrite(t) {
  rwTake = t;
  const secs = sectionsOf(t);
  rw.title.textContent = t.title;
  rw.upload.hidden = !!t.songId;
  rw.sections.innerHTML = secs.map((s, i) => `
    <div class="rw-sec" data-i="${i}">
      <label class="rw-head"><input type="checkbox" class="rw-pick"> <b>${esc(s.name)}</b> <span class="meta">· ${fmtSec(s.ms)}${s.lines.length ? '' : ' · no words'}</span></label>
      <textarea class="rw-text" rows="${Math.min(12, Math.max(2, s.lines.length + 1))}" data-orig="${esc(s.lines.join('\n'))}">${esc(s.lines.join('\n'))}</textarea>
    </div>`).join('');
  rw.error.hidden = true;
  rw.status.textContent = '';
  rw.status.className = 'status';
  rw.go.disabled = false;
  updateRewriteCost();
  rw.dialog.showModal();
}

function rewritePicks() {
  return [...rw.sections.querySelectorAll('.rw-sec')].map(div => ({
    pick: div.querySelector('.rw-pick').checked,
    text: div.querySelector('.rw-text').value,
  }));
}

function updateRewriteCost() {
  if (!rwTake) return;
  const secs = sectionsOf(rwTake);
  const picks = rewritePicks();
  const redo = secs.filter((s, i) => picks[i].pick);
  const ms = redo.reduce((a, s) => a + s.ms, 0);
  rw.cost.textContent = redo.length
    ? `Regenerating ${redo.length} section${redo.length === 1 ? '' : 's'} (${fmtSec(ms)} of audio, the only part charged). The other ${fmtSec(secs.reduce((a, s) => a + s.ms, 0) - ms)} is kept as it is.`
    : 'Tick the sections to regenerate, or just edit their lines.';
}

rw.sections.addEventListener('input', e => {
  const ta = e.target.closest('.rw-text');
  if (ta) {
    const changed = ta.value.trim() !== ta.dataset.orig.trim();
    if (changed) ta.closest('.rw-sec').querySelector('.rw-pick').checked = true;
  }
  updateRewriteCost();
});
rw.cancel.addEventListener('click', () => rw.dialog.close());

rw.go.addEventListener('click', async () => {
  const t = rwTake;
  if (!t) return;
  const secs = sectionsOf(t);
  const picks = rewritePicks();
  rw.error.hidden = true;
  if (!picks.some(p => p.pick)) { rw.error.textContent = 'Tick at least one section to regenerate.'; rw.error.hidden = false; return; }
  const newSections = secs.map((s, i) => {
    const lines = picks[i].pick ? picks[i].text.split(/\r?\n/).map(l => l.trim()).filter(Boolean) : s.lines;
    return { ...s, lines };
  });
  const problems = [];
  newSections.forEach((s, i) => {
    if (!picks[i].pick) return;
    if (s.lines.length > LIMITS.maxLinesPerChunk) problems.push(`[${s.name}] has ${s.lines.length} lines; the limit is ${LIMITS.maxLinesPerChunk}.`);
    const long = s.lines.find(l => l.length > LIMITS.maxLineChars);
    if (long) problems.push(`A line in [${s.name}] is longer than ${LIMITS.maxLineChars} characters.`);
  });
  if (problems.length) { rw.error.textContent = problems.join('\n'); rw.error.hidden = false; return; }

  rw.go.disabled = true;
  const started = Date.now();
  const busy = msg => { rw.status.textContent = msg; rw.status.className = 'status busy'; };
  try {
    let songId = t.songId;
    if (!songId) {
      busy('Uploading the original take to ElevenLabs…');
      songId = await uploadForInpainting(t.blob);
      t.songId = songId;
      await db.add(t);
    }
    const model = t.model === 'music_v2' ? 'music_v2' : 'music_v2_5';
    let offset = 0;
    const chunks = newSections.map((s, i) => {
      const chunk = picks[i].pick
        ? { text: s.lines.join('\n'), duration_ms: s.ms, positive_styles: s.pos || [], negative_styles: s.neg || [], context_adherence: 'high' }
        : { song_id: songId, range: { start_ms: offset, end_ms: offset + s.ms } };
      offset += s.ms;
      return chunk;
    });
    const body = { model_id: model, composition_plan: { chunks }, force_instrumental: false };
    const redoMs = newSections.filter((s, i) => picks[i].pick).reduce((a, s) => a + s.ms, 0);
    const tick = setInterval(() => busy(`Regenerating ${fmtSec(redoMs)}… ${Math.round((Date.now() - started) / 1000)}s`), 1000);
    busy(`Regenerating ${fmtSec(redoMs)}…`);
    let result;
    try { result = await compose(body, undefined, true); } finally { clearInterval(tick); }
    const take = {
      id: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now() + Math.random())),
      createdAt: Date.now(),
      title: t.title.replace(/ \(rewrite( \d+)?\)$/, '') + ' (rewrite)',
      model, mode: 'structured', output: t.output,
      lyrics: sectionsToLyrics(newSections),
      sections: newSections,
      songId: result.songId || null,
      parentId: t.id,
      plannedMs: offset,
      request: body,
      mime: result.blob.type || 'audio/mpeg',
      blob: result.blob,
    };
    await db.add(take);
    await renderTakes();
    rw.dialog.close();
    setStatus(`Rewrite done in ${Math.round((Date.now() - started) / 1000)}s. The new take is at the top of the list.` + (result.storeNote ? ' ' + result.storeNote : ''), result.storeNote ? '' : 'ok');
    showError('');
  } catch (err) {
    rw.status.textContent = '';
    rw.status.className = 'status';
    rw.error.textContent = err.message || String(err);
    rw.error.hidden = false;
  } finally {
    rw.go.disabled = false;
  }
});

// ---------- Settings ----------

function openSettings() {
  el.apiKey.value = apiKey();
  el.apiBase.value = localStorage.getItem(LS.base) || '';
  el.keyCheck.textContent = '';
  el.keyCheck.className = 'status';
  el.settings.showModal();
}
function refreshBanner() { el.banner.hidden = !!apiKey(); }

$('settings-btn').addEventListener('click', openSettings);
$('setup-open').addEventListener('click', openSettings);
$('key-toggle').addEventListener('click', e => {
  const show = el.apiKey.type === 'password';
  el.apiKey.type = show ? 'text' : 'password';
  e.target.textContent = show ? 'Hide' : 'Show';
});
$('key-forget').addEventListener('click', () => {
  localStorage.removeItem(LS.key);
  el.apiKey.value = '';
  refreshBanner();
  el.keyCheck.textContent = 'Key removed from this browser.';
});
$('settings-save').addEventListener('click', () => {
  const key = el.apiKey.value.trim();
  if (key) localStorage.setItem(LS.key, key); else localStorage.removeItem(LS.key);
  const base = el.apiBase.value.trim().replace(/\/+$/, '');
  if (base && base !== API_DEFAULT) localStorage.setItem(LS.base, base); else localStorage.removeItem(LS.base);
  refreshBanner();
});
$('key-check-btn').addEventListener('click', async () => {
  const key = el.apiKey.value.trim();
  if (!key) { el.keyCheck.textContent = 'Paste a key first.'; return; }
  localStorage.setItem(LS.key, key);
  const base = el.apiBase.value.trim().replace(/\/+$/, '');
  if (base && base !== API_DEFAULT) localStorage.setItem(LS.base, base); else localStorage.removeItem(LS.base);
  refreshBanner();
  el.keyCheck.className = 'status busy';
  el.keyCheck.textContent = 'Checking…';
  try {
    const res = await callApi('/v1/user/subscription', { method: 'GET' });
    const s = await res.json();
    const left = (s.character_limit ?? 0) - (s.character_count ?? 0);
    el.keyCheck.className = 'status ok';
    el.keyCheck.textContent = `Key works. Plan: ${s.tier || 'unknown'}; about ${left.toLocaleString()} credits left this cycle.`;
  } catch (err) {
    el.keyCheck.className = 'status';
    el.keyCheck.textContent = err.message;
  }
});

// ---------- Boot ----------

loadDraft();
renderStructure();
refreshBanner();
renderTakes();
if ('serviceWorker' in navigator) navigator.serviceWorker.register('./sw.js').catch(() => {});

// Exposed for tests and for poking around in the console.
window.VocalForge = { parseLyrics, parseTag, allocate, buildRequest, globalStyles, planToLyrics, parseDetailed, sectionsOf, LIMITS };
