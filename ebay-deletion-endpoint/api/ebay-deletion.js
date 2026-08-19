'use strict';

/**
 * eBay Marketplace Account Deletion / Closure Notification endpoint.
 *
 * eBay uses this in two ways:
 *
 *   GET  ?challenge_code=...  — ownership check, sent when you register the URL
 *                               and periodically afterwards. Must return
 *                               sha256(challengeCode + verificationToken + endpoint)
 *                               as JSON within a few seconds.
 *   POST                      — an actual account-deletion notice. Must return 2xx,
 *                               or eBay retries and eventually disables the keyset.
 *
 * The hash inputs are order-sensitive and the endpoint must be the exact URL
 * registered with eBay, character for character.
 */

const crypto = require('crypto');

function endpointUrl(req) {
  // Prefer the explicitly configured URL; fall back to the host actually being
  // called, which removes the chicken-and-egg of needing the URL before deploy.
  if (process.env.EBAY_ENDPOINT_URL) {
    return process.env.EBAY_ENDPOINT_URL.trim();
  }
  const host = req.headers['x-forwarded-host'] || req.headers.host;
  const path = (req.url || '').split('?')[0];
  return `https://${host}${path}`;
}

function handleChallenge(req, res, verificationToken) {
  const url = new URL(req.url, 'https://placeholder.invalid');
  const challengeCode = url.searchParams.get('challenge_code');

  if (!challengeCode) {
    res.status(400).json({
      error: 'missing challenge_code',
      hint: 'eBay sends ?challenge_code=... on the ownership check.',
    });
    return;
  }

  const endpoint = endpointUrl(req);
  const hash = crypto.createHash('sha256');
  hash.update(challengeCode);
  hash.update(verificationToken);
  hash.update(endpoint);
  const challengeResponse = hash.digest('hex');

  console.log(JSON.stringify({
    event: 'challenge', endpoint, challengeCodeLength: challengeCode.length,
  }));

  res.setHeader('Content-Type', 'application/json');
  res.status(200).json({ challengeResponse });
}

function handleDeletion(req, res) {
  // Acknowledge fast. eBay treats a slow or failed response as a delivery
  // failure and will disable the keyset if enough of them pile up.
  const notification = (req.body && req.body.notification) || {};
  const data = notification.data || {};

  console.log(JSON.stringify({
    event: 'account-deletion',
    notificationId: notification.notificationId || null,
    eventDate: notification.eventDate || null,
    username: data.username || null,
    receivedAt: new Date().toISOString(),
  }));

  // This scanner stores only public listing attributes; the sole user-linked
  // field is the seller username carried on a listing. If you later store
  // anything keyed to an eBay user, purge it here before responding.

  res.status(200).json({ status: 'acknowledged' });
}

module.exports = (req, res) => {
  const verificationToken = (process.env.EBAY_VERIFICATION_TOKEN || '').trim();
  if (!verificationToken) {
    console.error('EBAY_VERIFICATION_TOKEN is not set');
    res.status(500).json({ error: 'endpoint not configured' });
    return;
  }

  try {
    if (req.method === 'GET') {
      handleChallenge(req, res, verificationToken);
    } else if (req.method === 'POST') {
      handleDeletion(req, res);
    } else {
      res.setHeader('Allow', 'GET, POST');
      res.status(405).json({ error: 'method not allowed' });
    }
  } catch (err) {
    console.error('handler error', err);
    res.status(500).json({ error: 'internal error' });
  }
};
