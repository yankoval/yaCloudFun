"""Yandex Cloud Function for atomic and idempotent SSCC allocation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import boto3
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_BUCKET_NAME = "20ab2a0c-2726-4ba1-9c7c-7deae82941ff"
DEFAULT_STORAGE_FOLDER = "sscc"
DEFAULT_IDEMPOTENCY_FOLDER = "sscc-idempotency"
MAX_RETRIES = 5
MAX_COUNTER_RETRIES = 20
INITIAL_BACKOFF = 0.02
MAX_BACKOFF = 0.5
SOURCE_HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class RequestError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class IdempotencyConflict(RequestError):
    def __init__(self, message: str = "Idempotency key belongs to another request") -> None:
        super().__init__(message, status_code=409)


@dataclass(frozen=True)
class AllocationRequest:
    prefix: str
    extension: str
    count: int
    idempotency_key: str | None
    source_hash: str | None


def _bucket_name() -> str:
    return os.environ.get("SSCC_BUCKET_NAME", DEFAULT_BUCKET_NAME)


def _storage_folder() -> str:
    return os.environ.get("SSCC_STORAGE_FOLDER", DEFAULT_STORAGE_FOLDER).strip("/")


def _idempotency_folder() -> str:
    return os.environ.get(
        "SSCC_IDEMPOTENCY_FOLDER", DEFAULT_IDEMPOTENCY_FOLDER
    ).strip("/")


def get_s3_client() -> Any:
    """Initialize an S3 client using the current static-key deployment contract."""

    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        raise RequestError("Missing S3 credentials", status_code=401)

    return boto3.client(
        service_name="s3",
        endpoint_url="https://storage.yandexcloud.net",
        region_name="ru-central1",
        aws_access_key_id=access_key.strip(),
        aws_secret_access_key=secret_key.strip(),
    )


def calculate_check_digit(number_str: str) -> str:
    """Calculate a GS1 Modulo 10 check digit for a 17-digit base."""

    total = 0
    for index in range(len(number_str)):
        digit = int(number_str[-(index + 1)])
        total += digit * (3 if (index + 2) % 2 == 0 else 1)
    return str((10 - (total % 10)) % 10)


def _response(status_code: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "body": json.dumps(body, ensure_ascii=False, sort_keys=True),
    }


def _parse_event(event: Mapping[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    raw_body = event.get("body")
    if isinstance(raw_body, str):
        try:
            decoded = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RequestError("Request body is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise RequestError("Request body must contain an object")
        body = decoded
    elif isinstance(raw_body, Mapping):
        body = dict(raw_body)
    elif raw_body is not None:
        raise RequestError("Request body must contain an object")

    params = event.get("queryStringParameters") or {}
    if not isinstance(params, Mapping):
        raise RequestError("Query parameters must contain an object")
    return {**body, **params}


def _parse_count(raw_count: Any) -> int:
    if isinstance(raw_count, bool):
        raise RequestError("Count must be a positive integer")
    try:
        count = int(raw_count)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RequestError("Count must be a positive integer") from exc
    if count <= 0:
        raise RequestError("Count must be a positive integer")
    return count


def _parse_idempotency(input_data: Mapping[str, Any]) -> tuple[str | None, str | None]:
    raw_key = input_data.get("idempotency_key")
    raw_hash = input_data.get("source_hash")
    if raw_key in (None, "") and raw_hash in (None, ""):
        return None, None
    if raw_key in (None, "") or raw_hash in (None, ""):
        raise RequestError("idempotency_key and source_hash must be supplied together")

    try:
        idempotency_key = str(uuid.UUID(str(raw_key)))
    except (ValueError, AttributeError) as exc:
        raise RequestError("idempotency_key must contain a UUID") from exc

    source_hash = str(raw_hash).lower()
    if not SOURCE_HASH_PATTERN.fullmatch(source_hash):
        raise RequestError("source_hash must contain a SHA-256 hex digest")
    return idempotency_key, source_hash


def _serial_length(prefix: str) -> int:
    length = 17 - 1 - len(prefix)
    if length < 1:
        raise RequestError("Prefix is too long")
    return length


def _normalize_config(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    if "counters" not in raw_config:
        config: dict[str, Any] = {
            "default_extension": "0",
            "counters": {"0": raw_config.get("next_serial", 0)},
        }
    else:
        config = dict(raw_config)

    counters = config.get("counters")
    if not isinstance(counters, Mapping):
        raise RequestError("Counter configuration is invalid", status_code=500)
    config["counters"] = dict(counters)

    pending = config.get("pending_allocations", {})
    if not isinstance(pending, Mapping):
        raise RequestError("Pending allocation configuration is invalid", status_code=500)
    config["pending_allocations"] = dict(pending)
    return config


def _parse_request(input_data: Mapping[str, Any], config: Mapping[str, Any]) -> AllocationRequest:
    prefix = str(input_data.get("prefix", ""))
    if not prefix or not prefix.isdigit():
        raise RequestError("Prefix must contain digits")
    _serial_length(prefix)

    count = _parse_count(input_data.get("count", 1))
    extension = str(input_data.get("extension", config.get("default_extension", "0")))
    if len(extension) != 1 or not extension.isdigit():
        raise RequestError("Extension must be a single digit")

    idempotency_key, source_hash = _parse_idempotency(input_data)
    return AllocationRequest(
        prefix=prefix,
        extension=extension,
        count=count,
        idempotency_key=idempotency_key,
        source_hash=source_hash,
    )


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _error_code(exc: ClientError) -> str:
    return str((exc.response or {}).get("Error", {}).get("Code", ""))


def _is_missing(exc: ClientError) -> bool:
    return _error_code(exc) in {"NoSuchKey", "NotFound", "404"}


def _is_precondition_failed(exc: ClientError) -> bool:
    return _error_code(exc) in {"PreconditionFailed", "412"}


def _read_json(s3: Any, key: str, *, optional: bool = False) -> tuple[dict[str, Any], str] | None:
    try:
        response = s3.get_object(Bucket=_bucket_name(), Key=key)
    except ClientError as exc:
        if optional and _is_missing(exc):
            return None
        raise

    try:
        value = json.loads(response["Body"].read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestError(f"S3 object {key} is not valid JSON", status_code=500) from exc
    if not isinstance(value, dict):
        raise RequestError(f"S3 object {key} must contain an object", status_code=500)
    return value, str(response["ETag"])


def _counter_key(prefix: str) -> str:
    return f"{_storage_folder()}/{prefix}.json"


def _claim_key(idempotency_key: str) -> str:
    return f"{_idempotency_folder()}/{idempotency_key}.json"


def _request_fields(request: AllocationRequest) -> dict[str, Any]:
    return {
        "idempotency_key": request.idempotency_key,
        "source_hash": request.source_hash,
        "prefix": request.prefix,
        "extension": request.extension,
        "count": request.count,
    }


def _assert_same_request(record: Mapping[str, Any], request: AllocationRequest) -> None:
    for key, expected in _request_fields(request).items():
        if record.get(key) != expected:
            raise IdempotencyConflict()


def _allocation_id(prefix: str, extension: str, start_serial: int, count: int) -> str:
    canonical = f"{prefix}|{extension}|{start_serial}|{count}".encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _allocation_record(
    request: AllocationRequest,
    *,
    start_serial: int,
    serial_length: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        **_request_fields(request),
        "start_serial": start_serial,
        "serial_length": serial_length,
        "allocation_id": _allocation_id(
            request.prefix,
            request.extension,
            start_serial,
            request.count,
        ),
    }


def _validate_allocation_record(record: Mapping[str, Any]) -> None:
    try:
        start_serial = int(record["start_serial"])
        serial_length = int(record["serial_length"])
        count = int(record["count"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise RequestError("Stored allocation record is invalid", status_code=500) from exc
    if start_serial < 0 or serial_length < 1 or count <= 0:
        raise RequestError("Stored allocation record is invalid", status_code=500)


def _generate_ssccs(record: Mapping[str, Any]) -> list[str]:
    _validate_allocation_record(record)
    prefix = str(record["prefix"])
    extension = str(record["extension"])
    start_serial = int(record["start_serial"])
    serial_length = int(record["serial_length"])
    count = int(record["count"])

    result: list[str] = []
    for offset in range(count):
        serial = str(start_serial + offset).zfill(serial_length)
        base_number = extension + prefix + serial
        result.append(base_number + calculate_check_digit(base_number))
    return result


def _ensure_claim(s3: Any, request: AllocationRequest) -> tuple[dict[str, Any], str]:
    if request.idempotency_key is None:
        raise RuntimeError("Cannot create an idempotency claim without a key")

    key = _claim_key(request.idempotency_key)
    pending_claim = {
        "schema_version": 1,
        "status": "PENDING",
        **_request_fields(request),
    }
    try:
        response = s3.put_object(
            Bucket=_bucket_name(),
            Key=key,
            Body=_json_bytes(pending_claim),
            ContentType="application/json",
            IfNoneMatch="*",
        )
        return pending_claim, str(response["ETag"])
    except ClientError as exc:
        if not _is_precondition_failed(exc):
            raise

    existing = _read_json(s3, key)
    if existing is None:
        raise RuntimeError("Idempotency claim disappeared after create conflict")
    record, etag = existing
    _assert_same_request(record, request)
    if record.get("status") not in {"PENDING", "COMPLETED"}:
        raise RequestError("Stored idempotency status is invalid", status_code=500)
    if record.get("status") == "COMPLETED":
        _validate_allocation_record(record)
    return record, etag


def _complete_claim(
    s3: Any,
    request: AllocationRequest,
    allocation: Mapping[str, Any],
    claim_etag: str,
) -> dict[str, Any]:
    if request.idempotency_key is None:
        raise RuntimeError("Cannot complete an idempotency claim without a key")

    completed = {**dict(allocation), "status": "COMPLETED"}
    key = _claim_key(request.idempotency_key)
    current_etag = claim_etag
    for _ in range(MAX_RETRIES):
        try:
            s3.put_object(
                Bucket=_bucket_name(),
                Key=key,
                Body=_json_bytes(completed),
                ContentType="application/json",
                IfMatch=current_etag,
            )
            return completed
        except ClientError as exc:
            if not _is_precondition_failed(exc):
                raise

        existing = _read_json(s3, key)
        if existing is None:
            raise RuntimeError("Idempotency claim disappeared during completion")
        record, current_etag = existing
        _assert_same_request(record, request)
        if record.get("status") == "COMPLETED":
            _validate_allocation_record(record)
            if record.get("allocation_id") != allocation.get("allocation_id"):
                raise IdempotencyConflict("Idempotency key has another allocation")
            return record

    raise RequestError("Could not complete idempotency record", status_code=409)


def _cleanup_pending(s3: Any, request: AllocationRequest, allocation_id: str) -> None:
    if request.idempotency_key is None:
        return

    key = _counter_key(request.prefix)
    for _ in range(MAX_RETRIES):
        try:
            current = _read_json(s3, key)
            if current is None:
                return
            raw_config, etag = current
            config = _normalize_config(raw_config)
            pending = config["pending_allocations"]
            allocation = pending.get(request.idempotency_key)
            if not isinstance(allocation, Mapping):
                return
            if allocation.get("allocation_id") != allocation_id:
                logger.warning("Pending allocation differs from completed idempotency record")
                return
            del pending[request.idempotency_key]
            s3.put_object(
                Bucket=_bucket_name(),
                Key=key,
                Body=_json_bytes(config),
                ContentType="application/json",
                IfMatch=etag,
            )
            return
        except ClientError as exc:
            if not _is_precondition_failed(exc):
                logger.warning("Could not clean pending allocation after S3 error")
                return
    logger.warning("Could not clean pending allocation after concurrent updates")


def _success(record: Mapping[str, Any], *, duplicate: bool) -> dict[str, Any]:
    return _response(
        200,
        {
            "ssccs": _generate_ssccs(record),
            "allocation_id": record.get("allocation_id"),
            "duplicate": duplicate,
        },
    )


def _allocate(s3: Any, input_data: Mapping[str, Any]) -> dict[str, Any]:
    prefix = str(input_data.get("prefix", ""))
    if not prefix or not prefix.isdigit():
        raise RequestError("Prefix must contain digits")
    counter_key = _counter_key(prefix)

    for attempt in range(MAX_COUNTER_RETRIES):
        current = _read_json(s3, counter_key)
        if current is None:
            raise RuntimeError("Counter object disappeared")
        raw_config, counter_etag = current
        config = _normalize_config(raw_config)
        request = _parse_request(input_data, config)
        serial_length = _serial_length(request.prefix)

        claim_etag: str | None = None
        if request.idempotency_key is not None:
            claim, claim_etag = _ensure_claim(s3, request)
            if claim.get("status") == "COMPLETED":
                _cleanup_pending(s3, request, str(claim["allocation_id"]))
                return _success(claim, duplicate=True)

            pending = config["pending_allocations"].get(request.idempotency_key)
            if isinstance(pending, Mapping):
                _assert_same_request(pending, request)
                _validate_allocation_record(pending)
                completed = _complete_claim(s3, request, pending, str(claim_etag))
                _cleanup_pending(s3, request, str(completed["allocation_id"]))
                return _success(completed, duplicate=True)

        counters = config["counters"]
        try:
            current_serial = int(counters.get(request.extension, 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise RequestError("Stored counter value is invalid", status_code=500) from exc
        if current_serial < 0:
            raise RequestError("Stored counter value is invalid", status_code=500)

        max_serial_exclusive = 10**serial_length
        if current_serial + request.count > max_serial_exclusive:
            raise RequestError(
                f"Serial number overflow for extension {request.extension}",
                status_code=400,
            )

        allocation = _allocation_record(
            request,
            start_serial=current_serial,
            serial_length=serial_length,
        )
        counters[request.extension] = current_serial + request.count
        if request.idempotency_key is not None:
            config["pending_allocations"][request.idempotency_key] = allocation

        try:
            s3.put_object(
                Bucket=_bucket_name(),
                Key=counter_key,
                Body=_json_bytes(config),
                ContentType="application/json",
                IfMatch=counter_etag,
            )
        except ClientError as exc:
            if _is_precondition_failed(exc) and attempt < MAX_COUNTER_RETRIES - 1:
                backoff_ceiling = min(MAX_BACKOFF, INITIAL_BACKOFF * (2**attempt))
                sleep_time = random.uniform(0, backoff_ceiling)
                logger.info("Concurrent counter update; retrying allocation")
                time.sleep(sleep_time)
                continue
            raise

        if request.idempotency_key is None:
            return _success(allocation, duplicate=False)

        completed = _complete_claim(s3, request, allocation, str(claim_etag))
        _cleanup_pending(s3, request, str(completed["allocation_id"]))
        return _success(completed, duplicate=False)

    raise RequestError("Conflict: too many concurrent requests", status_code=409)


def handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    del context
    try:
        input_data = _parse_event(event)
        s3 = get_s3_client()
        return _allocate(s3, input_data)
    except RequestError as exc:
        return _response(exc.status_code, {"error": str(exc)})
    except ClientError as exc:
        if _is_missing(exc):
            return _response(404, {"error": "Counter configuration was not found"})
        if _is_precondition_failed(exc):
            return _response(409, {"error": "Conflict: concurrent update"})
        logger.exception("SSCC allocator S3 failure")
        return _response(500, {"error": "S3 operation failed"})
    except Exception:
        logger.exception("SSCC allocator failed")
        return _response(500, {"error": "Internal allocator error"})
