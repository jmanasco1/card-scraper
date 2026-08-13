"""
Cloudflare transport probe.

Three CI runs showed gemrate.com serving Cloudflare's "Just a moment..."
managed challenge to a full real-Chrome browser — the challenge never
cleared, which points at IP-reputation blocking of the datacenter runner
rather than a fingerprint problem.

This probe tests a *different* transport: curl_cffi, which impersonates a
real browser's TLS/JA3 + HTTP2 fingerprint at the socket level. If any
impersonation target returns 200 with real HTML/JSON, the site is beatable
without a browser and the scraper can be rebuilt on curl_cffi. If every
target returns 403/503, the block is IP-based and no client-side trick on a
datacenter IP will help.

Usage: python -m scraper.probe
"""

BASE = "https://www.gemrate.com"
PATHS = [
    "/",
    "/sets?grader=psa&category=basketball-cards",
    "/api/sets?grader=psa&category=basketball-cards",
]
IMPERSONATE = ["chrome124", "chrome120", "chrome110", "safari17_0", "edge101"]


def main():
    try:
        from curl_cffi import requests as cffi
    except Exception as e:
        print(f"curl_cffi not available: {e}")
        return

    for imp in IMPERSONATE:
        print(f"\n=== impersonate={imp} ===")
        for path in PATHS:
            url = BASE + path
            try:
                r = cffi.get(url, impersonate=imp, timeout=30)
            except Exception as e:
                print(f"  {path} -> ERROR {e}")
                continue
            body = r.text or ""
            challenged = "just a moment" in body.lower() or "cf-mitigated" in {
                k.lower() for k in r.headers
            }
            ctype = r.headers.get("content-type", "")
            snippet = body[:160].replace("\n", " ")
            flag = " [CHALLENGE]" if challenged else " [OK]" if r.status_code == 200 else ""
            print(f"  {path} -> {r.status_code} {ctype}{flag}")
            print(f"    {snippet!r}")


if __name__ == "__main__":
    main()
