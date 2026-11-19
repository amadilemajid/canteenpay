import base64
import requests
from django.conf import settings


def get_momo_access_token() -> str:
    """
    Exchanges your API User UUID and API Key for a temporary Bearer Token.
    """
    credentials = f"{settings.MOMO_API_USER}:{settings.MOMO_API_KEY}"
    encoded_creds = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")

    url = f"{settings.MOMO_BASE_URL}/collection/token/"
    headers = {
        "Authorization": f"Basic {encoded_creds}",
        "Ocp-Apim-Subscription-Key": settings.MOMO_SUBSCRIPTION_KEY,
    }

    response = requests.post(url, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()["access_token"]


def send_request_to_pay(
    reference_id: str,
    payer_phone: str,
    total_amount: str,
    external_id: str,
    student_name: str,
) -> int:
    """
    Sends the USSD payment prompt to the parent's MTN MoMo phone.
    Returns HTTP status code (202 means prompt sent successfully).
    """
    token = get_momo_access_token()

    url = f"{settings.MOMO_BASE_URL}/collection/v1_0/requesttopay"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Reference-Id": reference_id,
        "X-Target-Environment": settings.MOMO_TARGET_ENV,
        "Ocp-Apim-Subscription-Key": settings.MOMO_SUBSCRIPTION_KEY,
        "X-Callback-Url": settings.MOMO_CALLBACK_URL,
        "Content-Type": "application/json",
    }

    payload = {
        "amount": str(total_amount),
        "currency": settings.MOMO_CURRENCY,  # "EUR" in Sandbox, "UGX" in Production
        "externalId": external_id,
        "payer": {
            "partyIdType": "MSISDN",
            "partyId": payer_phone,
        },
        "payerMessage": f"Pocket money for {student_name}",
        "payeeNote": "CanteenPay Wallet TopUp",
    }

    response = requests.post(url, json=payload, headers=headers, timeout=15)
    if response.status_code != 202:
        print(f"MTN REJECTED ({response.status_code}): {response.text}")
    return response.status_code, response.text
