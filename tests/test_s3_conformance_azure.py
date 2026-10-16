"""Live S3 conformance checks against a running azure backend.

Skipped unless ``OBJSTORAGE_TEST_ENDPOINT`` is set, so ``make test`` stays
offline. The generic-verb tests near the top mirror
test_s3_conformance_rustfs.py's equivalent suite; everything below the
"anonymous/private bucket split" section has no rustfs equivalent at all,
because those tests exist specifically *because* this backend is a gateway
(a translation layer in front of a backend that was never designed to
speak S3) rather than a native S3 engine. Ported from
tutor-rclone-gateway's own test_s3_conformance.py. See EVALUATION.md for
how these results were used, and TESTING.rst for the full tiered
procedure, including the manual, secrets-gated real-Azure tier at the
bottom of this file.

Run against a Tutor deployment in local (not dev) mode, so Caddy is
running and the public/private split is actually exercised::

    export OBJSTORAGE_TEST_ENDPOINT=https://<OBJSTORAGE_AZURE_HOST>
    export OBJSTORAGE_TEST_ACCESS_KEY="$(tutor config printvalue \
        OPENEDX_AWS_ACCESS_KEY)"
    export OBJSTORAGE_TEST_SECRET_KEY="$(tutor config printvalue \
        OPENEDX_AWS_SECRET_ACCESS_KEY)"
    pip install boto3 requests
    pytest -v tests/test_s3_conformance_azure.py
"""

from __future__ import annotations

import io
import os
import statistics
import time
import typing as t
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

ENDPOINT = os.environ.get("OBJSTORAGE_TEST_ENDPOINT")

pytestmark = pytest.mark.skipif(
    not ENDPOINT, reason="set OBJSTORAGE_TEST_ENDPOINT to run live S3 checks"
)

# Past rclone's own 256 MiB default --multipart-streaming-buffer-limit
# (this backend now raises it to OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT,
# 2GiB by default - see EVALUATION.md Finding 1), and past the ~322 MiB
# point a real user hit an unexplained multi-minute stall uploading a
# 2 GiB file to `rclone serve s3` (rclone issue #7453, root cause never
# conclusively pinned) — a token "just enough to force multiple parts"
# size would not have caught that. This normally-ordered test doesn't need
# to cross this backend's own (now much larger) buffer limit to be
# meaningful: in-order parts admit immediately regardless of buffer
# fullness (see Finding 1), so this is testing ordinary multipart delivery
# at a realistic size, not the buffer boundary itself - the adversarial
# out-of-order test below is what exercises that boundary, deliberately,
# via OBJSTORAGE_TEST_REAL_AZURE_SIZE_MIB.
MULTIPART_SIZE = 350 * 1024 * 1024
# boto3's own default multipart chunk size. An artificially small part
# size "just to force multiple parts" would not exercise the same
# chunking behavior real traffic (course exports, video uploads) produces.
MULTIPART_PART_SIZE = 8 * 1024 * 1024


@pytest.fixture(scope="module")
def s3() -> t.Any:
    boto3 = pytest.importorskip("boto3")
    from botocore.client import Config

    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=os.environ.get("OBJSTORAGE_TEST_ACCESS_KEY", "openedx"),
        aws_secret_access_key=os.environ["OBJSTORAGE_TEST_SECRET_KEY"],
        region_name=os.environ.get("OBJSTORAGE_TEST_REGION", "us-east-1"),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            # Mirrors AWS_S3_CLIENT_CONFIG in openedx-common-settings.
            # Without it, boto3 >= 1.36 sends CRC32 checksums that
            # non-AWS S3 servers reject.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


@pytest.fixture(scope="module")
def bucket() -> str:
    return os.environ.get("OBJSTORAGE_TEST_BUCKET", "openedx")


@pytest.fixture(scope="module")
def private_bucket() -> str:
    return os.environ.get("OBJSTORAGE_TEST_PRIVATE_BUCKET", "openedxgrades")


