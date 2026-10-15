# Is the `azure` backend a good S3-to-Azure gateway for Open edX? A tested answer.

**Bottom line: no, not for course content, not yet.** This is why
`OBJSTORAGE_BACKEND=azure` alone isn't enough to use this backend — you must
also set `OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK=true`, and this document is what
you're acknowledging. `rclone serve s3` reliably and reproducibly **hangs** —
a livelocked process pinned at 100%+ CPU, zero network progress, that does
not self-recover and needs a container restart — when multipart upload
parts arrive out of ascending order, once the upload crosses a few hundred
MiB — nothing more adversarial than the S3 API's own explicitly-permitted
"parts may be uploaded in any order" behavior. See
[Finding 1](#finding-1-the-multipart-hang-headline).

A second, independent livelock was found and **fixed** during this same
evaluation pass: ordinary Studio usage (exporting a course containing a
SCORM component) hung the gateway via a completely unrelated mechanism — a
zero-duration `--dir-cache-time` causing a directory-cache cleanup-timer
goroutine storm inside rclone's VFS layer. Root-caused via a live Go
goroutine dump and fixed by setting `--dir-cache-time=1s` instead of `0`;
see [Finding 5](#finding-5-the-zero-duration-directory-cache-storm-fixed)
for the full story, including a correction to an earlier, less precise
version of this same finding. Finding 1 remains open and unfixed — it is a
genuine defect in rclone itself with no plugin-side workaround — which is
why the risk acknowledgment stays required.

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
  `HeadObject`, presigned URLs, `ListObjectsV2` *(including against an empty
  prefix — this used to livelock the gateway before the fix in
  [Finding 5](#finding-5-the-zero-duration-directory-cache-storm-fixed))*,
  server-side `CopyObject`, `DeleteObject` — all work exactly as they do
  against the `rustfs` backend.
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

### Finding 5: the zero-duration directory-cache storm (fixed)

**Severity at the time it was found: higher practical risk than Finding 1 —
no adversarial client behavior, no size threshold, no multipart upload
required. Status: root-caused and fixed — both `rclonegw` processes now run
with `--dir-cache-time=1s` instead of `=0`.**

This was first discovered indirectly: a real Studio course export of a
course containing a SCORM component hung at the "Compressing" stage
indefinitely, `rclonegw` pinned at 140-166% CPU with zero network progress.
`openedx-scorm-xblock`'s `path_exists()` calls `django-storages`'
`S3Boto3Storage.listdir()` (`storages/backends/s3.py:597`) to check whether
the package's extraction folder already exists — which, on a course's first
export, it never does. The same course exported in 1-2 seconds against both
the `rustfs` and `external_s3` backends; only `azure` hung.

**An earlier version of this finding claimed a minimal, standalone boto3
reproduction — that claim was wrong, and is corrected here.** The exact same
`list_objects`/`Delimiter` call that appeared to hang in isolation does
**not** reliably hang when replayed against a freshly-started `rclonegw`:
across repeated attempts (fresh container, exact real prefix, single calls,
8 concurrent calls, 200 sequential calls) it consistently returned in under
60ms. The original "isolated" repro wasn't actually isolated — it was
replayed against a gateway a stuck Celery client was still hammering in a
retry loop, so the appearance of a clean single-call reproduction was an
artifact of that retry storm, not evidence that any single call triggers
it on its own. Lesson for next time: before writing a minimal reproduction
into a finding, retest it against a target *you provisioned fresh for that
specific test* — don't just reconfirm the symptom and infer the trigger.

**Root cause, found via a live Go goroutine dump (`docker kill
--signal=SIGQUIT rclonegw`, which rclone handles by dumping all goroutine
stacks before exiting):** every blocked goroutine was waiting on
`sync.(*RWMutex).Lock` inside `github.com/rclone/rclone/vfs.(*Dir).cacheCleanup`
or `vfs.(*Dir).ForgetAll` (`vfs/dir.go:85` and `:230`), each one spawned by
`time.goFunc` — i.e., a *periodic directory-cache cleanup timer*, one per
VFS directory node. `--dir-cache-time=0` does not mean "caching disabled";
it means every cache entry is permanently and immediately expired, which
drives this cleanup timer to re-arm itself with no effective delay. Confirmed
with a direct A/B on the real trigger (a Studio export of the SCORM-containing
course), restarting only `rclonegw`/`rclonegw-public` between runs:

| `--dir-cache-time` | Real Studio export, repeated | Result |
|---|---|---|
| `0` | 4 attempts | **Hung every time** (CPU pinned 110-157%, confirmed via `docker stats` and a goroutine dump) |
| `1s`/`5s` | 9 attempts | **Succeeded every time** (0% CPU at rest throughout) |

Also directly observed: reverting back to `0` on an otherwise-idle gateway
(no new requests at all) showed CPU climbing from ~0% to ~140% over the
following ~15 seconds and staying there — the cleanup-timer storm can build
up in the background once triggered, independent of whether a client is
still waiting on a response.

**The fix**: `patches/azure/local-docker-compose-services` and
`patches/azure/k8s-deployments` now set `--dir-cache-time=1s` for both
`rclonegw` and `rclonegw-public` (previously `0`). This keeps the property
the flag exists for — buckets created by `tutor local do init` become
visible well within a second, not after the default 5-minute cache window —
while avoiding the zero-duration edge case in rclone's own timer-rearming
logic. This is a code fix, not a documentation-only note: unlike Finding 1
(a genuine upstream rclone defect with no known workaround), this one was
this plugin's own configuration choice, and changing it is enough to resolve
it. Not yet filed upstream against rclone — the zero-duration cleanup-timer
behavior is still arguably a latent bug in rclone itself (nothing says
`--dir-cache-time=0` should behave differently from "a very small but
positive duration" beyond caching semantics), but this plugin no longer
depends on anyone fixing it there.

Confirmed against both the bundled Azurite and, for the original discovery,
[floci-az](https://floci.io/az/) (an unrelated, independently implemented
Azure Blob Storage emulator) — ruling out an Azurite-specific quirk. Tested
against the same pinned `docker.io/rclone/rclone:1.75.1` as every other
finding here.

**How to reproduce the original hang** (on a version of this plugin before
this fix, or with `--dir-cache-time` manually reverted to `0`): enable the
`azure` backend, run `tutor local launch`, log into Studio, and export any
course containing an XBlock whose rendering checks for a not-yet-created
storage path (a SCORM package is the easiest way to get one).

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
| Multipart upload (out-of-order parts) | **Hangs, reproducibly, ≥~300 MiB** — unfixed, no plugin-side workaround | No equivalent risk — no translation layer |
| Listing an empty prefix (`listdir`/`ListObjectsV2` + `Delimiter`) | Fixed (`--dir-cache-time=1s`); see Finding 5 | No equivalent risk — no translation layer |
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
news, and Finding 5's livelock — found through completely ordinary Studio
usage, no adversarial behavior required — turned out to be fixable with a
one-line config change once root-caused. But Finding 1 is exactly the kind
of result this backend was built to find: a real, reproducible, severe
failure in large multipart uploads (the operation that broke MinIO's version
of this same architecture for this same community, now independently
reproduced in a different implementation) with no known fix. The `rustfs`
backend carries none of this risk
class, because it never translates between two different storage models in
the first place — use it, or the `external_s3` backend against a real
S3-compatible provider, unless Azure is a hard requirement. If it is, the
honest options today are: wait for upstream rclone to address this (worth
filing against [rclone/rclone](https://github.com/rclone/rclone) with the
reproduction steps above), evaluate a different translation layer (unvalidated),
or accept the operational cost of restarting a stalled gateway process as a
standing risk, with `OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK` as a reminder every
time you touch the config that you've accepted it.
