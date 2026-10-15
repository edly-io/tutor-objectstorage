Testing tutor-objectstorage
===========================

Five tiers, cheapest first, covering all three backends. Tiers 1-3 apply to
every backend; Tier 4 (live S3 conformance) has a suite per backend; Tier 5
(real Azure) only applies to the ``azure`` backend, since it is the only one
with an unresolved open question to settle.

.. contents::
   :local:
   :depth: 1

Tier 1 — static checks and template rendering
----------------------------------------------

No Docker, no Open edX, runs in a few seconds. This is what CI runs on every
pull request.

::

    pip install -e ".[dev]"
    make test

That runs lint, formatting, type checks, and the unit suite. The unit suite
alone::

    make test-unit

What it covers, beyond the usual (every config key is ``OBJSTORAGE_``-prefixed,
no ``MINIO_``/``RUSTFS_``/``RCLONEGW_``/``S3_`` key survives, every patch
renders for every backend, the init-job naming contract, the
``discovery``/``xqueue`` cross-plugin patches):

- **Selecting one backend never renders another backend's infrastructure or
  config.** The cross-backend containment suite in ``test_patches.py``
  renders every infra-relevant patch for each of the three backends and
  asserts none of the other two backends' markers (service names, CLI flags,
  env var names) appear in the output. This is the regression guard specific
  to this plugin's modular, self-guarded-fragment-per-backend design — a
  forgotten ``{% if OBJSTORAGE_BACKEND == "..." %}`` guard is exactly what it
  exists to catch.
- **The anonymous gateway process (azure backend) can never be configured to
  reference a private bucket or accept writes.** ``rclonegw-public``'s
  rendered command is asserted to have no ``--auth-key``, to have
  ``--read-only``, and its ``--include`` list is asserted to name only the
  intended public bucket(s) — never grades, ORA2 uploads, video, or
  learning-core buckets.
