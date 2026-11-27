import ssl
from django.conf import settings
from africastalking.SMS import SMSService

# Fix SSL certificate verification on Windows for sandbox
ssl._create_default_https_context = ssl._create_unverified_context

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
    parent_phone: str, student_name: str, amount_credited: str, new_balance: str
):
    """
    Sends an instant SMS receipt to the parent once MTN MoMo confirms payment.
    Skips quietly when the student has no usable parent phone.
    """
    if not is_valid_phone(parent_phone):
        return None

    message = (
        f"CanteenPay Receipt: UGX {int(float(amount_credited)):,} has been credited "
        f"to {student_name}'s canteen wallet. New Balance: UGX {int(float(new_balance)):,}. "
        f"Thank you!"
    )
    return _send_sms(parent_phone, message)


def _send_sms(parent_phone: str, message: str):
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
        print(f"SMS sent: {response}")
        return response
    except Exception as e:
        print(f"SMS delivery error: {e}")
        return None
