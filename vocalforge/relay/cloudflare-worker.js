/* Optional relay for VocalForge.
 *
 * Only needed if your browser refuses to call api.elevenlabs.io directly (a CORS error shows
 * up as "Could not reach https://api.elevenlabs.io" in the app). Deploy this as a Cloudflare
 * Worker (free tier is plenty), then put the worker's URL into Settings → API host.
 *
 * The relay forwards the request as-is, including the xi-api-key header the app sends, so it
 * never needs to know your key. Set ALLOWED_ORIGIN below so only your site can use it.
 *
 * Deploy:  npm i -g wrangler && wrangler login && wrangler deploy cloudflare-worker.js --name vocalforge-relay
 */

const UPSTREAM = 'https://api.elevenlabs.io';
const ALLOWED_ORIGIN = 'https://jberry2347.github.io'; // or '*' while testing locally

const CORS = {
  'Access-Control-Allow-Origin': ALLOWED_ORIGIN,
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'xi-api-key, content-type, accept',
  'Access-Control-Max-Age': '86400',
  'Vary': 'Origin',
};

export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: CORS });

    const origin = request.headers.get('Origin') || '';
    if (ALLOWED_ORIGIN !== '*' && origin !== ALLOWED_ORIGIN) {
      return new Response('Forbidden', { status: 403, headers: CORS });
    }
    if (!request.headers.get('xi-api-key')) {
      return new Response('Missing xi-api-key header', { status: 401, headers: CORS });
    }

    const url = new URL(request.url);
    if (!url.pathname.startsWith('/v1/music') && url.pathname !== '/v1/user/subscription') {
      return new Response('Not found', { status: 404, headers: CORS });
    }

    const upstream = new Request(UPSTREAM + url.pathname + url.search, {
      method: request.method,
      headers: {
        'xi-api-key': request.headers.get('xi-api-key'),
        'content-type': request.headers.get('content-type') || 'application/json',
        'accept': request.headers.get('accept') || '*/*',
      },
      body: request.method === 'GET' ? undefined : request.body,
    });
    const res = await fetch(upstream);
    const headers = new Headers(res.headers);
    for (const [k, v] of Object.entries(CORS)) headers.set(k, v);
    return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
  },
};
