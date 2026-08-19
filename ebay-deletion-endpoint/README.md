# eBay Account Deletion Notification Endpoint

eBay disables production keysets until the application either handles
marketplace account-deletion notifications or holds an exemption. This is the
smallest thing that satisfies the requirement: one serverless function, no
server, no domain, no cost.

`api/ebay-deletion.js` answers eBay's ownership challenge and acknowledges
deletion notices. `test-challenge.js` verifies the hash offline — run it before
deploying.

```bash
node test-challenge.js
```

## Deploy to Vercel

From inside this directory:

```bash
npx vercel login          # only if you are not already logged in
npx vercel --prod
```

Accept the defaults. Vercel prints a URL like
`https://ebay-deletion-endpoint.vercel.app`. **Your endpoint is that URL plus
`/api/ebay-deletion`.**

Now set the verification token — a string you invent, 32-80 characters, letters,
digits, `_` and `-` only. eBay needs the identical value.

```bash
npx vercel env add EBAY_VERIFICATION_TOKEN production
# paste the token when prompted

npx vercel --prod         # redeploy so the function picks up the variable
```

Confirm it answers:

```bash
curl "https://YOUR-URL.vercel.app/api/ebay-deletion?challenge_code=test123"
```

A JSON body containing `challengeResponse` with a 64-character hex string means
it is working. `endpoint not configured` means the environment variable did not
land — check you selected the production environment and redeployed.

## Register with eBay

At <https://developer.ebay.com/my/keys>, open the **Notifications** settings for
the production keyset and provide:

- **Email** — where eBay sends alerts about this endpoint
- **Endpoint URL** — `https://YOUR-URL.vercel.app/api/ebay-deletion`
- **Verification token** — the exact string set above

Save. eBay immediately calls the endpoint with a challenge code and the URL is
marked validated on success. The keyset is then re-enabled, usually at once.

## If validation fails

The failure is almost always one of:

- **Token mismatch.** The value in Vercel and the value in eBay's form differ,
  often by a trailing space or newline pasted along with it.
- **URL mismatch.** The hash covers the endpoint URL, so what you register must
  match what eBay calls, character for character. Register the `.vercel.app`
  production URL, not a preview deployment URL.
- **Variable not deployed.** Setting a variable does not update a running
  deployment; `npx vercel --prod` again after adding it.

`npx vercel logs` shows each challenge as it arrives, which makes it obvious
whether eBay is reaching the function at all.

## Optional hardening

eBay signs deletion POSTs with an `x-ebay-signature` header that can be verified
against their public key via the Notification API. This endpoint does not verify
it — the payloads carry no secrets and the handler takes no destructive action,
so the check is not required to satisfy eBay. Add it if you later purge real
data in response to a notice.
