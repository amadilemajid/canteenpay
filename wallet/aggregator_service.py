import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Student, WalletTopUp
from .sms_service import send_topup_receipt_sms

logger = logging.getLogger(__name__)


def _flw_v3_base_url() -> str:
    """Normalizes FLW_BASE_URL so Flutterwave v3 endpoints always include /v3 once."""
    base = (getattr(settings, "FLW_BASE_URL", "") or "https://api.flutterwave.com").rstrip("/")
    return base if base.endswith("/v3") else f"{base}/v3"


def initiate_ug_mobile_money_split(
    tx_ref: str,
    payer_phone: str,
    student_amount: int,
    platform_fee: int,
    school_subaccount_id: str,
    student_name: str,
    parent_email: str = "parents@canteenpay.ug",
    redirect_url: str = "",
) -> dict:
    """
    Initiates a Uganda Mobile Money (MTN or Airtel) charge via Flutterwave
    and automatically routes `student_amount` straight to the school's bank
    subaccount while keeping `platform_fee` in your account.
    """
    secret_key = (getattr(settings, "FLW_SECRET_KEY", "") or "").strip()
    upper_key = secret_key.upper()
    if (
        not secret_key
        or "YOUR-SECRET-KEY" in upper_key
        or "PLACEHOLDER" in upper_key
    ):
        return {
            "status": "error",
            "message": "Flutterwave API key is not configured on the server.",
        }

    total_charge = student_amount + platform_fee
    network = "AIRTEL" if payer_phone.startswith(("25670", "25675", "25674")) else "MTN"

    url = f"{_flw_v3_base_url()}/charges?type=mobile_money_uganda"
    headers = {
        "Authorization": f"Bearer {secret_key}",
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
    if redirect_url:
        payload["redirect_url"] = redirect_url

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
    url = f"{_flw_v3_base_url()}/transactions/verify_by_reference?tx_ref={tx_ref}"
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

            student = (
                Student.objects.select_for_update()
                .select_related("school")
                .get(id=topup.student_id)
            )
            student.wallet_balance += topup.amount
            update_fields = ["wallet_balance"]
            if student.maybe_reset_low_balance_flag():
                update_fields.append("low_balance_alert_sent")
            student.save(update_fields=update_fields)

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


def reconcile_pending_topups(max_batch=None):
    """
    Checks PENDING top-ups older than 2 minutes against Flutterwave, credits
    verified ones, and marks unpaid ones older than 15 minutes as FAILED.
    Errors per record are logged and skipped so page loads never crash.
    """
    now = timezone.now()
    two_minutes_ago = now - timedelta(minutes=2)
    fifteen_minutes_ago = now - timedelta(minutes=15)

    qs = WalletTopUp.objects.filter(
        status="PENDING", created_at__lte=two_minutes_ago
    ).order_by("created_at")
    if max_batch is not None:
        qs = qs[:max_batch]

    pending_refs = list(qs.values_list("momo_reference_id", flat=True))
    credited_count = 0
    failed_count = 0
    still_pending_count = 0

    for ref_id in pending_refs:
        try:
            outcome, topup, _ = verify_and_credit_flw_topup(ref_id)
            if outcome == "CREDITED":
                credited_count += 1
            elif outcome == "FAILED":
                failed_count += 1
            elif outcome == "PENDING" and topup.created_at <= fifteen_minutes_ago:
                with transaction.atomic():
                    locked = WalletTopUp.objects.select_for_update().get(pk=topup.pk)
                    if locked.status == "PENDING":
                        locked.status = "FAILED"
                        locked.save(update_fields=["status"])
                        failed_count += 1
            elif outcome == "PENDING":
                still_pending_count += 1
        except Exception:
            logger.exception("Error reconciling pending top-up %s", ref_id)

    return {
        "checked": len(pending_refs),
        "credited": credited_count,
        "failed": failed_count,
        "still_pending": still_pending_count,
    }