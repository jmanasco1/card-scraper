'use strict';

/**
 * Verifies the endpoint offline, before it is ever deployed.
 * Mirrors eBay's documented hash (challengeCode + verificationToken + endpoint)
 * computed independently here, then checks the handler agrees.
 */
const crypto = require('crypto');
const handler = require('./api/ebay-deletion.js');

const TOKEN = 'test_verification_token_at_least_32_chars_long';
const ENDPOINT = 'https://card-scraper-ebay.vercel.app/api/ebay-deletion';

function mockRes() {
  const res = { statusCode: null, body: null, headers: {} };
  res.status = (code) => { res.statusCode = code; return res; };
  res.json = (payload) => { res.body = payload; return res; };
  res.setHeader = (k, v) => { res.headers[k] = v; return res; };
  return res;
}

function run(name, fn) {
  try { fn(); console.log(`PASS  ${name}`); }
  catch (err) { console.error(`FAIL  ${name}\n      ${err.message}`); process.exitCode = 1; }
}

const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

process.env.EBAY_VERIFICATION_TOKEN = TOKEN;
process.env.EBAY_ENDPOINT_URL = ENDPOINT;

run('challenge returns the correct sha256, matching eBay\'s algorithm', () => {
  const challengeCode = 'a1b2c3d4-1234-5678-9abc-def012345678';
  const expected = crypto.createHash('sha256')
    .update(challengeCode).update(TOKEN).update(ENDPOINT).digest('hex');

  const res = mockRes();
  handler({ method: 'GET', url: `/api/ebay-deletion?challenge_code=${challengeCode}`, headers: {} }, res);

  assert(res.statusCode === 200, `expected 200, got ${res.statusCode}`);
  assert(res.body.challengeResponse === expected,
    `hash mismatch\n      got      ${res.body.challengeResponse}\n      expected ${expected}`);
  assert(res.headers['Content-Type'] === 'application/json', 'missing JSON content type');
  assert(/^[0-9a-f]{64}$/.test(res.body.challengeResponse), 'not a 64-char hex digest');
});

run('hash is order-sensitive (guards against silently reordering inputs)', () => {
  const challengeCode = 'zzz';
  const wrongOrder = crypto.createHash('sha256')
    .update(TOKEN).update(challengeCode).update(ENDPOINT).digest('hex');
  const res = mockRes();
  handler({ method: 'GET', url: `/api/ebay-deletion?challenge_code=${challengeCode}`, headers: {} }, res);
  assert(res.body.challengeResponse !== wrongOrder, 'handler used the wrong input order');
});

run('endpoint URL falls back to the calling host when unset', () => {
  delete process.env.EBAY_ENDPOINT_URL;
  const challengeCode = 'abc';
  const derived = 'https://derived.vercel.app/api/ebay-deletion';
  const expected = crypto.createHash('sha256')
    .update(challengeCode).update(TOKEN).update(derived).digest('hex');
  const res = mockRes();
  handler({ method: 'GET', url: `/api/ebay-deletion?challenge_code=${challengeCode}`,
            headers: { host: 'derived.vercel.app' } }, res);
  assert(res.body.challengeResponse === expected, 'did not derive endpoint from host header');
  process.env.EBAY_ENDPOINT_URL = ENDPOINT;
});

run('missing challenge_code is rejected, not hashed as empty', () => {
  const res = mockRes();
  handler({ method: 'GET', url: '/api/ebay-deletion', headers: {} }, res);
  assert(res.statusCode === 400, `expected 400, got ${res.statusCode}`);
});

run('deletion POST is acknowledged with 200', () => {
  const res = mockRes();
  handler({ method: 'POST', url: '/api/ebay-deletion', headers: {},
            body: { notification: { notificationId: 'n-1', eventDate: '2026-08-19T00:00:00Z',
                                    data: { username: 'someseller', userId: 'u1' } } } }, res);
  assert(res.statusCode === 200, `expected 200, got ${res.statusCode}`);
});

run('POST with an empty body still returns 200 rather than throwing', () => {
  const res = mockRes();
  handler({ method: 'POST', url: '/api/ebay-deletion', headers: {} }, res);
  assert(res.statusCode === 200, `expected 200, got ${res.statusCode}`);
});

run('unconfigured token fails closed with 500', () => {
  const saved = process.env.EBAY_VERIFICATION_TOKEN;
  delete process.env.EBAY_VERIFICATION_TOKEN;
  const res = mockRes();
  handler({ method: 'GET', url: '/api/ebay-deletion?challenge_code=x', headers: {} }, res);
  assert(res.statusCode === 500, `expected 500, got ${res.statusCode}`);
  process.env.EBAY_VERIFICATION_TOKEN = saved;
});

run('other verbs are rejected with 405', () => {
  const res = mockRes();
  handler({ method: 'DELETE', url: '/api/ebay-deletion', headers: {} }, res);
  assert(res.statusCode === 405, `expected 405, got ${res.statusCode}`);
});
