import json
import boto3
import os
import time
import random
import logging
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Constants
BUCKET_NAME = '20ab2a0c-2726-4ba1-9c7c-7deae82941ff'
STORAGE_FOLDER = 'sscc'
MAX_RETRIES = 5
INITIAL_BACKOFF = 0.1  # seconds
MAX_BACKOFF = 1.0

def get_s3_client():
    """Initializes the S3 client using environment variables."""
    access_key = os.environ.get('AWS_ACCESS_KEY_ID')
    secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
    
    if not access_key or not secret_key:
        raise ValueError("Missing S3 credentials. Please set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables.")
        
    return boto3.client(
        service_name='s3',
        endpoint_url='https://storage.yandexcloud.net',
        region_name='ru-central1',
        aws_access_key_id=access_key.strip(),
        aws_secret_access_key=secret_key.strip()
    )

def calculate_check_digit(number_str):
    """Calculates GS1 Modulo 10 check digit for an 17-digit string."""
    total = 0
    for i in range(len(number_str)):
        digit = int(number_str[-(i+1)])
        if (i + 2) % 2 == 0:
            total += digit * 3
        else:
            total += digit * 1
            
    check_digit = (10 - (total % 10)) % 10
    return str(check_digit)

def handler(event, context):
    try:
        # 0. Initialize S3 client
        try:
            s3 = get_s3_client()
        except ValueError as ve:
            return {'statusCode': 401, 'body': json.dumps({'error': str(ve)})}

        # 1. Parse input
        body = {}
        if isinstance(event.get('body'), str):
            try:
                body = json.loads(event['body'])
            except: pass
        elif isinstance(event.get('body'), dict):
            body = event['body']
            
        params = event.get('queryStringParameters', {}) or {}
        input_data = {**body, **params}
            
        prefix = str(input_data.get('prefix', ''))
        count = int(input_data.get('count', 1))
        
        if not prefix:
            return {'statusCode': 400, 'body': json.dumps({'error': 'Prefix is required'})}

        object_key = f"{STORAGE_FOLDER}/{prefix}.json"
        
        # 2. Update state with optimistic locking
        for attempt in range(MAX_RETRIES):
            try:
                # Read current state
                response = s3.get_object(Bucket=BUCKET_NAME, Key=object_key)
                etag = response['ETag']
                config = json.loads(response['Body'].read().decode('utf-8'))

                # Compatibility logic
                if "counters" not in config:
                    config = {
                        "default_extension": "0",
                        "counters": {"0": config.get("next_serial", 0)}
                    }

                # 3. Determine extension
                extension = str(input_data.get('extension', config.get('default_extension', '0')))

                if len(extension) != 1:
                    return {'statusCode': 400, 'body': json.dumps({'error': 'Extension must be a single digit'})}

                # 4. Get and increment specific counter
                counters = config.get("counters", {})
                current_serial = int(counters.get(extension, 0))

                # 5. Check for serial number overflow
                # Extension(1) + Prefix + Serial = 17 digits
                serial_len = 17 - 1 - len(prefix)
                if serial_len < 1:
                     return {'statusCode': 400, 'body': json.dumps({'error': 'Prefix is too long'})}

                max_serial_exclusive = 10**serial_len

                if current_serial + count > max_serial_exclusive:
                    return {
                        'statusCode': 400,
                        'body': json.dumps({
                            'error': f'Serial number overflow for extension {extension}. Max allowed value is {max_serial_exclusive - 1}.'
                        })
                    }

                # 6. Update state
                new_serial = current_serial + count
                counters[extension] = new_serial
                config["counters"] = counters

                s3.put_object(
                    Bucket=BUCKET_NAME,
                    Key=object_key,
                    Body=json.dumps(config).encode('utf-8'),
                    IfMatch=etag
                )

                # Success! Generate SSCCs
                ssccs = []
                for i in range(count):
                    serial_val = current_serial + i
                    serial_str = str(serial_val).zfill(serial_len)
                    base_number = extension + prefix + serial_str
                    check_digit = calculate_check_digit(base_number)
                    ssccs.append(base_number + check_digit)

                return {
                    'statusCode': 200,
                    'body': json.dumps({'ssccs': ssccs})
                }

            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code')

                if error_code == 'PreconditionFailed' and attempt < MAX_RETRIES - 1:
                    # Conflict detected, retry with backoff
                    sleep_time = min(MAX_BACKOFF, INITIAL_BACKOFF * (2 ** attempt) + random.random() * 0.1)
                    logger.info(f"Conflict detected for prefix {prefix}. Retry attempt {attempt + 1}/{MAX_RETRIES} after {sleep_time:.2f}s.")
                    time.sleep(sleep_time)
                    continue

                if error_code == 'NoSuchKey':
                    return {'statusCode': 404, 'body': json.dumps({'error': f'Configuration for prefix {prefix} not found'})}

                if error_code == 'PreconditionFailed':
                    return {'statusCode': 409, 'body': json.dumps({'error': 'Conflict: too many concurrent requests. Please retry.'})}

                return {'statusCode': 500, 'body': json.dumps({'error': f'S3 Error: {str(e)}'})}

    except Exception as e:
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}