- **The Caddyfile's conditional discovery-bucket clause (azure backend)
  cannot regress.** An adversarial design review caught a real bug before
  tutor-rclone-gateway (this backend's predecessor) shipped: an unguarded
  second path token would collapse to a bare ``/*`` whenever the
  ``discovery`` plugin is disabled, routing *every* bucket to the
  unauthenticated backend. ``test_caddyfile_discovery_clause_is_safe_when_disabled``
  renders the patch with discovery off and asserts the path matcher is
  exactly one token.
- **The azure backend's infrastructure patches and init script render
  empty without the risk acknowledgment.** Confirmed both via a dedicated
  unit test (renders with ``OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK=False`` and
  asserts every infra patch is blank) and live, by hand, against the real
  CLI — see Tier 2.
- **Kubernetes Services are ``ClusterIP`` for the azure backend, and a
  ``NetworkPolicy`` restricts ingress to the Caddy pod.** Both gateway
  Deployments would otherwise be reachable from any pod in the cluster via
  Service DNS.
- **No PVC, bind mount, or local data volume exists for the azure
  backend** — all object data lives in Azure. The rustfs backend is the
  opposite: a PVC/bind mount is required, and is asserted present.
- **The external_s3 backend renders no local infrastructure at all** — no
  compose service, no Kubernetes resource, no Caddy vhost — except the
  minimal stub job service every backend must contribute to the shared
  ``objectstorage-job`` slot (see the plan's "Init task unification").

To confirm the suite is not passing vacuously, break something on purpose and
check it goes red::

    sed -i 's/--read-only//' tutorobjectstorage/patches/azure/local-docker-compose-services
    make test-unit   # expect a public_service_is_read_only-style test to fail
    git checkout tutorobjectstorage/patches/azure/local-docker-compose-services

Tier 2 — generated configuration
---------------------------------

Checks that Tutor produces a valid stack, without starting it. Use a
throwaway root so your real deployment is untouched::

    export TUTOR_ROOT=/tmp/tutor-objectstorage-test
    tutor plugins enable objectstorage
    tutor config save          # defaults to OBJSTORAGE_BACKEND=rustfs
    tutor local dc config > /dev/null && echo "compose OK"

Switching backends in the same root (confirmed to work this way, live)::

    tutor config save --set OBJSTORAGE_BACKEND=azure
    # Error: OBJSTORAGE_BACKEND is 'azure', but OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK
    # is not set. Config is saved to disk at this point but the environment
    # is NOT regenerated — this is the hard-fail working as designed.
    tutor config save --set OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK=true
    # Now succeeds and regenerates the environment.

    tutor config save \
        --set OBJSTORAGE_BACKEND=external_s3 \
        --set OBJSTORAGE_EXTERNAL_S3_ENDPOINT_HOST=<host> \
        --set OPENEDX_AWS_ACCESS_KEY=<key> \
        --set OPENEDX_AWS_SECRET_ACCESS_KEY=<secret>

Then read the rendered output::

    tutor config printvalue OBJSTORAGE_BACKEND
    grep -A20 '^  rustfs:' "$TUTOR_ROOT/env/local/docker-compose.yml"      # rustfs
    grep -A20 '^  rclonegw:' "$TUTOR_ROOT/env/local/docker-compose.yml"    # azure
    grep AWS_S3_ENDPOINT_URL "$TUTOR_ROOT/env/apps/openedx/settings/lms/production.py"

For Kubernetes::

    tutor k8s apply --dry-run=client -f "$TUTOR_ROOT/env/k8s/deployments.yml"
    # The azure backend's NetworkPolicy resources live in override.yml:
    # there is no dedicated Tutor core patch hook for this resource kind.
    tutor k8s apply --dry-run=client -f "$TUTOR_ROOT/env/k8s/override.yml"

Tier 3 — live deployment
-------------------------

A real stack. The rustfs backend needs no external account; the azure
backend defaults to the bundled Azurite emulator (plus the risk
acknowledgment); the external_s3 backend needs a real or stand-in external
S3-compatible endpoint with its buckets already created::

    tutor local launch

Checks, in order, for whichever backend is active:

1. **The services are up** (rustfs/azure only — external_s3 has none)::

       tutor local logs rustfs                              # rustfs
       tutor local logs rclonegw rclonegw-public azurite     # azure

2. **The init task runs and is idempotent.** Run it twice; the second run
   must also succeed, and for ``external_s3`` must print the documented
   "nothing to provision" message rather than erroring::

       tutor local do init --limit=objectstorage
       tutor local do init --limit=objectstorage

   ``--limit`` matches by **plugin** name (``objectstorage``), not the
   underlying job/service name (``objectstorage-job``) — this plugin's init
   task is deliberately registered under the same name as the plugin itself
   to avoid the naming-mismatch trap tutor-rclone-gateway had
   (``--limit=rclonegw`` silently matched nothing there; it had to be
   ``--limit=rclonegateway``).

3. **(rustfs, azure) The public bucket is anonymously readable but not
   writable; a private bucket is not readable at all.**

   ::

       # replace <HOST> with the relevant OBJSTORAGE_*_HOST value
       curl -i https://<HOST>/openedx/does-not-exist        # 404, not a TLS/connection error
       curl -i -X PUT -d x https://<HOST>/openedx/probe.txt  # expect 403/405 (rustfs) or 400/403/404/405 (azure)
       curl -i https://<HOST>/openedxgrades/does-not-exist   # expect 403/404, never 200

4. **Open edX can write.** Upload a course asset in Studio and confirm it
   appears in the right bucket. Then upload a video and confirm it lands in
   ``openedxvideos`` — video goes through the multipart path, which fails
   differently from small uploads.

5. **(rustfs only) Data survives a restart; (azure) the gateway reconnects
   correctly.**

   ::

       tutor local restart rustfs                     # rustfs: confirms the bind-mounted volume persisted
       tutor local restart rclonegw rclonegw-public    # azure: data lives in Azure either way, this checks reconnection

.. note::

   Only the rustfs backend has a web console and a non-root chown step
   required on a fresh Linux install — see the README.

Tier 4 — S3 conformance
-------------------------

One live boto3 suite per backend, using the same client configuration Open
edX uses. Each is skipped unless its ``OBJSTORAGE_TEST_ENDPOINT`` is set, so
``make test`` stays offline.

rustfs::

    pip install boto3 requests
    export OBJSTORAGE_TEST_ENDPOINT=http://localhost:9000
    export OBJSTORAGE_TEST_ACCESS_KEY="$(tutor config printvalue OPENEDX_AWS_ACCESS_KEY)"
    export OBJSTORAGE_TEST_SECRET_KEY="$(tutor config printvalue OPENEDX_AWS_SECRET_ACCESS_KEY)"
    pytest -v tests/test_s3_conformance_rustfs.py

azure — **run this in `tutor local launch` mode, not `tutor dev`**: dev mode
runs no Caddy at all, so the public/private bucket split this suite spends
most of its time testing cannot exist there::

    pip install boto3 requests
    export OBJSTORAGE_TEST_ENDPOINT=https://<OBJSTORAGE_AZURE_HOST>
    export OBJSTORAGE_TEST_ACCESS_KEY="$(tutor config printvalue OPENEDX_AWS_ACCESS_KEY)"
    export OBJSTORAGE_TEST_SECRET_KEY="$(tutor config printvalue OPENEDX_AWS_SECRET_ACCESS_KEY)"
    pytest -v tests/test_s3_conformance_azure.py

Both were confirmed passing against a real local deployment during this
plugin's own development: the rustfs suite against a genuine RustFS
container (including a real in-order 40 MiB multipart upload and anonymous
read/write ACL checks), and the full azure suite — 15 passed, 6 intentionally
skipped — against the real rclonegw/rclonegw-public/Caddy/Azurite stack,
including the public/private routing split exercised *through* Caddy by
hostname, not just at the container level.

The azure suite covers everything the rustfs suite does, plus several checks
with no rustfs equivalent because they only matter for a translation-layer
gateway:

- Anonymous GET succeeds, anonymous PUT/DELETE are rejected, on the public
  bucket — **through Caddy**, which exercises routing and backend scoping
  together.
- Anonymous GET against a **private** bucket, through the same public-facing
  host, is rejected. This is the regression guard for the Caddyfile bug
  mentioned in Tier 1.
- CORS headers are present on a cross-origin request.
- A Range request returns the correct bytes and ``Content-Range`` header.
  **Confirmed quirk, not a test bug**: the HTTP status stays ``200`` rather
  than the spec-required ``206`` — see ``EVALUATION.md`` Finding 3.
- A **350 MiB**, normally-ordered (boto3-managed) multipart upload — past
  rclone's 256 MiB default streaming buffer. This passes cleanly and
  quickly. It does **not** mean multipart uploads are safe here — see the
  next paragraph.

**A more adversarial multipart test, run during tutor-rclone-gateway's own
evaluation (this backend's predecessor), found a reproducible hang — against
Azurite, before ever reaching Tier 5.** Parts submitted via raw
``upload_part`` calls in **strictly reversed order** complete fine up to
260 MiB and hang indefinitely (CPU pinned, zero network progress, no
self-recovery) at 300 MiB and above, confirmed at concurrency=1 alone (not
concurrency-dependent). Full details, exact reproduction steps, and the
bisection table are in ``EVALUATION.md``. This is gated the same way as Tier
5 below (``OBJSTORAGE_TEST_REAL_AZURE_GATE=1``) but reproduces against the
default Azurite backend — you do not need real Azure credentials to see it.

- A concurrency-latency check: 20 concurrent GETs must not show p99 latency
  scaling linearly with client count, which would reproduce a real,
  previously-shipped rclone bug (a lock held for an entire HTTP transaction,
  fixed in rclone 1.69). This one is **required, not skip-if-flaky**.

Two tests are opt-in and excluded by default::

    # Multipart server-side copy: rclone's own docs call this path
    # "unreliable and extremely slow, eventually failing" (issue #7454).
    OBJSTORAGE_TEST_SLOW=1 pytest -v tests/test_s3_conformance_azure.py -k multipart_server_side_copy

external_s3 — there is no automated live suite for this backend: "live" means
a real or stand-in external provider, which varies by provider and has no
one-size-fits-all CI story. During this plugin's own development it was
verified by standing up a standalone S3-compatible container (RustFS,
running completely outside Tutor, as a stand-in for "some external vendor"),
pre-creating its buckets by hand (matching this backend's documented
"buckets pre-exist" philosophy), pointing ``OBJSTORAGE_EXTERNAL_S3_*`` at it,
and confirming: zero local infrastructure renders; the init task prints its
documented no-op message rather than erroring or silently doing nothing; a
full authenticated read/write round trip succeeds using the plugin's own
rendered settings; and the profile-image-bucket override's two behaviors
(default → shared bucket, empty string → no override rendered at all, so
Open edX's own local-filesystem default survives) both hold, confirmed by
inspecting the actual rendered settings file before and after.

Tier 5 — real Azure: does the already-confirmed hang also happen there?
--------------------------------------------------------------------------

**The azure backend already failed Tier 4's adversarial multipart check,
against Azurite, before Tier 5 was ever run — see EVALUATION.md Finding 1.**
That alone is enough to not trust this backend with production course data;
Tier 5 is not what tells you whether to trust it, Tier 4 already did. What
Tier 5 *adds* is whether the hang is specifically about rclone's own request
handling (reproduces identically against real Azure) or has an
Azure-specific flavor. Either answer is worth recording; neither changes the
Tier 4 result.

1. Create a real Azure Storage account and override, together, in addition
   to the risk acknowledgment::

       tutor config save \
           --set OBJSTORAGE_BACKEND=azure \
           --set OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK=true \
           --set OBJSTORAGE_AZURE_ACCOUNT=<your-account> \
           --set OBJSTORAGE_AZURE_ACCOUNT_KEY=<your-key> \
           --set OBJSTORAGE_AZURE_ENDPOINT=https://<your-account>.blob.core.windows.net
       tutor local launch

2. Reproduce the Tier 4 hang first, at the same size that triggered it
   against Azurite, before spending time on the full concurrency/size sweep::

       export OBJSTORAGE_TEST_REAL_AZURE_GATE=1
       export OBJSTORAGE_TEST_REAL_AZURE_SIZE_MIB=300
       pytest -v "tests/test_s3_conformance_azure.py::test_multipart_upload_out_of_order_at_concurrency[1]"

3. If it reproduces (expected, given the hang's CPU/network signature looks
   like rclone's own logic rather than anything Azure-specific), that's the
   answer. If it does **not** reproduce against real Azure, that is itself an
   important and surprising finding worth the full sweep (concurrency
   1/2/4/8/10, sizes up to several GiB) to characterize.

4. Record whatever happens in ``EVALUATION.md``, including the exact rclone
   image tag tested.

This tier costs real Azure spend and needs real credentials — run it
deliberately, not as part of routine development.

Known failure modes
--------------------

.. list-table::
   :header-rows: 1

   * - Symptom
     - Backend
     - Cause
   * - ``SignatureDoesNotMatch``
     - any
     - Access key or secret differs between the backend and Open edX.
   * - Uploads fail with a checksum error
     - any
     - The ``request_checksum_calculation`` setting was removed from
       ``openedx-common-settings``.
   * - ``NoSuchBucket`` on first upload
     - rustfs, azure
     - ``tutor local do init`` never ran.
   * - ``NoSuchBucket`` on first upload
     - external_s3
     - Buckets were never created — this backend never provisions them;
       see the README.
   * - ``OBJSTORAGE_BACKEND is 'azure', but OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK
       is not set``
     - azure
     - Expected, not a bug — set the flag only after reading EVALUATION.md.
   * - Anonymous GET on the public bucket returns 403
     - rustfs
     - ``rc anonymous set download`` never ran against this bucket.
   * - Anonymous GET on the public bucket returns 403/404/400
     - azure
     - ``rclonegw-public`` isn't up, or its ``--include`` doesn't cover this
       bucket, or the Caddyfile's path matcher doesn't match — check all
       three before assuming it's an Azure-side permissions issue.
   * - Anonymous GET on a *private* bucket returns 200
     - any
     - Stop and treat this as a security incident, not a test failure to
       work around.
   * - ``tutor local do init`` says no such service
     - any
     - The job service name no longer matches the shared
       ``objectstorage-job`` slot. Tier 1 catches this.
   * - A request with no credentials at all returns
       ``400 UnsupportedAlgorithm`` instead of ``403``
     - azure
     - Confirmed rclone behavior, not a misconfiguration — a
       correctly-signed-but-wrong-key request still gets the expected
       ``403 InvalidAccessKeyId``. See ``EVALUATION.md`` Finding 2.
   * - ``rclonegw`` is pinned near 100%+ CPU with no network I/O, and stops
       responding
     - azure
     - The multipart hang (``EVALUATION.md`` Finding 1) or the empty-prefix
       listing hang (Finding 5 — far easier to hit: triggered by exporting
       any course with a SCORM component, or any ``listdir()``/``exists()``
       check against a path nothing has been written to yet).
       ``tutor local restart rclonegw`` alone is often **not** enough: if the
       client that triggered it is still retrying (e.g. a stuck Celery
       task), it immediately re-triggers the hang against the freshly
       restarted gateway. Restart the client too (e.g.
       ``tutor local restart cms-worker lms-worker``).
