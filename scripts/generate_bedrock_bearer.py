"""Generate a long-term Bedrock API key using IAM service-specific credentials.

Unlike the short-term bearer token (generate_shortlived_bedrock_bearer.py), this key's
lifetime is NOT bounded by the caller's session credentials.
The key lives for the specified number of days regardless.

The token is stored in SSM Parameter Store for safe distributed access.
On subsequent runs, the script reads from SSM and verifies the token still
works - no IAM mutation, no propagation delay, no disruption to other runners.

A new credential is only created when:
- No token exists in SSM (first run)
- The existing credential is expiring soon (< --min-remaining-days)
- --force is specified

Usage:
    python scripts/generate_bedrock_bearer.py
    python scripts/generate_bedrock_bearer.py --days 90
    python scripts/generate_bedrock_bearer.py --force
    python scripts/generate_bedrock_bearer.py --no-verify
"""

import argparse
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("generate_bedrock_bearer")
logging.basicConfig(level=logging.INFO)

DEFAULT_DAYS = 30
DEFAULT_MIN_REMAINING_DAYS = 1
POLICY_ARN = "arn:aws:iam::aws:policy/AmazonBedrockFullAccess"
SERVICE_NAME = "bedrock.amazonaws.com"
IAM_USER_NAME = "bedrock-api-user"
# SSM is regional - all reads/writes must use the same region.
# The API key itself is global (works against any Bedrock regional endpoint).
SSM_REGION = "us-east-1"
SSM_PARAMETER = "/bedrock-aws-bench/bedrock-api-key"


def ensure_iam_user(iam_client, user_name: str) -> None:
    """Ensure the IAM user exists and has the Bedrock policy attached."""
    try:
        iam_client.create_user(UserName=user_name)
        logger.info(f"Created IAM user: {user_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise

    iam_client.attach_user_policy(UserName=user_name, PolicyArn=POLICY_ARN)


def get_existing_credentials(iam_client, user_name: str) -> list[dict]:
    """List existing service-specific credentials for Bedrock."""
    response = iam_client.list_service_specific_credentials(
        UserName=user_name,
        ServiceName=SERVICE_NAME,
    )
    return response.get("ServiceSpecificCredentials", [])


def find_reusable_credential(credentials: list[dict], min_remaining_days: int) -> dict | None:
    """Return the first active credential with enough remaining life, or None."""
    now = datetime.now(timezone.utc)
    min_expiration = now + timedelta(days=min_remaining_days)
    for cred in credentials:
        if cred["Status"] != "Active":
            continue
        expiration = cred.get("ExpirationDate")
        if expiration and expiration <= min_expiration:
            continue
        return cred
    return None


def delete_all_credentials(iam_client, user_name: str, credentials: list[dict]) -> None:
    """Delete all existing service-specific credentials for the user."""
    for cred in credentials:
        cred_id = cred["ServiceSpecificCredentialId"]
        iam_client.delete_service_specific_credential(
            UserName=user_name,
            ServiceSpecificCredentialId=cred_id,
        )
        logger.info(f"Deleted existing credential: {cred_id}")


def generate_longterm_key(iam_client, user_name: str, days: int) -> dict:
    """Generate a long-term Bedrock API key via CreateServiceSpecificCredential."""
    response = iam_client.create_service_specific_credential(
        UserName=user_name,
        ServiceName=SERVICE_NAME,
        CredentialAgeDays=days,
    )
    credential = response["ServiceSpecificCredential"]
    return {
        "api_key": credential["ServiceCredentialSecret"],
        "credential_id": credential["ServiceSpecificCredentialId"],
    }


def get_token_from_ssm(ssm_client, parameter_name: str) -> str | None:
    """Read the token from SSM Parameter Store, or None if not found."""
    try:
        response = ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)
        return response["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return None
        raise


def store_token_in_ssm(ssm_client, parameter_name: str, api_key: str) -> None:
    """Store the token in SSM Parameter Store as a SecureString."""
    ssm_client.put_parameter(
        Name=parameter_name,
        Value=api_key,
        Type="SecureString",
        Overwrite=True,
        Description="Long-term Bedrock API key managed by generate_bedrock_bearer.py",
    )
    logger.info(f"Token stored in SSM: {parameter_name}")


def verify_token(api_key: str, retries: int = 3, delay: int = 5) -> bool:
    """Verify the token works with a lightweight Bedrock API call."""
    url = "https://bedrock.us-east-1.amazonaws.com/foundation-models/amazon.titan-embed-text-v1"
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True
        except urllib.error.HTTPError as e:
            if e.code == 403 and attempt < retries - 1:
                logger.info(f"Got 403, retrying in {delay}s (IAM propagation delay)...")
                time.sleep(delay)
                continue
            logger.error(f"Token verification failed: HTTP {e.code} - {e.reason}")
        except Exception as e:
            logger.error(f"Token verification failed: {e}")
            break
    return False


def write_output(api_key: str, output: str) -> None:
    """Write the key to the local output file."""
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"export AWS_BEARER_TOKEN_BEDROCK={api_key}\n")
    logger.info(f"Key written to {output}")


