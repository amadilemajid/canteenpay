import ssl
from django.conf import settings
from africastalking.SMS import SMSService

# Fix SSL certificate verification on Windows for sandbox
ssl._create_default_https_context = ssl._create_unverified_context


def send_topup_receipt_sms(
    parent_phone: str, student_name: str, amount_credited: str, new_balance: str
):
    """
    Sends an instant SMS receipt to the parent once MTN MoMo confirms payment.
    """
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

    message = (
        f"CanteenPay Receipt: UGX {int(float(amount_credited)):,} has been credited "
        f"to {student_name}'s canteen wallet. New Balance: UGX {int(float(new_balance)):,}. "
        f"Thank you!"
    )

    try:
        response = sms.send(message, [formatted_phone])
        print(f"SMS sent: {response}")
        return response
    except Exception as e:
        print(f"SMS delivery error: {e}")
        return None
