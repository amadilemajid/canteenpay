from decimal import Decimal, InvalidOperation
import requests
from django.conf import settings
from django.db import transaction

from .models import Student, WalletTopUp
from .sms_service import send_topup_receipt_sms


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


def _parse_decimal(value) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def verify_and_credit_flw_topup(reference_id):
    """
    Verifies a WalletTopUp server-side against Flutterwave inside a locked
    atomic transaction and credits the student's wallet only if all payment
    integrity checks pass.
    Returns (outcome, topup, student) where outcome is one of:
    ALREADY_PROCESSED, NOT_PENDING, CREDITED, FAILED, PENDING.
    """
    with transaction.atomic():
        topup = WalletTopUp.objects.select_for_update().get(
            momo_reference_id=reference_id
        )
        if topup.status == "SUCCESSFUL":
            return "ALREADY_PROCESSED", topup, topup.student
        if topup.status != "PENDING":
            return "NOT_PENDING", topup, topup.student

        flw_check = verify_flw_transaction_by_ref(str(topup.momo_reference_id))
        tx_data = flw_check.get("data") or {}
        paid_amount = _parse_decimal(tx_data.get("amount"))
        expected_total = topup.amount + topup.platform_fee

        if (
            flw_check.get("status") == "success"
            and tx_data.get("status") == "successful"
            and tx_data.get("currency") == "UGX"
            and paid_amount >= expected_total
        ):
            topup.status = "SUCCESSFUL"
            topup.save(update_fields=["status"])

            student = Student.objects.select_for_update().get(id=topup.student_id)
            student.wallet_balance += topup.amount
            student.save(update_fields=["wallet_balance"])

            short_ref = str(topup.momo_reference_id)[:8].upper()
            parent_phone = student.parent_phone
            student_name = student.full_name
            amount_str = str(topup.amount)
            new_balance_str = str(student.wallet_balance)
            transaction.on_commit(
                lambda: send_topup_receipt_sms(
                    parent_phone=parent_phone,
                    student_name=student_name,
                    amount_credited=amount_str,
                    new_balance=new_balance_str,
                    tx_ref=short_ref,
                )
            )
            return "CREDITED", topup, student

        if flw_check.get("status") == "success" and tx_data.get("status") == "failed":
            topup.status = "FAILED"
            topup.save(update_fields=["status"])
            return "FAILED", topup, topup.student

        return "PENDING", topup, topup.student