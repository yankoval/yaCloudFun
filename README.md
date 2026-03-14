# SSCC Generator Yandex Cloud Function

This project provides a Yandex Cloud Function to generate Serial Shipping Container Codes (SSCC) according to the GS1 Modulo 10 algorithm. It maintains a persistent serial counter using Yandex Object Storage.

## Features
- **SSCC-18 Generation**: Generates 18-digit SSCC codes.
- **Persistence**: Automatically increments and stores serial numbers in Yandex Object Storage.
- **CI/CD**: Fully automated deployment via GitHub Actions with OIDC federation.
- **Flexible Input**: Supports both JSON body and Query String parameters.

## Directory Structure
- `sscc_generator/`: Python source code and requirements.
- `.github/workflows/`: CI/CD workflows for automated deployment.

## Prerequisites
1. **Yandex Object Storage Bucket**: `20ab2a0c-2726-4ba1-9c7c-7deae82941ff`.
2. **Counter Configuration**: Create a JSON file in the bucket at `sscc/{prefix}.json` (e.g., `sscc/460705179.json`) with initial content:
   ```json
   {
     "next_serial": 0
   }
   ```
3. **GitHub Secrets**:
   - `YC_FOLDER_ID`: Your Yandex Cloud Folder ID.
   - `YC_SA_ID`: Service Account ID with `functions.admin` role.
   - `AWS_ACCESS_KEY_ID`: Static access key for S3.
   - `AWS_SECRET_ACCESS_KEY`: Static secret key for S3.

## Usage

### Function Input
The function accepts the following parameters:
- `prefix`: (Required) Your GS1 Company Prefix.
- `extension`: (Optional, default "0") SSCC extension digit (0-9).
- `count`: (Optional, default 1) Number of codes to generate.

### Sample Request
**POST** to function URL with JSON body:
```json
{
  "prefix": "460705179",
  "extension": "0",
  "count": 5
}
```

## Troubleshooting Credentials

If you receive a `SignatureDoesNotMatch` or `401 Unauthorized` error:

1. **Static Access Keys**: Ensure you are using **Static Access Keys** (ID and Secret) generated for your service account, NOT an IAM token or temporary credentials.
2. **Trailing Spaces**: Check that your GitHub Secrets do not contain leading or trailing spaces or newline characters.
3. **Permissions**: The service account associated with the keys must have at least `storage.viewer` and `storage.editor` roles for the specified bucket.
4. **Bucket Location**: Ensure your bucket is located in the `ru-central1` region (default for Yandex Cloud).

## Sample Python Client
```python
import requests
import json

def get_sscc_codes(function_url, prefix, count=1, extension="0"):
    payload = {
        "prefix": prefix,
        "extension": extension,
        "count": count
    }

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

codes = get_sscc_codes(URL, MY_PREFIX, count=3)
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
