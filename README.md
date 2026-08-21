# SSCC Generator Yandex Cloud Function

This project provides a Yandex Cloud Function to generate Serial Shipping Container Codes (SSCC) according to the GS1 Modulo 10 algorithm. It maintains persistent serial counters using Yandex Object Storage.

## Features
- **SSCC-18 Generation**: Generates valid 18-digit SSCC codes.
- **Multi-Extension Support**: Separate counters for each extension digit (0-9).
- **Default Extension**: Configurable default extension per prefix.
- **Overflow Protection**: Automatically calculates available serial range based on prefix length and prevents counter overflow.
- **Persistence**: Securely stores state in Yandex Object Storage.
- **Idempotent allocations**: `idempotency_key + source_hash` retries return
  the original range and never advance the counter twice.
- **Crash recovery**: a conditional claim and a short-lived pending allocation
  in the counter object close the failure window between reservation and result.
- **CI/CD**: Fully automated deployment via GitHub Actions with OIDC federation.
- **Flexible Input**: Supports both JSON body and Query String parameters.

## Directory Structure
- `sscc_generator/`: Python source code and requirements.
- `.github/workflows/`: CI/CD workflows for automated deployment.

## Prerequisites
1. **Yandex Object Storage Bucket**: `20ab2a0c-2726-4ba1-9c7c-7deae82941ff`.
2. **Counter Configuration**: Create a JSON file in the bucket at `sscc/{prefix}.json` (e.g., `sscc/460705179.json`).
   
   **Modern Structure (Recommended):**
   ```json
   {
     "default_extension": "0",
     "counters": {
       "0": 0,
       "1": 0
     }
   }
   ```
   *Note: The function is backward compatible and will automatically migrate old `{"next_serial": 0}` files.*

3. **GitHub Secrets**:
   - `YC_FOLDER_ID`: Your Yandex Cloud Folder ID.
   - `YC_SA_ID`: Service Account ID with `functions.admin` role.
   - `AWS_ACCESS_KEY_ID`: Static access key for S3.
   - `AWS_SECRET_ACCESS_KEY`: Static secret key for S3.

## Usage

### Function Input
The function accepts the following parameters:
- `prefix`: (Required) Your GS1 Company Prefix.
- `extension`: (Optional) SSCC extension digit (0-9). If omitted, the `default_extension` from config is used.
- `count`: (Optional, default 1) Number of codes to generate.
- `idempotency_key`: (Optional for legacy callers) UUID of the source job.
- `source_hash`: Required together with `idempotency_key`; SHA-256 of the
  canonical source job.

New callers must always send both idempotency fields. Legacy callers without
them remain backward compatible and receive a new range on every request.

### Sample Request
**POST** to function URL with JSON body:
```json
{
  "prefix": "460705179",
  "extension": "1",
  "count": 5,
  "idempotency_key": "5f40cb54-16ca-4f52-90ac-3c70a130aa0a",
  "source_hash": "64 hexadecimal SHA-256 characters"
}
```

The response also contains a deterministic `allocation_id` and a `duplicate`
flag. Reusing the UUID with a different hash, prefix, extension or count returns
HTTP 409.

## Storage contract

- `SSCC_BUCKET_NAME` defaults to the current SSCC bucket.
- `SSCC_STORAGE_FOLDER` defaults to `sscc` and contains the shared atomic
  counters used by both old and new function deployments.
- `SSCC_IDEMPOTENCY_FOLDER` defaults to `sscc-idempotency` and contains one
  exact-key record per source UUID; no LIST operation is used.

The CT workflow uses isolated `_prnsrv-test/sscc-*` prefixes. Feature pushes run
unit tests only and no longer overwrite `sscc-generator-ci`, because that URL is
the single production allocator used by both legacy and new clients. Main
deploys the backward-compatible upgrade to `sscc-generator-ci` after tests pass;
the separate `sscc-generator` function is not part of this migration.

### Overflow Errors
If the requested `count` exceeds the available serial range (e.g., a 12-digit prefix only leaves 4 digits for the serial), the function returns a `400 Bad Request` error.

## Troubleshooting Credentials

If you receive a `SignatureDoesNotMatch` or `401 Unauthorized` error:

1. **Static Access Keys**: Ensure you are using **Static Access Keys** (ID and Secret) generated for your service account, NOT an IAM token.
2. **Trailing Spaces**: Check that your GitHub Secrets do not contain leading or trailing spaces.
3. **Permissions**: The service account associated with the keys must have at least `storage.viewer` and `storage.editor` roles for the specified bucket.
4. **Bucket Location**: Ensure your bucket is in the `ru-central1` region.

## Sample Python Client
```python
import requests
import json

def get_sscc_codes(function_url, prefix, count=1, extension=None):
    payload = {
        "prefix": prefix,
        "count": count
    }
    if extension is not None:
        payload["extension"] = extension
    
    try:
        response = requests.post(function_url, json=payload)
        response.raise_for_status()
        data = response.json()
        return data.get("ssccs", [])
    except requests.exceptions.RequestException as e:
        print(f"Error calling SSCC generator: {e}")
        if response.text:
            print(f"Server response: {response.text}")
        return None

# Usage example:
URL = "https://functions.yandexcloud.net/YOUR_FUNCTION_ID"
MY_PREFIX = "460705179"

codes = get_sscc_codes(URL, MY_PREFIX, count=3, extension="1")
if codes:
    print("Generated SSCC Codes:")
    for code in codes:
        print(code)
```

## Deployment
Deployment is handled automatically by GitHub Actions:
- **CI/CT**: Triggered on feature branches and pull requests to `main`.
- **CD**: Triggered on push to the `main` branch.

Ensure your Yandex Cloud federation is configured with `repo:user/repo:environment:preprod` for CI/CT and `repo:user/repo:ref:refs/heads/main` for CD.
