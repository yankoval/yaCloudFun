from __future__ import annotations

import hashlib
import io
import json
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from botocore.exceptions import ClientError

from sscc_generator import index


BUCKET = "test-bucket"
PREFIX = "460705179"


def client_error(code: str, operation: str) -> ClientError:
    status = 412 if code == "PreconditionFailed" else 404
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}
        self.lock = threading.Lock()
        self.version = 0

    def seed(self, key: str, value: dict) -> None:
        with self.lock:
            self.version += 1
            self.objects[(BUCKET, key)] = {
                "body": json.dumps(value).encode("utf-8"),
                "etag": f'"etag-{self.version}"',
            }

    def get_object(self, *, Bucket: str, Key: str):
        with self.lock:
            item = self.objects.get((Bucket, Key))
            if item is None:
                raise client_error("NoSuchKey", "GetObject")
            return {
                "Body": io.BytesIO(bytes(item["body"])),
                "ETag": item["etag"],
            }

    def put_object(self, **kwargs):
        bucket = kwargs["Bucket"]
        key = kwargs["Key"]
        body = bytes(kwargs["Body"])
        with self.lock:
            current = self.objects.get((bucket, key))
            if kwargs.get("IfNoneMatch") == "*" and current is not None:
                raise client_error("PreconditionFailed", "PutObject")
            if "IfMatch" in kwargs:
                if current is None or current["etag"] != kwargs["IfMatch"]:
                    raise client_error("PreconditionFailed", "PutObject")
            self.version += 1
            etag = f'"etag-{self.version}"'
            self.objects[(bucket, key)] = {"body": body, "etag": etag}
            return {"ETag": etag}

    def json(self, key: str) -> dict:
        with self.lock:
            return json.loads(bytes(self.objects[(BUCKET, key)]["body"]).decode("utf-8"))


class AllocatorTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.s3.seed(
            f"test-counters/{PREFIX}.json",
            {"default_extension": "0", "counters": {"0": 100}},
        )
        self.environment = patch.dict(
            "os.environ",
            {
                "SSCC_BUCKET_NAME": BUCKET,
                "SSCC_STORAGE_FOLDER": "test-counters",
                "SSCC_IDEMPOTENCY_FOLDER": "test-idempotency",
                "AWS_ACCESS_KEY_ID": "test-access",
                "AWS_SECRET_ACCESS_KEY": "test-secret",
            },
            clear=False,
        )
        self.environment.start()
        self.client_patch = patch.object(index, "get_s3_client", return_value=self.s3)
        self.client_patch.start()

    def tearDown(self):
        self.client_patch.stop()
        self.environment.stop()

    @staticmethod
    def request(*, job_uuid: str, source_hash: str | None = None, count: int = 2):
        return {
            "body": json.dumps(
                {
                    "prefix": PREFIX,
                    "extension": "0",
                    "count": count,
                    "idempotency_key": job_uuid,
                    "source_hash": source_hash or hashlib.sha256(b"source").hexdigest(),
                }
            )
        }

    @staticmethod
    def body(response: dict) -> dict:
        return json.loads(response["body"])

    def test_duplicate_returns_same_allocation_without_advancing_counter(self):
        job_uuid = str(uuid.uuid4())
        first = index.handler(self.request(job_uuid=job_uuid), None)
        second = index.handler(self.request(job_uuid=job_uuid), None)

        self.assertEqual(200, first["statusCode"])
        self.assertEqual(200, second["statusCode"])
        self.assertEqual(self.body(first)["ssccs"], self.body(second)["ssccs"])
        self.assertFalse(self.body(first)["duplicate"])
        self.assertTrue(self.body(second)["duplicate"])
        counter = self.s3.json(f"test-counters/{PREFIX}.json")
        self.assertEqual(102, counter["counters"]["0"])
        self.assertEqual({}, counter["pending_allocations"])

    def test_same_key_with_different_source_hash_is_conflict(self):
        job_uuid = str(uuid.uuid4())
        first = index.handler(self.request(job_uuid=job_uuid), None)
        conflict = index.handler(
            self.request(job_uuid=job_uuid, source_hash=hashlib.sha256(b"other").hexdigest()),
            None,
        )

        self.assertEqual(200, first["statusCode"])
        self.assertEqual(409, conflict["statusCode"])
        self.assertEqual(
            102,
            self.s3.json(f"test-counters/{PREFIX}.json")["counters"]["0"],
        )

    def test_parallel_duplicates_converge_on_one_allocation(self):
        job_uuid = str(uuid.uuid4())
        with patch.object(index.time, "sleep", return_value=None):
            with ThreadPoolExecutor(max_workers=8) as executor:
                responses = list(
                    executor.map(
                        lambda _: index.handler(self.request(job_uuid=job_uuid, count=5), None),
                        range(20),
                    )
                )

        self.assertTrue(all(response["statusCode"] == 200 for response in responses))
        allocations = {tuple(self.body(response)["ssccs"]) for response in responses}
        self.assertEqual(1, len(allocations))
        self.assertEqual(
            105,
            self.s3.json(f"test-counters/{PREFIX}.json")["counters"]["0"],
        )

    def test_parallel_distinct_requests_never_overlap(self):
        job_uuids = [str(uuid.uuid4()) for _ in range(20)]
        with patch.object(index.time, "sleep", return_value=None):
            with ThreadPoolExecutor(max_workers=8) as executor:
                responses = list(
                    executor.map(
                        lambda job_uuid: index.handler(
                            self.request(job_uuid=job_uuid, count=3), None
                        ),
                        job_uuids,
                    )
                )

        self.assertTrue(all(response["statusCode"] == 200 for response in responses))
        codes = [code for response in responses for code in self.body(response)["ssccs"]]
        self.assertEqual(60, len(codes))
        self.assertEqual(60, len(set(codes)))
        self.assertEqual(
            160,
            self.s3.json(f"test-counters/{PREFIX}.json")["counters"]["0"],
        )

    def test_counter_contention_can_outlive_legacy_five_retry_limit(self):
        job_uuid = str(uuid.uuid4())
        original_put_object = self.s3.put_object
        conflicts_remaining = 8

        def contended_put_object(**kwargs):
            nonlocal conflicts_remaining
            if (
                kwargs["Key"] == f"test-counters/{PREFIX}.json"
                and "IfMatch" in kwargs
                and conflicts_remaining > 0
            ):
                conflicts_remaining -= 1
                raise client_error("PreconditionFailed", "PutObject")
            return original_put_object(**kwargs)

        with patch.object(self.s3, "put_object", side_effect=contended_put_object):
            with patch.object(index.time, "sleep", return_value=None):
                response = index.handler(self.request(job_uuid=job_uuid, count=3), None)

        self.assertEqual(200, response["statusCode"])
        self.assertEqual(0, conflicts_remaining)
        self.assertEqual(
            103,
            self.s3.json(f"test-counters/{PREFIX}.json")["counters"]["0"],
        )

    def test_pending_claim_before_counter_update_is_recoverable(self):
        job_uuid = str(uuid.uuid4())
        source_hash = hashlib.sha256(b"source").hexdigest()
        self.s3.seed(
            f"test-idempotency/{job_uuid}.json",
            {
                "schema_version": 1,
                "status": "PENDING",
                "idempotency_key": job_uuid,
                "source_hash": source_hash,
                "prefix": PREFIX,
                "extension": "0",
                "count": 2,
            },
        )

        response = index.handler(self.request(job_uuid=job_uuid), None)

        self.assertEqual(200, response["statusCode"])
        self.assertFalse(self.body(response)["duplicate"])
        self.assertEqual(
            102,
            self.s3.json(f"test-counters/{PREFIX}.json")["counters"]["0"],
        )

    def test_pending_claim_recovers_after_crash_window(self):
        job_uuid = str(uuid.uuid4())
        source_hash = hashlib.sha256(b"source").hexdigest()
        allocation = {
            "schema_version": 1,
            "idempotency_key": job_uuid,
            "source_hash": source_hash,
            "prefix": PREFIX,
            "extension": "0",
            "count": 2,
            "start_serial": 100,
            "serial_length": 7,
            "allocation_id": index._allocation_id(PREFIX, "0", 100, 2),
        }
        self.s3.seed(
            f"test-idempotency/{job_uuid}.json",
            {
                "schema_version": 1,
                "status": "PENDING",
                "idempotency_key": job_uuid,
                "source_hash": source_hash,
                "prefix": PREFIX,
                "extension": "0",
                "count": 2,
            },
        )
        self.s3.seed(
            f"test-counters/{PREFIX}.json",
            {
                "default_extension": "0",
                "counters": {"0": 102},
                "pending_allocations": {job_uuid: allocation},
            },
        )

        response = index.handler(self.request(job_uuid=job_uuid), None)

        self.assertEqual(200, response["statusCode"])
        self.assertTrue(self.body(response)["duplicate"])
        self.assertEqual(
            "COMPLETED",
            self.s3.json(f"test-idempotency/{job_uuid}.json")["status"],
        )
        self.assertEqual(
            {},
            self.s3.json(f"test-counters/{PREFIX}.json")["pending_allocations"],
        )

    def test_legacy_request_remains_backward_compatible(self):
        event = {"body": json.dumps({"prefix": PREFIX, "extension": "0", "count": 1})}
        first = index.handler(event, None)
        second = index.handler(event, None)

        self.assertEqual(200, first["statusCode"])
        self.assertEqual(200, second["statusCode"])
        self.assertNotEqual(self.body(first)["ssccs"], self.body(second)["ssccs"])
        self.assertEqual(
            102,
            self.s3.json(f"test-counters/{PREFIX}.json")["counters"]["0"],
        )

    def test_invalid_count_does_not_advance_counter(self):
        job_uuid = str(uuid.uuid4())
        response = index.handler(self.request(job_uuid=job_uuid, count=0), None)

        self.assertEqual(400, response["statusCode"])
        self.assertEqual(
            100,
            self.s3.json(f"test-counters/{PREFIX}.json")["counters"]["0"],
        )


if __name__ == "__main__":
    unittest.main()
