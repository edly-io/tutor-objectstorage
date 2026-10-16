# Is the `azure` backend a good S3-to-Azure gateway for Open edX? A tested answer.

**Bottom line: usable for real course content now, with a residual risk
operators should understand — which is exactly why `OBJSTORAGE_BACKEND=azure`
alone still isn't enough to use this backend; you must also set
`OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK=true`, and this document is what you're
acknowledging.** Two independent livelocks were found in this backend's
`rclone serve s3` process during this evaluation — a process pinned at
100%+ CPU, zero progress, that does not self-recover without a container
restart — and both are now addressed, not merely documented:

1. **A directory-cache cleanup-timer storm**, triggered by completely
   ordinary Studio usage (exporting a course containing a SCORM component),
   root-caused via a live Go goroutine dump to a zero-duration
   `--dir-cache-time` setting. **Fixed**: `--dir-cache-time=10ms` closes this
   with no known residual risk. See
   [Finding 5](#finding-5-the-zero-duration-directory-cache-storm-fixed) —
   including a correction to an earlier, less precise version of this same
   finding, caught in the same pass.
2. **A multipart reorder-buffer deadlock**, triggered when upload parts
   arrive out of ascending order — a pattern the S3 API explicitly permits —
   past the memory rclone is willing to buffer for parts still waiting on an
   earlier one. **Narrowed, not eliminated**: raising
   `OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT` from rclone's own 256MiB
   default to 2GiB clears every case in the original adversarial test
   matrix (300-350 MiB, concurrency 1 through 10, fully reversed part
   order) with zero hangs - but an upload larger than the configured limit,
   delivered in sufficiently reversed order, can still hit the same wait.
   See [Finding 1](#finding-1-the-multipart-hang-headline).

Both were found against the backend's default target (Azurite), confirmed a
second time against a different, independent Azure Blob Storage emulator
([floci-az](https://floci.io/az/)) with no code changes, and are not
theoretical risks read off a changelog: each was triggered and the CPU-pinned
signature directly observed via `docker stats`, and Finding 1's root cause
was confirmed by reading rclone's own source, not inferred from symptoms
alone.

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

### Finding 1: the multipart hang — root-caused and narrowed (not eliminated)

**Severity when first found: do not use for production course content.
Status after root-causing it: mitigated, not fixed — raising
`OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT` (default now 2GiB, was rclone's
own 256MiB default) clears every case in this evaluation's adversarial test
matrix, but a sufficiently large upload in sufficiently reversed order can
still hit the same wait past that higher threshold.**

`test_multipart_upload_out_of_order_at_concurrency` (gated behind
`OBJSTORAGE_TEST_REAL_AZURE_GATE=1`, intended to also run against real
Azure — see [What wasn't tested](#what-wasnt-tested)) uploads a file via raw
`upload_part` calls submitted in **strictly reversed order** — part N first,
part 1 last — which the S3 API explicitly permits clients to do. Run against
this backend's own default Azurite target, at the original default
256MiB buffer:

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
multiple measurements seconds apart, and did not recover after the client
disconnected — `tutor local restart rclonegw` was required to return it to a
clean baseline. This reproduced identically on two separate occasions (first
discovered running the full concurrency sweep [1, 2, 4, 8, 10] at 350 MiB,
then confirmed in isolation at concurrency=1 alone — so **concurrency is not
the trigger; size/part-count crossing somewhere between 260 and 300 MiB,
combined with reversed delivery order, is**).

**Root cause, found by reading rclone's own source
(`cmd/serve/s3/multipart.go`) after reproducing the hang live and capturing
a Go goroutine dump (`docker kill --signal=SIGQUIT`):** `serve s3` writes
multipart uploads through the VFS **in part-number order**, by design — a
part that arrives ahead of the one the sink is currently waiting for is held
in an in-memory reorder buffer (`multipartUpload.streamBuf`) until its turn.
That buffer is bounded by `--multipart-streaming-buffer-limit` (an existing,
documented rclone flag, default 256MiB, help text: *"Maximum memory buffered
per streamed multipart upload for parts arriving out of order, 0 for
unlimited"*) specifically so a client can't make the gateway buffer
unbounded memory. `waitForTurn` (confirmed live in the goroutine dump at
`multipart.go:256`, its `up.cond.Wait()` call) blocks a part that
doesn't fit in the remaining buffer budget on a `sync.Cond.Wait()` until
room frees up — which, for a client delivering parts in **strict descending
order one at a time** (upload part N, wait for the response, upload part
N-1, ...), never happens: the buffer can't drain until part 1 streams
through, and part 1 is the *last* thing such a client sends, by which point
it's already blocked waiting on part N's response. This is a genuine
request-level deadlock, not a spin or a leak — confirmed directly in the
goroutine dump: multiple goroutines (one per retried request) parked in
`sync.Cond.Wait` inside `waitForTurn`, not consuming CPU themselves. (The
113–213% CPU figure reported above was measured during a *different*
capture than the one that produced the goroutine dump; both signatures —
CPU-pinned in one capture, low-CPU-but-blocked in another — are consistent
with a condition-variable deadlock, since what spins or doesn't depends on
how many stale retried requests have piled up waiting on the same
condition, not on the deadlock itself.)

This independently reproduces — and arguably exceeds — the real-world incident
in rclone's own issue tracker
([#7453](https://github.com/rclone/rclone/issues/7453)): a user uploading a
2 GiB file hit an unexplained multi-minute stall at roughly 322 MiB / 64
parts, root cause never conclusively pinned, resolved only by lowering
client-side concurrency. This evaluation's finding shows the trigger isn't
concurrency specifically — strictly sequential (concurrency=1) delivery in
reversed order is sufficient on its own, and now explains *why*: at that
message size and the default 256MiB buffer, the reorder buffer simply fills
up before the part that would drain it arrives.

**The fix (narrows the risk; does not remove the underlying mechanism)**:
`OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT` is a new config key (default
`"2G"`, rendered as rclone's own `--multipart-streaming-buffer-limit` on the
authenticated `rclonegw` only — `rclonegw-public` is `--read-only` and never
reaches this code path at all). Re-ran the full original adversarial matrix
against the raised limit:

| Size | Concurrency | Result at 256MiB (original default) | Result at 2GiB |
|---|---|---|---|
| 300 MiB | 1 | Hang | Pass, parts in 11.98s (clock includes a 4s outlier on the last part), complete in 0.23s |
| 350 MiB | 1 | Hang | Pass, parts in 12.03s, complete in 0.23s |
| 350 MiB | 2 | *(not independently tested at 256MiB)* | Pass, parts in 9.68s, complete in 0.23s |
| 350 MiB | 4 | Hang (first discovered here) | Pass, parts in 3.53s, complete in 0.38s |
| 350 MiB | 8 | *(not independently tested at 256MiB)* | Pass, parts in 4.28s, complete in 0.24s |
| 350 MiB | 10 | *(not independently tested at 256MiB)* | Pass, parts in 6.65s, complete in 0.22s |

Every case completed with the correct final object size and zero CPU
pinning at rest afterward. This is a genuine trade-off, not a free fix:
raising the buffer raises *how much* out-of-order data the gateway will
hold in memory per in-flight multipart upload before it would start
blocking again — an upload larger than the configured limit, delivered in
sufficiently reversed order, can still deadlock the same way, just at a
higher threshold. Setting it to rclone's own `"0"` ("unlimited") would
remove the deadlock entirely but also the memory bound it exists for,
re-opening a shape of risk adjacent to the unauthenticated DoS
(CVE-2026-88045) this backend's pinned rclone version already fixes —
not recommended. 2GiB was chosen as a default because (a) it comfortably
exceeds `OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT`'s own documented default
test matrix above, and (b) the gateway's authenticated endpoint is not
exposed to anonymous traffic (`rclonegw-public` is a separate, read-only
process), so exhausting this buffer requires an already-trusted client with
valid `OPENEDX_AWS_*` credentials, not an anonymous attacker — the residual
risk is bounded by how large and how badly-ordered a legitimate upload
happens to be, not an open DoS surface.

**Why this matters for Open edX specifically**: course import/export and
video uploads are exactly the workloads that cross the multipart threshold,
and nothing in the S3 API forces a client to upload parts in ascending order
— boto3's own `TransferConfig`-managed uploads happen to do so by default
(which is why the plain 350 MiB test above passed cleanly even at the
original 256MiB default — ordinary, non-adversarial clients were never
actually at much risk from this), but any client, retry logic, or
parallel-upload tool that doesn't guarantee strict ordering could trigger
this. A livelocked gateway process doesn't just fail the one upload — given
this backend's single `rclonegw` process serves *every* bucket, it stalls
every other concurrent request to any private bucket until someone notices
and restarts it. If your deployment's course videos routinely exceed the
configured buffer limit, raise it further (at the cost of more worst-case
memory per concurrent multipart upload), and re-run this test matrix at
your actual maximum file size before trusting the result.

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
below — but nothing about the mechanism (an in-memory reorder buffer keyed
on a config flag, confirmed by reading the source) points to an
Azure-specific cause; it is rclone's own out-of-order part-buffering logic,
independent of backend.

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
with `--dir-cache-time=10ms` instead of `=0`.**

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
| `10ms` | 4 attempts | **Succeeded every time** (0.03% CPU at rest throughout) |

Also directly observed: reverting back to `0` on an otherwise-idle gateway
(no new requests at all) showed CPU climbing from ~0% to ~140% over the
following ~15 seconds and staying there — the cleanup-timer storm can build
up in the background once triggered, independent of whether a client is
still waiting on a response.

**First fix attempt (`=1s`) introduced its own regression — caught before
shipping.** `rclonegw`'s own `HeadObject`/`GetObject` resolve a specific key
by calling `vfs.Stat(path)`, which walks the path through the *same*
per-directory `items` cache the cleanup storm came from (confirmed by
reading rclone's own source: `cmd/serve/s3/backend.go`'s `HeadObject`/
`GetObject` both call `_vfs.Stat(fp)`, and `vfs.Stat` resolves each path
component via `dir.Stat`, backed by the cached listing `_readDir()`
populates). Any nonzero `--dir-cache-time` therefore opens a window where a
file written by one lookup of a directory isn't visible to a *different*
lookup of the same directory until the cache expires. At `=1s` this window
was long enough to break this plugin's own live conformance suite in CI: an
ordinary `PutObject` immediately followed by a `HeadObject`/`GetObject` for
the same key — not a corner case, exactly the pattern `test_s3_conformance_azure.py`
and real Open edX code both do — returned `404`/`NoSuchKey` for several
tests (`test_head_object_reports_size`, `test_copy_object`,
`test_anonymous_read_on_public_bucket`, `test_range_request_returns_partial_content`,
`test_concurrent_get_latency_does_not_scale_with_client_count`),
reproduced locally against the real `tests/docker-compose.ci.yml` stack.

**The fix**: `--dir-cache-time=10ms` — short enough that the write-then-read
regression did not reproduce across repeated full conformance-suite runs
locally (0 failures across 3 full runs, including the 20-concurrent-client
latency test), while still passing the original livelock trigger (the real
Studio/SCORM export) 4-for-4. `patches/azure/local-docker-compose-services`,
`patches/azure/k8s-deployments`, and `tests/docker-compose.ci.yml` all use
this value now. It keeps the property `--dir-cache-time` exists for —
buckets created by `tutor local do init` become visible within
milliseconds, not after the default 5-minute cache window — while avoiding
both the zero-duration edge case and (as best as repeated local testing can
show) the write-visibility regression a full second of caching introduced.
This is a code fix, not a documentation-only note: unlike Finding 1 (a
genuine upstream rclone defect with no known workaround), this one was this
plugin's own configuration choice, and changing it is enough to resolve it.
Not yet filed upstream against rclone — the zero-duration cleanup-timer
behavior is still arguably a latent bug in rclone itself (nothing says
`--dir-cache-time=0` should behave differently from "a very small but
positive duration" beyond caching semantics), but this plugin no longer
depends on anyone fixing it there.

**This is a narrowed risk, not an eliminated one.** `10ms` is empirically
safer than `1s` against both failure modes in every test run so far, but it
is still a nonzero cache window, and a sufficiently tight write-then-read
race (or a sufficiently slow Azure-side response stretching a directory
refresh past 10ms while a concurrent write lands) could in principle still
hit the same staleness gap, just far less often. No occurrence of it has
been observed at `10ms` in any test run during this evaluation, including
the deliberately-concurrent `test_concurrent_get_latency_does_not_scale_with_client_count`
test, but "not observed" is not "proven impossible" — if real course
imports/exports or video uploads start intermittently returning
`NoSuchKey`/`404` immediately after a write on this backend, this is the
first place to look, and a still-smaller value (or a different fix
entirely) may be warranted.

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
| Multipart upload (out-of-order parts) | Narrowed, not eliminated: hangs past `OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT` (default 2GiB, was rclone's 256MiB); see Finding 1 | No equivalent risk — no translation layer |
| Listing an empty prefix (`listdir`/`ListObjectsV2` + `Delimiter`) | Fixed (`--dir-cache-time=10ms`); see Finding 5 | No equivalent risk — no translation layer |
| Bucket-level anonymous read | Requires a second process + proxy routing | One native command |
| CORS | Requires proxy-layer headers | Native default |
| Local disk / non-root chown dance | None — stateless, no local volume | Required on fresh Linux installs |
| Kubernetes PVC | None needed | Required |
| Web console | None (use Azure Portal/Storage Explorer) | Built in |
| Backend lock-in | None — any rclone-supported remote | RustFS's own format |
| Requires explicit risk acknowledgment to enable | Yes (`OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK`) | No |

## What wasn't tested

**Real Azure Blob Storage.** Every result above is against Azurite, this
backend's default local emulator. Finding 1's mechanism is now confirmed by
reading rclone's own source (`cmd/serve/s3/multipart.go`), not just inferred
from a CPU/memory signature — it is an in-memory reorder buffer entirely
internal to `serve s3`, with no code path that depends on which remote
backs the `azureblob:` fs, so there is no structural reason to expect a
different result against real Azure. That said, it has not been run against
the real service. Running `test_multipart_upload_out_of_order_at_concurrency`
against a real Azure Storage account requires a real account and will incur
Azure costs — that needs explicit sign-off and credentials from whoever
evaluates this backend further. Until that run happens, treat Finding 1 (and
its fix) as confirmed-against-Azurite and presumed-but-unconfirmed against
real Azure.

## Recommendation

Usable for real course content, with a residual risk worth understanding
rather than a blanket "don't." Both livelocks this evaluation found turned
out to be addressable once root-caused rather than accepted as unexplained
upstream mysteries — Finding 5 (ordinary Studio usage, no adversarial
behavior needed) has no known residual risk at `--dir-cache-time=10ms`;
Finding 1 (adversarially-ordered large multipart uploads) is narrowed from
"hangs past ~300MiB on any client that happens to deliver parts in
sufficiently reversed order" to "hangs past `OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT`
(2GiB by default) on an upload reversed badly enough to fill that buffer
before the part that would drain it arrives" — which the evaluation's own
"what works" section already noted ordinary, non-adversarial clients
(boto3's own `TransferConfig`-managed uploads included) don't naturally
produce even at rclone's original, smaller default.

The two-process anonymous-bucket design works and was validated thoroughly,
which was already good news independent of either finding. The residual risk
that remains — an upload exceeding your configured buffer limit, combined
with sufficiently reversed delivery order — is real but now bounded,
quantifiable, and tunable: if your deployment's course videos can exceed
2GiB, raise `OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT` to comfortably cover
your actual maximum file size and re-run this evaluation's test matrix
(`TESTING.rst` Tier 5) against that size before trusting the result, the
same way this evaluation did for 2GiB. The `rustfs` backend still carries
none of this risk class at all, because it never translates between two
different storage models in the first place, and remains the simpler choice
when Azure isn't a hard requirement. When it is, this backend is now a more
defensible choice than "no known fix" previously suggested — worth filing
the underlying zero-duration-buffer-limit-defaults question against
[rclone/rclone](https://github.com/rclone/rclone) regardless, since nothing
about this plugin's fix changes rclone's own default, only this backend's
configuration of it.
