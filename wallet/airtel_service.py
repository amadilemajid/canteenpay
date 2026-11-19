import requests
from django.conf import settings


def get_airtel_access_token() -> str:
    """
    Exchanges Airtel Client ID and Secret for a temporary OAuth2 Bearer Token.
    """
    url = f"{settings.AIRTEL_BASE_URL}/auth/oauth2/token"
    payload = {
        "client_id": settings.AIRTEL_CLIENT_ID,
        "client_secret": settings.AIRTEL_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }
    headers = {"Content-Type": "application/json"}

    response = requests.post(url, json=payload, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()["access_token"]


def format_airtel_msisdn(phone: str) -> str:
    """
    Converts 0752123456 or +256752123456 into 752123456 (9 digits required by Airtel).
    """
    cleaned = phone.strip().replace("+", "").replace(" ", "")
    if cleaned.startswith("256"):
        return cleaned[3:]
    if cleaned.startswith("0"):
        return cleaned[1:]
    return cleaned


def send_airtel_ussd_push(
    reference_id: str, payer_phone: str, total_amount: str, student_reg_no: str
) -> bool:
    """
    Triggers the Airtel Money PIN prompt on the parent's phone.
    """
    token = get_airtel_access_token()
    msisdn = format_airtel_msisdn(payer_phone)

    url = f"{settings.AIRTEL_BASE_URL}/merchant/v1/payments/"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Country": settings.AIRTEL_COUNTRY,
        "X-Currency": settings.AIRTEL_CURRENCY,
    }
    payload = {
        "reference": f"CanteenPay-{student_reg_no}",
        "subscriber": {
            "country": settings.AIRTEL_COUNTRY,
            "currency": settings.AIRTEL_CURRENCY,
            "msisdn": int(msisdn),
        },
        "transaction": {
            "amount": int(float(total_amount)),
            "country": settings.AIRTEL_COUNTRY,
            "currency": settings.AIRTEL_CURRENCY,
            "id": reference_id,
        },
    }

    response = requests.post(url, json=payload, headers=headers, timeout=15)
    data = response.json()
    return response.status_code == 200 and data.get("status", {}).get("success") is True