@pytest.fixture(scope="module")
def public_host() -> str:
    """The Caddy-fronted hostname the public bucket is reachable at.

    Defaults to OBJSTORAGE_TEST_ENDPOINT itself: Caddy serves both the
    authenticated and anonymous backends on the *same* hostname and
    splits by method + path, not by host.
    """
    return os.environ.get("OBJSTORAGE_TEST_PUBLIC_HOST", ENDPOINT)


@pytest.fixture(scope="module")
def public_direct_endpoint() -> str | None:
    return os.environ.get("OBJSTORAGE_TEST_PUBLIC_DIRECT_ENDPOINT")


@pytest.fixture
def key(s3: t.Any, bucket: str) -> t.Iterator[str]:
    name = f"tutor-objectstorage-azure-test/{uuid.uuid4()}.bin"
    yield name
    try:
        s3.delete_object(Bucket=bucket, Key=name)
    except Exception:  # noqa: BLE001 - cleanup must not mask failures
        pass


# --- generic S3 verbs, same contract test_s3_conformance_rustfs.py tests --


def test_list_buckets(s3: t.Any, bucket: str) -> None:
    names = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    assert bucket in names, f"bucket {bucket!r} missing; run `tutor local do init`"


def test_put_and_get_roundtrip(s3: t.Any, bucket: str, key: str) -> None:
    body = b"tutor-objectstorage azure conformance"
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == body


def test_head_object_reports_size(s3: t.Any, bucket: str, key: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=b"x" * 128)
    assert s3.head_object(Bucket=bucket, Key=key)["ContentLength"] == 128


def test_presigned_get_url_is_fetchable(s3: t.Any, bucket: str, key: str) -> None:
    """Grades exports and private downloads are served this way."""
    requests = pytest.importorskip("requests")
    body = b"presigned"
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300
    )
    response = requests.get(url, timeout=30)
    assert response.status_code == 200, response.text[:400]
    assert response.content == body


def test_list_objects_v2(s3: t.Any, bucket: str, key: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=b"listed")
    listing = s3.list_objects_v2(
        Bucket=bucket, Prefix="tutor-objectstorage-azure-test/"
    )
    assert key in {o["Key"] for o in listing.get("Contents", [])}


def test_copy_object(s3: t.Any, bucket: str, key: str) -> None:
    """Course import/export copies objects server-side."""
    s3.put_object(Bucket=bucket, Key=key, Body=b"original")
    dest = f"{key}.copy"
    try:
        s3.copy_object(
            Bucket=bucket, Key=dest, CopySource={"Bucket": bucket, "Key": key}
        )
        assert s3.get_object(Bucket=bucket, Key=dest)["Body"].read() == b"original"
    finally:
        s3.delete_object(Bucket=bucket, Key=dest)


def test_delete_object(s3: t.Any, bucket: str, key: str) -> None:
    from botocore.exceptions import ClientError

    s3.put_object(Bucket=bucket, Key=key, Body=b"transient")
    s3.delete_object(Bucket=bucket, Key=key)
    with pytest.raises(ClientError):
        s3.head_object(Bucket=bucket, Key=key)


# --- the anonymous/private bucket split: no rustfs equivalent -------------
#
# rclone's `serve s3` has no bucket-level ACL/policy mechanism at all, so
# this backend fronts two separate `rclone serve s3` processes behind one
# Caddy host instead of one server with a bucket policy (see the
# caddyfile and local-docker-compose-services patches). These tests exist
# to prove that split actually holds at runtime, not just that it looks
# right in the rendered templates (test_patches.py covers that half).


def test_anonymous_read_on_public_bucket(
    public_host: str, bucket: str, s3: t.Any, key: str
) -> None:
    """The main bucket must be anonymously readable: forum image uploads
    are plain <img> tags baked into stored post HTML, so presigned URLs
    don't work for them — they would eventually expire."""
    requests = pytest.importorskip("requests")
    s3.put_object(Bucket=bucket, Key=key, Body=b"public")
    response = requests.get(f"{public_host}/{bucket}/{key}", timeout=30)
    assert response.status_code == 200, (
        f"anonymous read failed ({response.status_code}); check "
        "rclonegw-public is up and --include covers this bucket"
    )
    assert response.content == b"public"


