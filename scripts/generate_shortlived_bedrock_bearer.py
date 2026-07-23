import base64
import logging
import os

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger("generate_bedrock_bearer")
logging.basicConfig(level=logging.INFO)

# Important: make sure that the region for which the token is generated
# is matching regions where calls will be made (both for agent
# and LLM Judge for eg.)
DEFAULT_REGION = "us-east-1"


def generate_bedrock_token(session, region=DEFAULT_REGION, expires_in=43200) -> str | None:
    """Generate a short-lived Bedrock bearer token using SigV4 presigning."""
    creds = session.get_credentials().get_frozen_credentials()
    signer = SigV4QueryAuth(creds, "bedrock", region, expires=expires_in)
    request = AWSRequest(
        method="POST",
        url="https://bedrock.amazonaws.com/?Action=CallWithBearerToken",
        headers={"host": "bedrock.amazonaws.com"},
    )
    signer.add_auth(request)
    if not request.url:
        return None
    presigned = request.url.replace("https://", "") + "&Version=1"
    encoded = base64.b64encode(presigned.encode()).decode()
    return f"bedrock-api-key-{encoded}"


if __name__ == "__main__":
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    logger.info(
        f"Generating Bedrock token in AWS_REGION: {region}. Make sure LLM inference "
        "(agent, verifier, etc.) is made in the same region!"
    )
    session = boto3.Session(region_name=region)
    token = generate_bedrock_token(session=session, region=region)

    if token:
        fd = os.open("bedrock.env", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("AWS_BEARER_TOKEN_BEDROCK=" + token)
    else:
        raise Exception("Failed to generate token.")
