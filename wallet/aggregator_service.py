import requests
from django.conf import settings


def initiate_ug_mobile_money_split(
    tx_ref: str,
    payer_phone: str,
    student_amount: int,
    platform_fee: int,
    school_subaccount_id: str,
    student_name: str,
    parent_email: str = "parents@canteenpay.ug",
) -> dict:
    """
    Initiates a Uganda Mobile Money (MTN or Airtel) charge via Flutterwave
    and automatically routes `student_amount` straight to the school's bank
    subaccount while keeping `platform_fee` in your account.
    """
    total_charge = student_amount + platform_fee
    network = "AIRTEL" if payer_phone.startswith(("25670", "25675", "25674")) else "MTN"

    url = f"{settings.FLW_BASE_URL}/charges?type=mobile_money_uganda"
    headers = {
        "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "tx_ref": tx_ref,
        "amount": total_charge,
        "currency": "UGX",
        "network": network,
        "email": parent_email,
        "phone_number": payer_phone,
        "fullname": f"Parent of {student_name}",
        "subaccounts": [
            {
                "id": school_subaccount_id,
                "transaction_charge_type": "flat_subaccount",
                # Amount deducted from the school's share = your platform fee
                "transaction_charge": platform_fee,
            }
        ],
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
    except Exception as e:
        return {"status": "error", "message": f"Flutterwave connection error: {str(e)}"}
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError:
        return {
            "status": "error",
            "message": f"Flutterwave returned non-JSON response: {response.text[:200]}"
        }


def verify_flw_transaction_by_ref(tx_ref: str) -> dict:
    """
    Directly queries Flutterwave to verify a transaction's final status
    using our WalletTopUp momo_reference_id (tx_ref).
    """
    url = f"{settings.FLW_BASE_URL}/transactions/verify_by_reference?tx_ref={tx_ref}"
    headers = {
        "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.get(url, headers=headers, timeout=20)
        return response.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}