#: rclone's serve s3 rejects a request that carries NO Authorization
#: header at all with "400 UnsupportedAlgorithm" rather than a standard
#: S3 403 — confirmed live against this plugin's own stack: a
#: correctly-SigV4-signed-but-wrong-credentials request gets the
#: expected, standard "403 InvalidAccessKeyId" instead, so this is
#: specifically a quirk of the zero-auth-header case, not a broken auth
#: check. Either way nothing here is a security gap (the request is
#: genuinely rejected, not served) — just a non-standard error code
#: worth knowing about when debugging a client that expects AWS's usual
#: vocabulary. See EVALUATION.md.
ANONYMOUS_REJECTION_CODES = (400, 403, 404, 405)


def test_anonymous_write_is_rejected_on_public_bucket(
    public_host: str, bucket: str
) -> None:
    """The public bucket must be readable, not writable. A PUT (any
    method other than GET/HEAD) never reaches rclonegw-public at all —
    Caddy's method-based routing sends it straight to the authenticated
    backend, which rejects it for lacking credentials. rclonegw-public's
    own --read-only flag is a separate, additional guarantee, exercised
    directly by test_rclonegw_public_isolation_holds_even_bypassing_caddy."""
    requests = pytest.importorskip("requests")
    probe = (
        f"{public_host}/{bucket}/tutor-objectstorage-azure-test/anon-write-probe.txt"
    )
    response = requests.put(probe, data=b"should be rejected", timeout=30)
    assert response.status_code in ANONYMOUS_REJECTION_CODES, (
        f"anonymous write returned {response.status_code}, a 2xx — the "
        "write was not rejected at all"
    )
    delete = requests.delete(f"{public_host}/{bucket}/", timeout=30)
    assert delete.status_code in ANONYMOUS_REJECTION_CODES


def test_anonymous_read_on_private_bucket_is_rejected(
    public_host: str, private_bucket: str
) -> None:
    """The regression guard for Caddy routing + rclonegw-public
    --include scoping working *together*: an anonymous request against a
    PRIVATE bucket, through the same public-facing host, must not
    succeed. This is the test that would have caught the caddyfile
    discovery-clause bug this backend was specifically built to avoid (see
    test_patches.py) if it had shipped anyway."""
    requests = pytest.importorskip("requests")
    response = requests.get(f"{public_host}/{private_bucket}/anything", timeout=30)
    assert response.status_code in ANONYMOUS_REJECTION_CODES, (
        f"anonymous read of a PRIVATE bucket returned {response.status_code} "
        "through the public-facing host — this is a real data leak, not a "
        "cosmetic issue"
    )


def test_rclonegw_public_isolation_holds_even_bypassing_caddy(
    public_direct_endpoint: str | None, private_bucket: str, bucket: str
) -> None:
    """Hit rclonegw-public directly, bypassing Caddy's method+path
    routing entirely, to prove the --include/--read-only scoping is a
    real VFS-layer boundary and not just something Caddy happens to
    enforce. Set OBJSTORAGE_TEST_PUBLIC_DIRECT_ENDPOINT (e.g.
    http://localhost:9002 in dev mode, or the CI compose stack) to run
    this."""
    if not public_direct_endpoint:
        pytest.skip("set OBJSTORAGE_TEST_PUBLIC_DIRECT_ENDPOINT to run this check")
    requests = pytest.importorskip("requests")
    response = requests.get(f"{public_direct_endpoint}/{private_bucket}/x", timeout=30)
    assert response.status_code in (403, 404)
    buckets_xml = requests.get(f"{public_direct_endpoint}/", timeout=30).text
    assert private_bucket not in buckets_xml, (
        "ListBuckets on the anonymous instance exposes a private bucket "
        "name — even if its contents are protected, this is still an "
        "information leak the --include scoping should prevent entirely"
    )
    assert bucket in buckets_xml


# --- browser-facing behavior: CORS and Range ------------------------------
#
# Forum images and video content are fetched directly by learner
# browsers; video scrubbing relies on Range requests. RustFS provides
# both for free; rclone's `serve s3` provides neither on its own.