def main() -> None:
    """Generate or reuse a long-term Bedrock API key, stored in SSM."""
    parser = argparse.ArgumentParser(
        description="Generate a long-term Bedrock API key (not bounded by session TTL)."
    )
    parser.add_argument(
        "--user-name",
        default=IAM_USER_NAME,
        help=f"IAM user name to associate the key with (default: {IAM_USER_NAME}).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Number of days the key should be valid (default: {DEFAULT_DAYS}).",
    )
    parser.add_argument(
        "--min-remaining-days",
        type=int,
        default=DEFAULT_MIN_REMAINING_DAYS,
        help="Minimum days of remaining validity to reuse a credential "
        f"(default: {DEFAULT_MIN_REMAINING_DAYS}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete existing credentials and create a fresh one.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip token verification.",
    )
    parser.add_argument(
        "--output",
        default="bedrock.env",
        help="Output file path for the bearer token (default: bedrock.env).",
    )
    args = parser.parse_args()

    session = boto3.Session(region_name=SSM_REGION)
    iam_client = session.client("iam")
    ssm_client = session.client("ssm")

    # Step 1: Check credential expiration first (cheap IAM read)
    ensure_iam_user(iam_client, args.user_name)
    existing = get_existing_credentials(iam_client, args.user_name)
    credential_expiring = False

    if existing and not args.force:
        reusable = find_reusable_credential(existing, args.min_remaining_days)
        if not reusable:
            credential_expiring = True
            logger.info(f"Credential expiring within {args.min_remaining_days}d - will rotate.")

    # Step 2: Try to reuse the token from SSM (fast path, no IAM mutation)
    if not args.force and not credential_expiring:
        cached_token = get_token_from_ssm(ssm_client, SSM_PARAMETER)
        if cached_token:
            logger.info(f"Found token in SSM ({SSM_PARAMETER}), verifying...")
            if args.no_verify or verify_token(cached_token, retries=1):
                logger.info("Reusing valid token from SSM.")
                write_output(cached_token, args.output)
                return
            logger.info(
                "Token from SSM failed verification (expired or revoked). Generating a new one..."
            )
        else:
            logger.info(f"No token found in SSM ({SSM_PARAMETER}).")

    # Step 3: Rotate - delete expiring/force credentials
    if credential_expiring or args.force:
        if existing:
            delete_all_credentials(iam_client, args.user_name, existing)
            existing = []

    # Step 4: Generate a new credential
    if not existing:
        logger.info(
            f"Generating long-term Bedrock key for user '{args.user_name}' "
            f"(valid {args.days} days)..."
        )
        try:
            result = generate_longterm_key(iam_client, args.user_name, args.days)
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "NoSuchEntity":
                logger.error(f"IAM user '{args.user_name}' does not exist.")
            elif error_code == "LimitExceeded":
                logger.error(
                    "Limit exceeded: max 2 service-specific credentials per user per service. "
                    "Use --force to delete existing credentials and create a fresh one."
                )
            else:
                logger.error(f"Failed to generate key: {e}")
            sys.exit(1)

        api_key = result["api_key"]
        logger.info(f"Credential ID: {result['credential_id']}")
    else:
        cred_ids = [c["ServiceSpecificCredentialId"] for c in existing]
        expirations = [str(c.get("ExpirationDate", "unknown")) for c in existing]
        logger.error(
            f"Found {len(existing)} IAM credential(s) with enough remaining life: {cred_ids} "
            f"(expirations: {expirations}), but cannot retrieve their secret without resetting "
            "(which would invalidate the key for other runners). "
            f"Token in SSM ({SSM_PARAMETER}) was invalid or missing. "
            "Use --force to delete existing credentials and create a fresh one."
        )
        sys.exit(1)

    # Step 5: Verify, store in SSM, and write locally
    if not args.no_verify:
        logger.info("Verifying token against Bedrock API...")
        if not verify_token(api_key):
            logger.warning("Token verification failed - the key may not be usable yet.")
            sys.exit(1)
        logger.info("Token verified successfully.")

    store_token_in_ssm(ssm_client, SSM_PARAMETER, api_key)
    write_output(api_key, args.output)


if __name__ == "__main__":
    main()
