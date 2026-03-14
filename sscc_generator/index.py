import json
import boto3
from botocore.exceptions import ClientError

# Constants
BUCKET_NAME = '20ab2a0c-2726-4ba1-9c7c-7deae82941ff'
STORAGE_FOLDER = 'sscc'

# Initialize S3 client for Yandex Object Storage outside the handler for warm starts
s3 = boto3.client(
    service_name='s3',
    endpoint_url='https://storage.yandexcloud.net'
)

def calculate_check_digit(number_str):
    """Calculates GS1 Modulo 10 check digit for an 17-digit string."""
    # GS1 SSCC (18 digits)
    # Positions 1 (rightmost, check digit) to 18 (leftmost)
    # Even positions (2, 4, 6...): * 3
    # Odd positions (3, 5, 7...): * 1

    total = 0
    # Process from right to left (excluding the check digit position)
    for i in range(len(number_str)):
        digit = int(number_str[-(i+1)])
        # Position 2 is index 0 in reversed, Position 3 is index 1...
        if (i + 2) % 2 == 0:
            total += digit * 3
        else:
            total += digit * 1

    check_digit = (10 - (total % 10)) % 10
    return str(check_digit)

def handler(event, context):
    try:
        # Parse input from body or query string parameters
        # Support both for compatibility with different test tools
        body = {}
        if isinstance(event.get('body'), str):
            try:
                body = json.loads(event['body'])
            except:
                pass
        elif isinstance(event.get('body'), dict):
            body = event['body']

        params = event.get('queryStringParameters', {}) or {}

        # Merge params into body, prioritizing query string
        input_data = {**body, **params}

        count = int(input_data.get('count', 1))
        prefix = str(input_data.get('prefix', ''))
        extension = str(input_data.get('extension', '0'))

        if not prefix:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Prefix is required'})
            }

        if len(extension) != 1:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Extension must be a single digit'})
            }

        object_key = f"{STORAGE_FOLDER}/{prefix}.json"

        try:
            # 1. Get current state
            response = s3.get_object(Bucket=BUCKET_NAME, Key=object_key)
            data = json.loads(response['Body'].read().decode('utf-8'))

            current_serial = int(data.get('next_serial', 0))

            # 2. Increment serial
            new_serial = current_serial + count
            data['next_serial'] = new_serial

            # 3. Save back
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=object_key,
                Body=json.dumps(data)
            )

            # 4. Generate SSCCs
            ssccs = []
            serial_len = 17 - 1 - len(prefix)

            if serial_len < 1:
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': 'Prefix length is too long for SSCC structure'})
                }

            for i in range(count):
                serial_val = current_serial + i
                serial_str = str(serial_val).zfill(serial_len)

                if len(serial_str) > serial_len:
                    return {
                        'statusCode': 400,
                        'body': json.dumps({'error': f'Serial number overflow: {serial_str} > {serial_len} digits'})
                    }

                base_number = extension + prefix + serial_str
                check_digit = calculate_check_digit(base_number)
                # Return 18-digit SSCC
                sscc = base_number + check_digit
                ssccs.append(sscc)

            return {
                'statusCode': 200,
                'body': json.dumps({'ssccs': ssccs})
            }

        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code')
            if error_code == 'NoSuchKey':
                return {
                    'statusCode': 404,
                    'body': json.dumps({'error': f'Configuration file {object_key} not found'})
                }
            else:
                return {
                    'statusCode': 500,
                    'body': json.dumps({'error': f'S3 Error: {str(e)}'})
                }

    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }
