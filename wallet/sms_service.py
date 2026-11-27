import logging
from decimal import Decimal
from django.conf import settings
from africastalking.SMS import SMSService

logger = logging.getLogger(__name__)

_PLACEHOLDER_PHONES = {"n/a", "-", "none"}


def is_valid_phone(phone) -> bool:
    """False for missing, blank, or placeholder numbers such as N/A, -, or none."""
    if phone is None:
        return False
    cleaned = str(phone).strip()
    if not cleaned or cleaned.lower() in _PLACEHOLDER_PHONES:
        return False
    return True


def send_welcome_pin_sms(parent_phone: str, student_name: str, class_stream: str, code: str, pin: str):
    """
    One-time PIN delivery. The plaintext PIN is not stored; callers pass it
    only for this send and the take-home slip.
    """
    if not is_valid_phone(parent_phone):
        return None

    message = (
        f"CanteenPay: {student_name}"
        f"{f' ({class_stream})' if class_stream else ''}. "
        f"Canteen code {code}. One-time PIN {pin}. "
        f"Give this to the student. Do not file this message."
    )
    return _send_sms(parent_phone, message)


def send_topup_receipt_sms(
    parent_phone: str,
    student_name: str,
    amount_credited: str,
    new_balance: str,
    tx_ref: str = "",
):
    """
    Sends an instant SMS receipt to the parent once payment is confirmed.
    Skips quietly when the student has no usable parent phone.
    """
    if not is_valid_phone(parent_phone):
        return None

    amount_int = int(Decimal(str(amount_credited)))
    balance_int = int(Decimal(str(new_balance)))
    ref_part = f" Ref: {tx_ref}." if tx_ref else ""
    message = (
        f"CanteenPay Receipt: UGX {amount_int:,} has been credited "
        f"to {student_name}'s canteen wallet. New Balance: UGX {balance_int:,}.{ref_part} "
        f"Thank you!"
    )
    return _send_sms(parent_phone, message)


def send_refund_receipt_sms(
    parent_phone: str, student_name: str, amount_refunded: str, new_balance: str
):
    """Notifies the parent when a canteen order is refunded to the student's wallet."""
    if not is_valid_phone(parent_phone):
        return None

    amount_int = int(Decimal(str(amount_refunded)))
    balance_int = int(Decimal(str(new_balance)))
    message = (
        f"CanteenPay Refund: UGX {amount_int:,} has been refunded "
        f"to {student_name}'s canteen wallet. New Balance: UGX {balance_int:,}."
    )
    return _send_sms(parent_phone, message)


def send_pin_lockout_alert_sms(parent_phone: str, student_name: str, code: str):
    """Alerts the parent if a student's PIN is temporarily locked after 5 failed attempts."""
    if not is_valid_phone(parent_phone):
        return None

    message = (
        f"CanteenPay Security Alert: Wallet {code} ({student_name}) was locked for 15 minutes "
        f"after 5 incorrect PIN attempts. Contact the school bursar if needed."
    )
    return _send_sms(parent_phone, message)


def _send_sms(parent_phone: str, message: str):
    if not is_valid_phone(parent_phone):
        return None

    # Instantiate directly to avoid stale global state from africastalking.initialize()
    sms = SMSService(settings.AT_USERNAME, settings.AT_API_KEY)

    # Normalize phone to +256XXXXXXXXX
    cleaned = parent_phone.strip().replace(" ", "")
    if cleaned.startswith("+"):
        formatted_phone = cleaned
    elif cleaned.startswith("256"):
        formatted_phone = f"+{cleaned}"
    elif cleaned.startswith("0") and len(cleaned) == 10:
        formatted_phone = f"+256{cleaned[1:]}"
    else:
        formatted_phone = f"+{cleaned}"

    try:
        response = sms.send(message, [formatted_phone])
        return response
    except Exception:
        logger.exception("SMS delivery failed for %s", formatted_phone)
        return None
