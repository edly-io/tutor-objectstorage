# Is the `azure` backend a good S3-to-Azure gateway for Open edX? A tested answer.

**Bottom line: no, not for course content, not yet.** This is why
`OBJSTORAGE_BACKEND=azure` alone isn't enough to use this backend — you must
also set `OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK=true`, and this document is what
you're acknowledging. `rclone serve s3` reliably and reproducibly **hangs** —
a livelocked process pinned at 100%+ CPU, zero network progress, that does
not self-recover and needs a container restart — in two independently
confirmed cases:

1. When multipart upload parts arrive out of ascending order, once the
   upload crosses a few hundred MiB — nothing more adversarial than the S3
   API's own explicitly-permitted "parts may be uploaded in any order"
   behavior. See [Finding 1](#finding-1-the-multipart-hang-headline).
2. **On a single `ListObjects`/`ListObjectsV2` call with a `Delimiter`
   against a prefix that has zero objects under it** — no multipart upload,
   no adversarial ordering, no size threshold required. This is the "does
   this folder exist yet" check `django-storages`' `S3Boto3Storage.listdir()`
   performs, and it is triggered by completely ordinary Studio usage: this
   evaluation hit it exporting a real course that happens to contain a SCORM
   component, through the unmodified Studio UI, on the very first export.
   See [Finding 5](#finding-5-listing-an-empty-prefix-hangs-the-gateway) —
   arguably the more operationally important of the two, since it needs no
   adversarial client behavior at all to trigger.

Both were found against the backend's default target (Azurite), confirmed a
second time against a different, independent Azure Blob Storage emulator
([floci-az](https://floci.io/az/)) with no code changes, and are not
theoretical risks read off a changelog: each was triggered and the CPU-pinned
signature directly observed via `docker stats`.

Everything else about this backend — the two-process anonymous/private
bucket split, the Kubernetes network isolation, CORS, concurrency, the
generic S3 verbs — worked correctly once built, including surviving an
adversarial design review before a line of code was written, and including a
full live re-verification through the real Caddy container when this backend
was folded into this unified plugin. That makes Finding 1 more significant,
not less: this is a careful implementation of the idea, and the idea still
breaks on contact with a real multipart upload pattern.

## What this document is

A comparison between this plugin's `azure` backend (an S3-to-Azure
translation layer built on [rclone](https://rclone.org)'s `serve s3`
command, originally shipped as the standalone `tutor-rclone-gateway`
plugin) and its `rustfs` backend (a native, non-translating S3-compatible
storage engine, originally shipped as the standalone `tutor-rustfs`
plugin) — written to answer a question raised on [the Open edX
forum](https://discuss.openedx.org/t/rustfs-plugin-to-replace-tutor-minio-thoughts-on-azure-gateway/19670)
after MinIO's own "gateway" mode — which did this same kind of S3-to-Azure
translation — was deprecated in 2022, and RustFS turned out to have no
equivalent. Every claim below is backed by either a specific test in this
repository (`tests/test_s3_conformance_azure.py`, `tests/test_patches.py`)
or a cited external source; see the inline references.

## Architecture: a translation layer, not an engine

This is the load-bearing distinction, and it is worth stating plainly before
any test result: the `rustfs` backend runs RustFS, a native S3-compatible
storage *engine* — data lives in RustFS's own format, and the S3 API is both
the control plane and the data plane, implemented by a team whose entire
product is that implementation. The `azure` backend instead runs `rclone
serve s3` as a *translation layer* in front of Azure Blob Storage, a backend
that was never designed to speak S3 at all. Every byte that moves through
this backend crosses a translation boundary between two different storage
models (S3's flat key/multipart-upload model vs. Azure's
container/block-blob model) that an engine like RustFS simply does not have
to cross.

MinIO invented this exact pattern — an S3 gateway in front of non-S3 cloud
backends, including Azure — ran it for years, and killed it. The [deprecation
announcement](https://blog.min.io/deprecation-of-the-minio-gateway/) cites the
ongoing cost of keeping per-backend translation correct as the reason, not a
lack of demand. Open edX's own community independently hit the failure mode
this backend was built to test for: a [forum
thread](https://discuss.openedx.org/t/tutor-qunice-minio-as-gateway-for-azure-storage-issues/12995)
documents MinIO running in gateway mode in front of Azure Blob Storage in
Tutor, where multipart course-import/export uploads failed with "One or more
of the specified parts could not be found," traced to Azure Storage itself —
not MongoDB, not file size limits. The only fix anyone found was abandoning
gateway mode (`MINIO_GATEWAY: null`) and running object storage standalone.
That thread is why the `rustfs` backend has no gateway mode at all, and it is
the precedent this entire evaluation was measuring rclone against.

## What works

Confirmed by the live conformance suite
(`tests/test_s3_conformance_azure.py`), run against a real `tutor local
launch` deployment with the backend's default Azurite target, separately
against the CI compose stack, and re-confirmed live when this backend was
folded into the unified `tutor-objectstorage` plugin:

- **Generic S3 operations** — `ListBuckets`, `PutObject`/`GetObject`,
  `HeadObject`, presigned URLs, `ListObjectsV2` *(when at least one object
  already exists under the listed prefix — see
  [Finding 5](#finding-5-listing-an-empty-prefix-hangs-the-gateway) for the
  case that doesn't)*, server-side `CopyObject`, `DeleteObject` — all work
  exactly as they do against the `rustfs` backend.
- **The anonymous/private bucket split holds, robustly.** rclone's `serve s3`
  has no bucket-level ACL mechanism at all (unlike RustFS's `rc anonymous set
  download`), so this backend runs two `rclone serve s3` processes — one
  authenticated, one anonymous-read-only-and-scoped — behind a Caddy rule
  that splits traffic by method and path (see the README's "How it works").
  This was tested three separate ways, and all three passed:
  - Through Caddy, by hostname, with the real Caddy container in front:
    anonymous GET succeeds on the public bucket, anonymous PUT/DELETE are
    rejected, and anonymous GET on a *private* bucket is rejected.
  - **Directly against the anonymous process, bypassing Caddy entirely**: a
    private bucket is not merely access-denied, it is structurally invisible
    — `GET /openedxgrades/x` returns a clean `404 NoSuchBucket`, and
    `ListBuckets` on that process lists only the public bucket, never the
    others by name. This confirms rclone's `--include` flag is a real
    VFS-layer boundary, not a listing-only filter.
  - The Kubernetes `NetworkPolicy` that restricts both gateway Deployments to
    ingress from the Caddy pod only (closing the exposure that the `rustfs`
    backend's own `NodePort`-without-restriction pattern would have created
    here) renders correctly and was validated statically.
- **CORS** works, because this backend deliberately does not rely on
  rclone's own `--allow-origin` flag (whose documentation ties it to
  rclone's WebDAV/remote-control code path, with no confirmation it covers
  `serve s3` at all) and sets the headers at the Caddy layer instead.
- **Concurrent-GET latency does not scale with client count** — the
  regression guard for a real, previously-shipped rclone bug (a lock held for
  an entire HTTP transaction, serializing every other client, fixed in
  1.69) — passed at 20 concurrent clients.
- **A single, sequential, boto3-managed 350 MiB multipart upload** (past the
  256 MiB default streaming-buffer threshold) completed in under 6 seconds
  against Azurite, with the correct final size — re-confirmed with a smaller
  40 MiB in-order upload through the real stack during this plugin's merge.

## What doesn't

### Finding 1: the multipart hang (headline)

**Severity: do not use for production course content until this is
understood or fixed upstream.**

`test_multipart_upload_out_of_order_at_concurrency` (gated behind
`OBJSTORAGE_TEST_REAL_AZURE_GATE=1`, intended to also run against real
Azure — see [What wasn't tested](#what-wasnt-tested)) uploads a file via raw
`upload_part` calls submitted in **strictly reversed order** — part N first,
part 1 last — which the S3 API explicitly permits clients to do. Run against
this backend's own default Azurite target:

| Size | Concurrency | Result |
|---|---|---|
| 40 MiB (5 parts) | 1 | Pass, 1.17s |
| 100 MiB (~13 parts) | 1 | Pass, 2.05s |
| 200 MiB (~25 parts) | 1 | Pass, 3.60s |
| 260 MiB (~33 parts) | 1 | Pass, 4.61s |
| **300 MiB (~38 parts)** | **1** | **Hang — no completion within 70s, killed** |
| **350 MiB (~44 parts)** | **1** | **Hang — no completion within 70s (and separately within 300s), killed** |

The hang is not "slow" — it is a livelock. `docker stats` on the `rclonegw`
container during the hang showed **113–213% CPU, perfectly flat memory
(~634 MiB), and perfectly flat network I/O (0 new bytes in or out)** across
multiple measurements seconds apart. The process is actively burning two CPU
cores while making no observable progress, and does not recover after the
client disconnects — `tutor local restart rclonegw` was required each time to
return it to a clean 0% CPU / 18 MiB baseline. This reproduced identically on
two separate occasions (first discovered running the full concurrency sweep
[1, 2, 4, 8, 10] at 350 MiB, then confirmed in isolation at concurrency=1
alone — so **concurrency is not the trigger; size/part-count crossing
somewhere between 260 and 300 MiB, combined with reversed delivery order,
is**).

This independently reproduces — and arguably exceeds — the real-world incident
in rclone's own issue tracker
([#7453](https://github.com/rclone/rclone/issues/7453)): a user uploading a
2 GiB file hit an unexplained multi-minute stall at roughly 322 MiB / 64
parts, root cause never conclusively pinned, resolved only by lowering
client-side concurrency. This evaluation's finding shows the trigger isn't
concurrency specifically — strictly sequential (concurrency=1) delivery in
reversed order is sufficient on its own.

**Why this matters for Open edX specifically**: course import/export and
video uploads are exactly the workloads that cross the multipart threshold,
and nothing in the S3 API forces a client to upload parts in ascending order —
boto3's own `TransferConfig`-managed uploads happen to do so by default (which
is why the plain 350 MiB test above passed cleanly), but any client,
retry logic, or parallel-upload tool that doesn't guarantee strict ordering
could trigger this. A livelocked gateway process doesn't just fail the one
upload — given this backend's single `rclonegw` process serves *every*
bucket, it stalls every other concurrent request to any private bucket until
someone notices and restarts it.

**How to reproduce**: `TESTING.rst` Tier 5, or directly:
```
export OBJSTORAGE_TEST_ENDPOINT=<your endpoint>
export OBJSTORAGE_TEST_ACCESS_KEY=... OBJSTORAGE_TEST_SECRET_KEY=...
export OBJSTORAGE_TEST_REAL_AZURE_GATE=1
export OBJSTORAGE_TEST_REAL_AZURE_SIZE_MIB=300
pytest -v "tests/test_s3_conformance_azure.py::test_multipart_upload_out_of_order_at_concurrency[1]"
```
Tested against `docker.io/rclone/rclone:1.75.1` (the exact pinned version
this backend ships), `--vfs-cache-mode off` (this backend's default), backed
by Azurite 3.37.0. Not yet tested against real Azure Blob Storage — see
below — but nothing about the hang's signature (CPU-pinned, zero I/O,
Azurite-local) points to an Azure-specific cause; it looks like rclone's own
out-of-order part-buffering logic, independent of backend.

### Finding 2: non-standard error code for missing credentials

A request to the authenticated gateway with a correctly-signed-but-wrong
access key gets the expected, standard `403`-shaped
`InvalidAccessKeyId`. A request with **no** `Authorization` header at all gets
`400 UnsupportedAlgorithm: Encountered an unsupported algorithm` — not the
`403 MissingAuthenticationToken` real AWS S3 (and RustFS) return. No data
leaks and no write succeeds either way — this is purely an error-message
ergonomics gap — but a client or monitoring system that pattern-matches on
AWS's usual error vocabulary (reasonably, since everything else here mimics
AWS so closely) may mishandle or mislabel this specific case. Re-confirmed
live, through Caddy, during this backend's merge into the unified plugin —
both the anonymous-read-of-private-bucket case and the anonymous-write case
returned exactly this. See `tests/test_s3_conformance_azure.py`'s
`ANONYMOUS_REJECTION_CODES`.

### Finding 3: Range requests don't return 206

A `GET` with a `Range` header returns the **correct bytes** and a **correct
`Content-Range` header** — but HTTP status `200`, not the spec-required `206`
(both RFC 7233 and the S3 API require 206 for a satisfied range request; real
AWS S3 and RustFS both return 206). Verified this originates in rclone
itself, not Caddy: Caddy's `reverse_proxy` is a transparent passthrough that
does not rewrite status codes. Most clients that check for `Content-Range`
rather than strictly gating on status 206 will work fine; clients or CDNs that
branch on status code may not treat this as a partial response. Relevant to
video scrubbing, which depends on Range support.

### Finding 4: this backend needed real infrastructure the rustfs backend doesn't

Not a runtime bug, but a fair point of comparison: building a working,
secure version of this backend required solving three problems the `rustfs`
backend never has to:

1. **No bucket ACLs** → a second gateway process plus Caddy routing (see
   "What works" above) instead of one `rc anonymous set download` command.
2. **No native CORS** → headers set at the proxy layer instead of a server
   default.
3. **No "this bucket must stay private" network boundary** → a Kubernetes
   `NetworkPolicy` this backend has to ship and the `rustfs` backend doesn't
   need.

None of these were insurmountable — all three were built, tested, and work —
but they are real operational surface this architecture adds, exactly the
kind of "maintaining correct per-backend translation is expensive" cost MinIO
cited when it killed its own version of this pattern.

### Finding 5: listing an empty prefix hangs the gateway

**Severity: higher practical risk than Finding 1 — needs no adversarial
client behavior, no size threshold, and no multipart upload at all.**

Minimal, backend-agnostic reproduction (no Django, no Open edX, no SCORM
involved):

```python
import boto3
s3 = boto3.client("s3", endpoint_url="http://<gateway-host>", ...)
# Hangs indefinitely; rclonegw pins at 140-165% CPU within seconds.
s3.get_paginator("list_objects").paginate(
    Bucket="openedx", Delimiter="/", Prefix="definitely-does-not-exist/"
)
```

The identical call against a prefix with **at least one object already
under it** returns in under 50ms. The only variable is whether the prefix is
empty. `docker stats` during the hang showed the same signature as Finding 1
— 140-166% CPU, near-zero memory growth, the client blocked on
`socket.recv` waiting for response headers that never arrive — and, also
like Finding 1, **no self-recovery**: restarting `rclonegw` alone was not
enough to clear it, because the original stuck client (still inside its
retry loop) reconnected and re-issued the identical request against the
freshly-restarted gateway, re-triggering the livelock within seconds. Both
the gateway *and* the stuck client have to be restarted together.

This was first discovered indirectly: a real Studio course export of a
course containing a SCORM component hung at the "Compressing" stage
indefinitely. `openedx-scorm-xblock`'s `path_exists()` calls
`django-storages`' `S3Boto3Storage.listdir()`
(`storages/backends/s3.py:597`) to check whether the package's extraction
folder already exists — which, on a course's first export, it never does.
`listdir()` issues exactly the paginated `list_objects` + `Delimiter="/"`
call shown above. The same course exported in 1-2 seconds against both the
`rustfs` and `external_s3` backends; only the `azure` backend hung, and only
on this specific call shape.

**Why this matters more than Finding 1 in practice**: Finding 1 requires an
upload crossing a size threshold with parts delivered out of order — a
pattern most ordinary clients (including boto3's own default
`TransferConfig`) don't produce. This one requires nothing beyond "ask
whether an empty folder exists," which is an unremarkable, common operation
— `S3Boto3Storage.listdir()`/`.exists()`-style folder checks appear
throughout Open edX and third-party XBlocks, not only in the SCORM XBlock
used to find this. Any such check against a path that hasn't been written to
yet will hit this.

Confirmed twice, independently: against the bundled Azurite, and separately
against [floci-az](https://floci.io/az/) (an unrelated, independently
implemented Azure Blob Storage emulator) with no code or config changes
beyond the endpoint — ruling out an Azurite-specific quirk. Tested against
the same pinned `docker.io/rclone/rclone:1.75.1` as every other finding
here.

**How to reproduce**: enable the `azure` backend, run `tutor local launch`,
log into Studio, and export any course containing an XBlock whose rendering
checks for a not-yet-created storage path (a SCORM package is the easiest
way to get one) — or skip Open edX entirely and run the minimal boto3
snippet above directly against a running `rclonegw`.

### Known, from rclone's own documentation and issue tracker (context for Finding 1, not independently re-verified here)

- `rclone serve s3` is explicitly labeled **"Experimental"** by rclone's own
  docs.
- **CVE-2026-88045** (CVSS 7.5, High): the multipart-streaming code path
  allowed an unauthenticated attacker to exhaust server memory by declaring a
  large `Content-Length` and never sending the body. Fixed in 1.75.1, the
  exact version this backend pins — but notably, this is the *same area of
  code* (multipart streaming, rewritten in 1.75.0) that Finding 1 lives in,
  one point release later.
- Multipart **server-side copy** is documented upstream as unreliable and
  "extremely slow, eventually failing"
  ([#7454](https://github.com/rclone/rclone/issues/7454)) — not independently
  re-tested here; see `test_multipart_server_side_copy`
  (`OBJSTORAGE_TEST_SLOW=1`, not run as part of this evaluation).
- A past concurrency bug (fixed in 1.69) held a lock for an entire HTTP
  transaction, serializing every other client — this evaluation's
  concurrent-GET test confirms that *specific* bug is fixed, but the class of
  bug (resource contention in the serve-s3 layer) clearly isn't extinct,
  given Finding 1.

## azure backend vs. rustfs backend

| | `azure` backend | `rustfs` backend |
|---|---|---|
| Architecture | Translation layer (S3 → Azure) | Native S3 engine |
| Backend maturity | `serve s3`: "Experimental" | RustFS 1.0.0 GA |
| Multipart upload (standard order) | Works | Works |
| Multipart upload (out-of-order parts) | **Hangs, reproducibly, ≥~300 MiB** | No equivalent risk — no translation layer |
| Listing an empty prefix (`listdir`/`ListObjectsV2` + `Delimiter`) | **Hangs, reproducibly, any size** | No equivalent risk — no translation layer |
| Bucket-level anonymous read | Requires a second process + proxy routing | One native command |
| CORS | Requires proxy-layer headers | Native default |
| Local disk / non-root chown dance | None — stateless, no local volume | Required on fresh Linux installs |
| Kubernetes PVC | None needed | Required |
| Web console | None (use Azure Portal/Storage Explorer) | Built in |
| Backend lock-in | None — any rclone-supported remote | RustFS's own format |
| Requires explicit risk acknowledgment to enable | Yes (`OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK`) | No |

## What wasn't tested

**Real Azure Blob Storage.** Every result above is against Azurite, this
backend's default local emulator. Azurite's own documentation disclaims any
performance guarantee and does not model high-concurrency client behavior —
the dimension most likely to matter for a translation-layer commit-semantics
issue. Finding 1's CPU/memory signature strongly suggests the hang is in
rclone's own request-handling logic rather than anything Azure-specific
(Azurite implements the same Azure Blob block-commit API Azure itself does),
but this has not been confirmed against the real service. Running
`test_multipart_upload_out_of_order_at_concurrency` against a real Azure
Storage account requires a real account and will incur Azure costs — that
needs explicit sign-off and credentials from whoever evaluates this backend
further. Until that run happens, treat Finding 1 as confirmed-against-Azurite
and presumed-but-unconfirmed against real Azure.

## Recommendation

Do not point production course data at the `azure` backend. The two-process
anonymous-bucket design works and was validated thoroughly, which is the good
news — but Findings 1 and 5 are exactly the kind of result this backend was
built to find: real, reproducible, severe failures — one in large multipart
uploads (the operation that broke MinIO's version of this same architecture
for this same community, now independently reproduced in a different
implementation), and one in an everyday "does this folder exist yet" check
that needs no adversarial client behavior at all and was found through
completely ordinary Studio usage. The `rustfs` backend carries none of this risk
class, because it never translates between two different storage models in
the first place — use it, or the `external_s3` backend against a real
S3-compatible provider, unless Azure is a hard requirement. If it is, the
honest options today are: wait for upstream rclone to address this (worth
filing against [rclone/rclone](https://github.com/rclone/rclone) with the
reproduction steps above), evaluate a different translation layer (unvalidated),
or accept the operational cost of restarting a stalled gateway process as a
standing risk, with `OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK` as a reminder every
time you touch the config that you've accepted it.
