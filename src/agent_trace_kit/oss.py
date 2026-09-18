"""S3-compatible object upload for JD Cloud OSS (SigV4, stdlib only).

Secrets are loaded from environment variables or an external ``secrets.env``
file — never from inside the repository.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class OssConfig:
    endpoint: str  # https://s3.<region>.jdcloud-oss.com
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = "cn-north-1"
    public_base: str = ""  # optional CDN/custom domain; defaults to path-style
    key_prefix: str = "pairwise"

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.endpoint).netloc

    def public_url(self, key: str) -> str:
        encoded = "/".join(urllib.parse.quote(seg, safe="") for seg in key.split("/"))
        if self.public_base:
            return self.public_base.rstrip("/") + "/" + encoded
        return f"{self.endpoint.rstrip('/')}/{self.bucket}/{encoded}"


def load_secrets_file(path: str | Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file (ignores comments/blank lines)."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str = "s3") -> bytes:
    k = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")


def _request(
    cfg: OssConfig,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
):
    """Perform a SigV4-signed request; returns (status, headers, body_bytes)."""
    parsed = urllib.parse.urlparse(url)
    now = _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body or b"").hexdigest()

    query_items = sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in query_items
    )
    headers = {**(headers or {})}
    headers["Host"] = parsed.netloc
    headers["X-Amz-Date"] = amz_date
    headers["X-Amz-Content-Sha256"] = payload_hash

    signed_names = sorted(h.lower() for h in headers)
    canonical_headers = "".join(
        f"{name}:{headers[next(k for k in headers if k.lower() == name)].strip()}\n"
        for name in signed_names
    )
    canonical_uri = urllib.parse.quote(parsed.path or "/", safe="/-_.~")
    canonical_request = "\n".join([
        method,
        canonical_uri,
        canonical_query,
        canonical_headers,
        ";".join(signed_names),
        payload_hash,
    ])
    scope = f"{date_stamp}/{cfg.region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(
        _signing_key(cfg.secret_access_key, date_stamp, cfg.region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={cfg.access_key_id}/{scope}, "
        f"SignedHeaders={';'.join(signed_names)}, Signature={signature}"
    )

    req = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def list_buckets(cfg: OssConfig) -> list[str]:
    url = f"{cfg.endpoint.rstrip('/')}/"
    status, _, body = _request(cfg, "GET", url)
    if status != 200:
        raise RuntimeError(f"list_buckets failed ({status}): {body[:300]!r}")
    text = body.decode("utf-8", "replace")
    import re
    return re.findall(r"<Name>([^<]+)</Name>", text)


def ensure_bucket(cfg: OssConfig) -> dict:
    """Create the bucket if missing. Returns {existed, name}."""
    if cfg.bucket in list_buckets(cfg):
        return {"existed": True, "name": cfg.bucket}
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<CreateBucketConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<LocationConstraint>{cfg.region}</LocationConstraint>"
        "</CreateBucketConfiguration>"
    ).encode("utf-8")
    url = f"{cfg.endpoint.rstrip('/')}/{cfg.bucket}"
    status, _, resp = _request(cfg, "PUT", url, body=body)
    if status not in (200, 201):
        raise RuntimeError(f"create_bucket failed ({status}): {resp[:300]!r}")
    return {"existed": False, "name": cfg.bucket}


def upload_file(cfg: OssConfig, local_path: str | Path, key: str, *, content_type: str = "") -> dict:
    """Upload one file with public-read ACL; returns key/url/size/sha256."""
    p = Path(local_path)
    data = p.read_bytes()
    headers = {
        "Content-Type": content_type or _guess_type(p.name),
        "Content-Length": str(len(data)),
        "x-amz-acl": "public-read",
    }
    url = f"{cfg.endpoint.rstrip('/')}/{cfg.bucket}/{urllib.parse.quote(key, safe='/')}"
    status, resp_headers, body = _request(cfg, "PUT", url, headers=headers, body=data, timeout=120)
    if status not in (200, 201):
        raise RuntimeError(f"upload {key} failed ({status}): {body[:300]!r}")
    return {
        "key": key,
        "url": cfg.public_url(key),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "etag": resp_headers.get("ETag", "").strip('"'),
    }


def anonymous_head(url: str, timeout: float = 15.0) -> dict:
    """Unauthenticated HEAD used to prove a public link is actually reachable."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": resp.status, "content_length": resp.headers.get("Content-Length", "")}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "error": str(exc)}
    except OSError as exc:
        return {"status": 0, "error": str(exc)}


def _guess_type(name: str) -> str:
    if name.endswith(".jsonl"):
        return "application/x-ndjson"
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".mp4"):
        return "video/mp4"
    if name.endswith(".csv"):
        return "text/csv"
    return "application/octet-stream"


def config_from_mapping(data: dict[str, str], secrets_path: str | Path = "") -> OssConfig | None:
    """Build OssConfig from desk settings + environment/secrets file.

    Returns None when credentials or bucket are not configured.
    """
    env: dict[str, str] = {}
    if secrets_path:
        env.update(load_secrets_file(secrets_path))
    env.update({k: v for k, v in os.environ.items() if v})
    ak = env.get("OSS_ACCESS_KEY_ID") or env.get("AWS_ACCESS_KEY_ID") or ""
    sk = env.get("OSS_SECRET_ACCESS_KEY") or env.get("AWS_SECRET_ACCESS_KEY") or ""
    endpoint = (data.get("oss_endpoint") or "").strip()
    bucket = (data.get("oss_bucket") or "").strip()
    if not (endpoint and bucket and ak and sk):
        return None
    return OssConfig(
        endpoint=endpoint.rstrip("/"),
        bucket=bucket,
        access_key_id=ak,
        secret_access_key=sk,
        region=(data.get("oss_region") or "cn-north-1").strip(),
        public_base=(data.get("oss_public_base") or "").strip(),
        key_prefix=(data.get("oss_key_prefix") or "pairwise").strip().strip("/"),
    )


def describe_config(cfg: OssConfig | None) -> dict:
    if cfg is None:
        return {"configured": False}
    return {
        "configured": True,
        "endpoint": cfg.endpoint,
        "region": cfg.region,
        "bucket": cfg.bucket,
        "public_base": cfg.public_base,
        "key_prefix": cfg.key_prefix,
        "access_key_id_preview": (cfg.access_key_id[:6] + "…") if cfg.access_key_id else "",
    }


def connection_test(cfg: OssConfig) -> dict:
    try:
        buckets = list_buckets(cfg)
        return {"ok": True, "buckets": buckets, "bucket_present": cfg.bucket in buckets, **describe_config(cfg)}
    except Exception as exc:  # surfaced verbatim in the settings UI
        return {"ok": False, "error": str(exc), **describe_config(cfg)}
