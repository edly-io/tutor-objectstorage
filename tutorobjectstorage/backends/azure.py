from .base import Backend

# -----------------------------------------------------------------------------
# Azure Blob Storage via rclone's `serve s3` translation layer.
#
# MinIO used to offer a "gateway" mode that translated the S3 API onto
# external backends, including Azure Blob Storage (which has no S3 API of
# its own). MinIO deprecated gateway mode in 2022; RustFS, its replacement,
# never had one. This backend evaluates rclone (https://rclone.org) as a
# replacement for that lost capability: Open edX keeps talking to a plain S3
# endpoint, and `rclone serve s3` translates onto an `azureblob:` remote
# behind it.
#
# rclone's own documentation labels `serve s3` "Experimental", and this is
# genuinely a different category of thing than the rustfs backend: that one
# runs a native S3-compatible storage *engine*; this one runs a *translation
# layer* in front of a backend that was never designed to speak S3. When
# multipart upload parts arrive out of ascending order (a pattern the S3 API
# explicitly permits) past OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT worth of
# buffered-but-not-yet-written data, rclone's own reorder buffer blocks the
# request that doesn't fit, waiting for the part(s) ahead of it in sequence -
# a genuine request-level deadlock if a client happens to upload entirely in
# descending order one part at a time (sequential, concurrency=1), since the
# part the server is waiting for will never arrive until the one it's
# currently blocking on returns. See EVALUATION.md Finding 1 for the full
# writeup, including why raising this buffer (done here, default 2GiB -
# comfortably past what a normal, ascending/boto3-managed multipart upload
# ever buffers, confirmed by re-running the deliberately-adversarial fully-
# reversed test matrix at every documented concurrency level, 300-350 MiB,
# with zero hangs) narrows this risk rather than eliminating it: an upload
# larger than this buffer, delivered in sufficiently reversed order, can
# still hit the same wait. This is why OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK
# exists and defaults to false - see plugin.py's validate_objstorage_config.
#
# Defaults point at Azurite, Microsoft's free local Azure Storage emulator,
# so `tutor local launch` works with no real Azure account. Override
# OBJSTORAGE_AZURE_ACCOUNT / _ACCOUNT_KEY / _ENDPOINT together to point at a
# real Azure Storage account instead; see the README.
# -----------------------------------------------------------------------------

BACKEND = Backend(
    name="azure",
    defaults={
        "HOST": "files.{{ LMS_HOST }}",
        "REGION": "us-east-1",
        # https://hub.docker.com/r/rclone/rclone/tags
        # Hard-pinned to an exact tag, never a floating range: this
        # backend's multipart-upload testing (see EVALUATION.md) is only
        # valid for the exact version tested. 1.75.1 fixes CVE-2026-88045,
        # an unauthenticated memory-exhaustion DoS in the multipart upload
        # path - never go below it. Bump deliberately, and re-run the live
        # conformance suite against the new tag before adopting it.
        "DOCKER_IMAGE": "docker.io/rclone/rclone:1.75.1",
        # The image creates a non-root `rclone` user at this UID/GID but
        # does not switch to it by default; we do so explicitly. Unlike
        # RustFS there is no bind-mounted data directory, so there is no
        # chown-before-first-start step tied to this value.
        "UID": 1009,
        "GID": 1009,
        # Azure Blob Storage backend. These three defaults point at the
        # bundled Azurite emulator for local testing with no real Azure
        # account. Azurite's account name and key are fixed, publicly
        # documented constants - not secrets - the same for every Azurite
        # install everywhere. Override all three together for a real Azure
        # Storage account; see the README before doing so.
        "ACCOUNT": "devstoreaccount1",
        "ACCOUNT_KEY": "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==",  # noqa: E501
        "ENDPOINT": "http://azurite:10000/devstoreaccount1",
        # https://mcr.microsoft.com/en-us/artifact/mar/azure-storage/azurite/tags
        "AZURITE_DOCKER_IMAGE": "mcr.microsoft.com/azure-storage/azurite:3.37.0",
        # The explicit second opt-in required before this backend's
        # infrastructure will render at all. See plugin.py.
        "ACKNOWLEDGE_RISK": False,
        # rclone's own `--multipart-streaming-buffer-limit` (default in
        # rclone itself: 256MiB; "0" means unlimited). Memory rclonegw will
        # buffer, per in-flight multipart upload, for parts that arrive
        # before the part the sink is actually waiting for - see the module
        # docstring above and EVALUATION.md Finding 1. Raised here past
        # rclone's own default because a single out-of-order video upload
        # can legitimately exceed 256MiB; this is still a bound, not
        # "unlimited" - an operator whose course videos routinely exceed
        # this should raise it further, trading more worst-case memory per
        # concurrent multipart upload for a larger safety margin.
        "MULTIPART_BUFFER_LIMIT": "2G",
    },
    # No per-backend AWS_SECRET_ACCESS_KEY here: see rustfs.py's comment -
    # all three backends share one generated secret (config.SHARED_UNIQUE).
    requires_ack_key="OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK",
)