def test_cors_header_present_on_public_bucket(public_host: str, bucket: str) -> None:
    requests = pytest.importorskip("requests")
    response = requests.options(
        f"{public_host}/{bucket}/",
        headers={"Origin": "https://example.com"},
        timeout=30,
    )
    assert response.headers.get("Access-Control-Allow-Origin"), (
        "no Access-Control-Allow-Origin header; direct browser fetch/XHR "
        "against this host (not just <img> tags) will be silently blocked"
    )


def test_range_request_returns_partial_content(
    s3: t.Any, bucket: str, key: str
) -> None:
    """Confirmed live: rclone's serve s3 honors the Range header for the
    DATA it returns (correct byte slice, correct Content-Range header)
    but — unlike real AWS S3/RustFS — does not switch the HTTP status
    code to 206, returning 200 instead. This is a genuine S3-API-spec
    deviation (both RFC 7233 and the S3 API require 206 for a satisfied
    Range request), not a cosmetic detail: an HTTP client that branches
    on status code rather than the presence of Content-Range may not
    treat this as a partial response. See EVALUATION.md. The assertions
    below pin the *actual* behavior (right bytes, right header,
    non-compliant status) so a silent regression in either direction -
    data or status - gets caught.
    """
    body = b"0123456789" * 100
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    response = s3.get_object(Bucket=bucket, Key=key, Range="bytes=100-199")
    status = response["ResponseMetadata"]["HTTPStatusCode"]
    headers = response["ResponseMetadata"]["HTTPHeaders"]
    assert status in (200, 206), f"unexpected status {status} for a Range request"
    if status == 200:
        assert headers.get("content-range") == f"bytes 100-199/{len(body)}", (
            "status 200 AND a missing/wrong Content-Range would mean the "
            "full object was returned, not a partial one"
        )
    assert response["Body"].read() == body[100:200]


# --- concurrency: regression guard for a real, previously-shipped bug ----


def test_concurrent_get_latency_does_not_scale_with_client_count(
    s3: t.Any, bucket: str, key: str
) -> None:
    """A lock in rclone's serve-s3 layer was once held for an entire HTTP
    transaction, serializing every other client behind one slow request
    (fixed in rclone 1.69). If p99 latency scales ~linearly with
    concurrent client count, that is the same serialization signature
    recurring. This is required, not skip-if-flaky: skipping it on
    flakiness would discard exactly the evidence it exists to produce."""
    body = b"x" * (64 * 1024)
    s3.put_object(Bucket=bucket, Key=key, Body=body)

    def timed_get(_: int) -> float:
        start = time.monotonic()
        s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return time.monotonic() - start

    def p99(workers: int) -> float:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            latencies = list(pool.map(timed_get, range(20)))
        return statistics.quantiles(latencies, n=100)[98]

    baseline = p99(1)
    concurrent = p99(20)
    assert concurrent < max(baseline * 5, 1.0), (
        f"p99 latency at 20 concurrent clients ({concurrent:.2f}s) is more "
        f"than 5x the single-client baseline ({baseline:.2f}s) — this looks "
        "like request serialization, not normal contention"
    )


# --- multipart: the central open question for this architecture ----------
#
# Open edX's own community already hit a real failure running this same
# shape of architecture (MinIO gateway mode in front of Azure Blob
# Storage): multipart course-export uploads failed to complete. This test
# is sized and chunked to actually probe for an rclone equivalent, not
# just exercise the happy path at a token size. Run against Azurite (the
# default backend), this is a correctness smoke test, not proof the
# Azure-specific failure mode is absent: Azurite's own docs disclaim any
# performance guarantee and don't model large concurrent-client behavior,
# which is precisely the dimension that matters. See the real-Azure tier
# at the bottom of this file and EVALUATION.md.


def test_large_multipart_upload(s3: t.Any, bucket: str, key: str) -> None:
    from boto3.s3.transfer import TransferConfig

    payload = io.BytesIO(os.urandom(MULTIPART_SIZE))
    s3.upload_fileobj(
        payload,
        bucket,
        key,
        Config=TransferConfig(
            multipart_threshold=MULTIPART_PART_SIZE,
            multipart_chunksize=MULTIPART_PART_SIZE,
        ),
    )
    assert s3.head_object(Bucket=bucket, Key=key)["ContentLength"] == MULTIPART_SIZE


@pytest.mark.skipif(
    not os.environ.get("OBJSTORAGE_TEST_SLOW"),
    reason="set OBJSTORAGE_TEST_SLOW=1 to run this (rclone's own docs "
    "describe multipart server-side copy as unreliable and extremely "
    "slow, eventually failing — issue #7454)",
)
def test_multipart_server_side_copy(s3: t.Any, bucket: str, key: str) -> None:
    """Course re-export relies on exactly this path at scale, so a slow
    failure here counts as a failure, not a timeout to retry past."""
    from boto3.s3.transfer import TransferConfig

    payload = io.BytesIO(os.urandom(MULTIPART_SIZE))
    s3.upload_fileobj(
        payload,
        bucket,
        key,
        Config=TransferConfig(
            multipart_threshold=MULTIPART_PART_SIZE,
            multipart_chunksize=MULTIPART_PART_SIZE,
        ),
    )
    dest = f"{key}.copy"
    try:
        s3.copy_object(
            Bucket=bucket, Key=dest, CopySource={"Bucket": bucket, "Key": key}
        )
        copy_size = s3.head_object(Bucket=bucket, Key=dest)["ContentLength"]
        assert copy_size == MULTIPART_SIZE
    finally:
        s3.delete_object(Bucket=bucket, Key=dest)


# --- the decisive test: real Azure, realistic size, deliberate disorder --
#
# Manual, secrets-gated, and required at least once against real Azure
# before publishing any conclusion about multipart viability — see
# TESTING.rst and EVALUATION.md. Deliberately uses raw upload_part calls
# rather than upload_fileobj/TransferConfig, which hide part ordering:
# ordinary S3 clients are allowed to upload parts out of order and
# concurrently, and rclone's own docs say out-of-order parts are merely
# "buffered until their turn", not rejected — if that buffering has a
# bug, this is what would surface it.


@pytest.mark.skipif(
    not os.environ.get("OBJSTORAGE_TEST_REAL_AZURE_GATE"),
    reason="set OBJSTORAGE_TEST_REAL_AZURE_GATE=1 to run this against a "
    "real Azure Storage account (not Azurite) — see TESTING.rst",
)
@pytest.mark.parametrize("concurrency", [1, 2, 4, 8, 10])
def test_multipart_upload_out_of_order_at_concurrency(
    s3: t.Any, bucket: str, concurrency: int
) -> None:
    size_mib = int(os.environ.get("OBJSTORAGE_TEST_REAL_AZURE_SIZE_MIB", "1024"))
    size = size_mib * 1024 * 1024
    part_size = MULTIPART_PART_SIZE
    num_parts = max(size // part_size, 1)
    test_key = (
        f"tutor-objectstorage-azure-test/multipart-{concurrency}-{uuid.uuid4()}.bin"
    )
    parts_data = [os.urandom(part_size) for _ in range(num_parts)]

    created = s3.create_multipart_upload(Bucket=bucket, Key=test_key)
    upload_id = created["UploadId"]
    try:
        order = list(range(len(parts_data)))[::-1]  # deliberately reversed

        def upload_one(i: int) -> dict[str, t.Any]:
            part_number = i + 1
            resp = s3.upload_part(
                Bucket=bucket,
                Key=test_key,
                PartNumber=part_number,
                UploadId=upload_id,
                Body=parts_data[i],
            )
            return {"PartNumber": part_number, "ETag": resp["ETag"]}

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            parts = list(pool.map(upload_one, order))
        parts.sort(key=lambda p: t.cast(int, p["PartNumber"]))

        s3.complete_multipart_upload(
            Bucket=bucket,
            Key=test_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        head = s3.head_object(Bucket=bucket, Key=test_key)
        assert head["ContentLength"] == num_parts * part_size
    except Exception:
        s3.abort_multipart_upload(Bucket=bucket, Key=test_key, UploadId=upload_id)
        raise
    finally:
        try:
            s3.delete_object(Bucket=bucket, Key=test_key)
        except Exception:  # noqa: BLE001 - cleanup must not mask failures
            pass
