import base64
import csv
import hmac
import io
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import qrcode
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .aggregator_service import (
    initiate_ug_mobile_money_split,
    reconcile_pending_topups,
    verify_and_credit_flw_topup,
)
from .models import (
    BursarCashTopUp,
    CanteenOrder,
    CanteenRefund,
    School,
    SchoolOnboardingRequest,
    Student,
    WalletTopUp,
)
from .sms_service import (
    send_canteen_purchase_sms,
    send_pin_lockout_alert_sms,
)

logger = logging.getLogger(__name__)

PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT_MINUTES = 15
DOUBLE_TAP_WINDOW_SECONDS = 10
LOOKUP_RATE_LIMIT = 20
LOOKUP_RATE_WINDOW_SECONDS = 60


def _client_ip(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    return (request.META.get("REMOTE_ADDR") or "").strip() or "unknown"


def normalize_ug_phone(phone: str) -> str:
    """
    Converts local formats like 0772123456 or +256772123456 into 256772123456.
    """
    cleaned = phone.strip().replace("+", "").replace(" ", "")
    if cleaned.startswith("0") and len(cleaned) == 10:
        return "256" + cleaned[1:]
    return cleaned


# Valid Uganda MNO prefixes (after leading 0 → 256):  MTN 077/078/076/039, Airtel 070/074/075
_UG_VALID_PREFIX_RE = re.compile(r"^256(70|74|75|76|77|78|39)\d{7}$")


def _validate_ug_phone(raw_phone: str) -> tuple:
    """
    Returns (normalized_number, None) on success or (None, error_message) on failure.
    Accepts: 07XXXXXXXX (10 digits), 256XXXXXXXXX (12 digits), +256XXXXXXXXX (13 chars).
    """
    cleaned = raw_phone.strip().replace("+", "").replace(" ", "").replace("-", "")

    if cleaned.startswith("0"):
        if len(cleaned) < 10:
            return None, "Phone number is too short — Uganda numbers have 10 digits (e.g. 0772123456)."
        if len(cleaned) > 10:
            return None, "Phone number is too long — Uganda numbers have 10 digits (e.g. 0772123456)."
        normalized = "256" + cleaned[1:]
    elif cleaned.startswith("256"):
        if len(cleaned) != 12:
            return None, "Invalid Uganda international format — expected 256XXXXXXXXX (12 digits)."
        normalized = cleaned
    else:
        return None, "Enter a Uganda phone number starting with 07… or 256… (e.g. 0772123456)."

    if not _UG_VALID_PREFIX_RE.match(normalized):
        return None, (
            "Unrecognised network — enter an MTN (077/078/076) or Airtel (070/074/075) Uganda number."
        )
    return normalized, None


def _parse_json_body(request):
    try:
        data = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _is_placeholder_flw_key(secret_key: str) -> bool:
    key = (secret_key or "").strip().upper()
    return (
        not key
        or "TEST" in key
        or "YOUR-SECRET-KEY" in key
        or "PLACEHOLDER" in key
    )


def _student_net_spent_today(student, today) -> Decimal:
    spent_today = (
        CanteenOrder.objects.filter(student=student, created_at__date=today).aggregate(
            total=Sum("amount")
        )["total"]
        or Decimal("0.00")
    )
    refunded_today = (
        CanteenRefund.objects.filter(
            order__student=student, order__created_at__date=today
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    return max(spent_today - refunded_today, Decimal("0.00"))


def _verify_pin_or_lockout(student: Student, raw_pin: str):
    """
    Shared 5-attempt PIN check and 15-minute lockout across POS endpoints.
    Must be called inside transaction.atomic() with the student row locked.
    Returns None when valid, or a JsonResponse on failure/lockout.
    """
    now = timezone.now()
    if student.pin_locked_until and student.pin_locked_until > now:
        return JsonResponse(
            {"error": "PIN locked after 5 wrong attempts. Try again in 15 minutes."},
            status=423,
        )

    if not raw_pin or not student.verify_pin(raw_pin):
        student.failed_pin_attempts += 1
        if student.failed_pin_attempts >= PIN_MAX_ATTEMPTS:
            student.pin_locked_until = now + timedelta(minutes=PIN_LOCKOUT_MINUTES)
            student.failed_pin_attempts = 0
            student.save(update_fields=["failed_pin_attempts", "pin_locked_until"])

            parent_phone = student.parent_phone
            student_name = student.full_name
            code = student.student_reg_no
            transaction.on_commit(
                lambda: send_pin_lockout_alert_sms(parent_phone, student_name, code)
            )
            return JsonResponse(
                {"error": "PIN locked after 5 wrong attempts. Try again in 15 minutes."},
                status=423,
            )

        student.save(update_fields=["failed_pin_attempts"])
        return JsonResponse({"error": "Incorrect student PIN."}, status=401)

    if student.failed_pin_attempts != 0 or student.pin_locked_until is not None:
        student.failed_pin_attempts = 0
        student.pin_locked_until = None
        student.save(update_fields=["failed_pin_attempts", "pin_locked_until"])
    return None


@login_required(login_url="/staff/login/")
@require_POST
def canteen_checkout(request):
    """
    Called by the canteen tablet after scanning the student's QR code
    and entering the purchase amount + student's 4-digit PIN.
    """
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request payload."}, status=400)

    reg_no = str(data.get("student_reg_no", "")).strip()
    raw_pin = str(data.get("pin", "")).strip()
    items_note = str(data.get("items_note", "")).strip()[:255]
    raw_idem = str(data.get("idempotency_key") or "").strip()[:64]
    idempotency_key = raw_idem or None

    if not reg_no:
        return JsonResponse({"error": "CanteenPay code is required."}, status=400)

    try:
        amount = Decimal(str(data.get("amount", "0")))
    except (InvalidOperation, ValueError, TypeError):
        return JsonResponse({"error": "Invalid purchase amount."}, status=400)

    if amount <= 0:
        return JsonResponse({"error": "Invalid purchase amount."}, status=400)
    if amount != amount.to_integral_value():
        return JsonResponse(
            {"error": "Enter a whole UGX purchase amount (no decimals)."},
            status=400,
        )

    try:
        with transaction.atomic():
            student = (
                Student.objects.select_for_update()
                .select_related("school")
                .get(student_reg_no__iexact=reg_no, is_active=True)
            )

            pin_error = _verify_pin_or_lockout(student, raw_pin)
            if pin_error is not None:
                return pin_error

            if idempotency_key:
                existing_order = CanteenOrder.objects.filter(
                    idempotency_key=idempotency_key
                ).first()
                if existing_order is not None:
                    if existing_order.student_id == student.id:
                        return JsonResponse(
                            {
                                "status": "APPROVED",
                                "order_id": str(existing_order.id),
                                "order_ref": str(existing_order.id)[:8].upper(),
                                "student_name": student.full_name,
                                "student_reg_no": student.student_reg_no,
                                "amount": str(existing_order.amount),
                                "items_note": existing_order.items_note,
                                "new_balance": str(student.wallet_balance),
                                "duplicate": True,
                            },
                            status=200,
                        )
                    return JsonResponse(
                        {"error": "Duplicate transaction key."}, status=409
                    )

            recent_cutoff = timezone.now() - timedelta(seconds=DOUBLE_TAP_WINDOW_SECONDS)
            if CanteenOrder.objects.filter(
                student=student, amount=amount, created_at__gte=recent_cutoff
            ).exists():
                return JsonResponse(
                    {
                        "error": (
                            "Duplicate purchase detected. Wait 10 seconds before "
                            "charging the same amount again."
                        )
                    },
                    status=409,
                )

            if student.wallet_balance < amount:
                return JsonResponse(
                    {
                        "error": "Insufficient wallet balance.",
                        "current_balance": str(student.wallet_balance),
                    },
                    status=400,
                )

            today = timezone.localdate()
            spent_today = _student_net_spent_today(student, today)
            daily_limit = student.effective_daily_spend_limit()
            if (spent_today + amount) > daily_limit:
                remaining = daily_limit - spent_today
                return JsonResponse(
                    {
                        "error": "Daily spending limit exceeded.",
                        "remaining_daily_limit": str(max(remaining, Decimal("0.00"))),
                    },
                    status=400,
                )

            student.wallet_balance -= amount
            student.save(update_fields=["wallet_balance"])

            order = CanteenOrder.objects.create(
                student=student,
                attendant=request.user,
                idempotency_key=idempotency_key,
                amount=amount,
                items_note=items_note,
            )

            parent_phone = student.parent_phone
            student_name = student.full_name
            amount_str = str(order.amount)
            items_copy = order.items_note
            new_balance_str = str(student.wallet_balance)
            order_ref = str(order.id)[:8].upper()
            transaction.on_commit(
                lambda: send_canteen_purchase_sms(
                    parent_phone=parent_phone,
                    student_name=student_name,
                    amount_spent=amount_str,
                    items_note=items_copy,
                    new_balance=new_balance_str,
                    order_ref=order_ref,
                )
            )

        return JsonResponse(
            {
                "status": "APPROVED",
                "order_id": str(order.id),
                "order_ref": str(order.id)[:8].upper(),
                "student_name": student.full_name,
                "student_reg_no": student.student_reg_no,
                "amount": str(order.amount),
                "items_note": order.items_note,
                "new_balance": str(student.wallet_balance),
            },
            status=200,
        )

    except Student.DoesNotExist:
        return JsonResponse({"error": "Student ID not found or inactive."}, status=404)
    except IntegrityError:
        return JsonResponse({"error": "Duplicate purchase submission."}, status=409)
    except Exception:
        logger.exception("Unexpected error during canteen_checkout for %s", reg_no)
        return JsonResponse(
            {"error": "Unable to complete checkout right now. Please try again."},
            status=500,
        )


@require_POST
def initiate_parent_topup(request):
    """
    Triggers a Flutterwave Split-Payment Mobile Money prompt on the parent's phone.
    Direct unsplit MTN/Airtel fallback routes are disabled so funds always route
    to a school's verified Flutterwave subaccount.
    """
    if settings.LIVE_PAYMENTS_ENABLED and _is_placeholder_flw_key(
        settings.FLW_SECRET_KEY
    ):
        return JsonResponse(
            {"error": "Online payments are temporarily unavailable."},
            status=503,
        )

    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request payload."}, status=400)

    reg_no = str(data.get("student_reg_no", "")).strip()
    raw_phone = str(data.get("payer_phone", "")).strip()

    if not reg_no or not raw_phone:
        return JsonResponse(
            {"error": "CanteenPay code and Mobile Money number are required."},
            status=400,
        )

    try:
        amount = Decimal(str(data.get("amount", "0")))
    except (InvalidOperation, ValueError, TypeError):
        return JsonResponse({"error": "Invalid top-up amount."}, status=400)

    if amount < Decimal("1000"):
        return JsonResponse(
            {"error": "Minimum pocket money top-up is UGX 1,000."}, status=400
        )
    if amount != amount.to_integral_value():
        return JsonResponse(
            {"error": "Enter a whole UGX top-up amount (no decimals)."}, status=400
        )

    try:
        student = Student.objects.select_related("school").get(
            student_reg_no__iexact=reg_no, is_active=True
        )

        subaccount_id = (student.school.subaccount_id or "").strip()
        if not subaccount_id:
            return JsonResponse(
                {"error": "This school is not yet accepting online pocket money."},
                status=400,
            )

        payer_phone, phone_error = _validate_ug_phone(raw_phone)
        if phone_error:
            return JsonResponse({"error": phone_error}, status=400)
        platform_fee = Decimal("500.00")
        momo_ref_id = uuid.uuid4()

        topup = WalletTopUp.objects.create(
            student=student,
            momo_reference_id=momo_ref_id,
            payer_phone=payer_phone,
            amount=amount,
            platform_fee=platform_fee,
            status="PENDING",
        )

        return_url = request.build_absolute_uri(f"/?ref={momo_ref_id}")
        flw_res = initiate_ug_mobile_money_split(
            tx_ref=str(momo_ref_id),
            payer_phone=payer_phone,
            student_amount=int(amount),
            platform_fee=int(platform_fee),
            school_subaccount_id=subaccount_id,
            student_name=student.full_name,
            redirect_url=return_url,
        )

        if flw_res.get("status") == "success":
            redirect_url = (
                flw_res.get("meta", {})
                .get("authorization", {})
                .get("redirect")
            )
            return JsonResponse(
                {
                    "status": "PROMPT_SENT",
                    "message": (
                        f"Click the authorization button below to complete your "
                        f"UGX {int(amount):,} top-up for {student.school.name} "
                        f"(+UGX {int(platform_fee)} fee)."
                    ),
                    "redirect_url": redirect_url,
                    "reference_id": str(momo_ref_id),
                    "student_name": student.full_name,
                    "school": student.school.name,
                },
                status=202,
            )

        topup.status = "FAILED"
        topup.save(update_fields=["status"])
        logger.error("Flutterwave rejected top-up %s (reg_no=%s): %s", momo_ref_id, reg_no, flw_res)

        # Try message at top level, then nested under data (Flutterwave sometimes nests it)
        gateway_msg = (
            str(flw_res.get("message") or "").strip()
            or str((flw_res.get("data") or {}).get("message") or "").strip()
        )
        if (
            gateway_msg
            and not gateway_msg.startswith("Flutterwave connection error")
            and not gateway_msg.startswith("Flutterwave returned non-JSON")
        ):
            user_err = f"Payment gateway: {gateway_msg}"
        else:
            # Surface the raw status/message for diagnosis when no clean message is found
            raw_status = flw_res.get("status", "unknown")
            raw_msg = str(flw_res)[:300]
            user_err = f"Payment gateway error ({raw_status}): {raw_msg}"
        return JsonResponse({"error": user_err}, status=502)

    except Student.DoesNotExist:
        return JsonResponse(
            {"error": "Student registration number not found."}, status=404
        )
    except Exception:
        logger.exception("Unexpected error in initiate_parent_topup for %s", reg_no)
        return JsonResponse(
            {"error": "Unable to start payment right now. Please try again."},
            status=500,
        )


def _topup_receipt_payload(topup: WalletTopUp, student: Student, message: str) -> dict:
    """Shared digital receipt payload returned for credited parent top-ups."""
    return {
        "status": "SUCCESSFUL",
        "message": message,
        "new_balance": str(student.wallet_balance),
        "reference_id": str(topup.momo_reference_id),
        "short_ref": str(topup.momo_reference_id)[:8].upper(),
        "student_name": student.full_name,
        "class_stream": student.class_stream,
        "student_reg_no": student.student_reg_no,
        "school": student.school.name,
        "amount": str(topup.amount),
        "platform_fee": str(topup.platform_fee),
        "total_paid": str(topup.amount + topup.platform_fee),
        "paid_at": timezone.localtime(topup.created_at).strftime("%d %b %Y, %H:%M"),
    }


@require_POST
def verify_parent_topup(request):
    """
    Called when the parent clicks 'Verify & Credit Wallet' (or during auto-poll)
    to verify the transaction server-side with Flutterwave and credit the wallet.
    """
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request payload."}, status=400)

    reference_id = str(data.get("reference_id", "")).strip()
    if not reference_id:
        return JsonResponse({"error": "Transaction reference is required."}, status=400)

    try:
        outcome, topup, student = verify_and_credit_flw_topup(reference_id)

        if outcome == "ALREADY_PROCESSED":
            msg = f"Confirmed! UGX {int(topup.amount):,} credited to {student.full_name}."
            return JsonResponse(_topup_receipt_payload(topup, student, msg), status=200)

        if outcome == "CREDITED":
            msg = f"Payment Verified! UGX {int(topup.amount):,} added to {student.full_name}'s wallet."
            return JsonResponse(_topup_receipt_payload(topup, student, msg), status=200)

        if outcome in ("FAILED", "NOT_PENDING"):
            return JsonResponse(
                {
                    "status": "FAILED",
                    "error": "Payment was not completed on Mobile Money.",
                },
                status=400,
            )

        return JsonResponse(
            {
                "status": "PENDING",
                "message": "Payment is still awaiting PIN/OTP approval on Flutterwave.",
            },
            status=202,
        )

    except (WalletTopUp.DoesNotExist, ValidationError, ValueError):
        return JsonResponse({"error": "Transaction reference not found."}, status=404)
    except Exception:
        logger.exception("Unexpected error verifying top-up %s", reference_id)
        return JsonResponse(
            {"error": "Unable to verify payment right now. Please try again."},
            status=500,
        )


@login_required(login_url="/staff/login/")
@require_POST
def check_student_balance(request):
    """
    Allows an authenticated canteen attendant to check a student's balance
    and remaining daily limit after the student enters their 4-digit PIN.
    """
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request payload."}, status=400)

    reg_no = str(data.get("student_reg_no", "")).strip()
    raw_pin = str(data.get("pin", "")).strip()
    if not reg_no:
        return JsonResponse({"error": "CanteenPay code is required."}, status=400)

    try:
        with transaction.atomic():
            student = (
                Student.objects.select_for_update()
                .select_related("school")
                .get(student_reg_no__iexact=reg_no, is_active=True)
            )

            pin_error = _verify_pin_or_lockout(student, raw_pin)
            if pin_error is not None:
                return pin_error

            today = timezone.localdate()
            spent_today = _student_net_spent_today(student, today)
            remaining_daily = max(
                student.effective_daily_spend_limit() - spent_today, Decimal("0.00")
            )

        return JsonResponse(
            {
                "student_name": student.full_name,
                "school": student.school.name,
                "wallet_balance": str(student.wallet_balance),
                "remaining_daily_limit": str(remaining_daily),
                "eligible": student.wallet_balance > 0 and remaining_daily > 0,
            },
            status=200,
        )

    except Student.DoesNotExist:
        return JsonResponse({"error": "Student ID not found or inactive."}, status=404)
    except Exception:
        logger.exception("Unexpected error in check_student_balance for %s", reg_no)
        return JsonResponse(
            {"error": "Unable to check balance right now. Please try again."},
            status=500,
        )


@ensure_csrf_cookie
@never_cache
def parent_topup_page(request):
    return render(request, "wallet/parent_topup.html")


@ensure_csrf_cookie
@never_cache
@login_required(login_url="/staff/login/")
def canteen_pos_page(request):
    today = timezone.localdate()
    shift_qs = CanteenOrder.objects.filter(
        attendant=request.user, created_at__date=today
    )
    shift_agg = shift_qs.aggregate(total=Sum("amount"), count=Count("id"))
    recent_orders = [
        {
            "order_ref": str(order.id)[:8].upper(),
            "student_name": order.student.full_name,
            "student_reg_no": order.student.student_reg_no,
            "amount": int(order.amount),
            "items_note": order.items_note,
            "time": timezone.localtime(order.created_at).strftime("%H:%M"),
        }
        for order in shift_qs.select_related("student").order_by("-created_at")[:5]
    ]
    return render(
        request,
        "wallet/canteen_pos.html",
        {
            "shift_total": int(shift_agg["total"] or 0),
            "shift_count": shift_agg["count"] or 0,
            "recent_orders_json": json.dumps(recent_orders),
        },
    )


@require_GET
@never_cache
def pos_manifest(request):
    """Web App Manifest so Android Chrome installs /pos/ as a standalone PWA."""
    manifest = {
        "name": "CanteenPay POS",
        "short_name": "CanteenPay",
        "description": "CanteenPay Uganda QR Scanner POS for School Canteens",
        "id": "/pos/",
        "start_url": "/pos/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#111827",
        "theme_color": "#0f172a",
        "icons": [
            {
                "src": static("wallet/icons/icon-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": static("wallet/icons/icon-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": static("wallet/icons/icon-512-maskable.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
    return JsonResponse(manifest, content_type="application/manifest+json")


@require_GET
@never_cache
def pos_service_worker(request):
    """
    Minimal Service Worker for PWA installability.
    Caches only immutable static POS assets (QR scanner JS + app icons) so
    HTML/CSRF tokens, authentication redirects, and /api/ calls always hit the server.
    """
    precache_urls = json.dumps(
        [
            static("wallet/js/html5-qrcode.min.js"),
            static("wallet/icons/icon-192.png"),
            static("wallet/icons/icon-512.png"),
        ]
    )
    sw_js = f"""const CACHE_NAME = "canteenpay-pos-static-v1";
const PRECACHE_URLS = {precache_urls};

self.addEventListener("install", (event) => {{
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS))
    );
    self.skipWaiting();
}});

self.addEventListener("activate", (event) => {{
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(
                keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
            )
        )
    );
    self.clients.claim();
}});

self.addEventListener("fetch", (event) => {{
    if (event.request.method !== "GET") return;
    const url = new URL(event.request.url);
    if (url.origin !== self.location.origin) return;
    if (!url.pathname.startsWith("/static/wallet/")) return;

    event.respondWith(
        caches.match(event.request).then(
            (cached) =>
                cached ||
                fetch(event.request).then((res) => {{
                    if (res && res.ok) {{
                        const copy = res.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
                    }}
                    return res;
                }})
        )
    );
}});
"""
    return HttpResponse(sw_js, content_type="application/javascript")


@login_required(login_url="/staff/login/")
@require_GET
def pos_student_search(request):
    """
    Logged-in till lookup by wallet code or student name.
    The public parent page stays code-only and does not use this endpoint.
    """
    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})

    students = (
        Student.objects.filter(is_active=True)
        .filter(Q(student_reg_no__icontains=query) | Q(full_name__icontains=query))
        .select_related("school")
        .order_by("full_name")[:15]
    )
    return JsonResponse(
        {
            "results": [
                {
                    "student_reg_no": student.student_reg_no,
                    "full_name": student.full_name,
                    "class_stream": student.class_stream,
                    "school__name": student.school.name,
                }
                for student in students
            ]
        }
    )


def wallet_card_pages(students, page_size=8):
    """A4 sheets of 8 laminated wallet cards. QR payload is the wallet code only."""
    cards = []
    for student in students:
        qr = qrcode.QRCode(box_size=8, border=1)
        qr.add_data(student.student_reg_no)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        cards.append(
            {
                "school_name": student.school.name,
                "full_name": student.full_name,
                "class_stream": student.class_stream,
                "student_reg_no": student.student_reg_no,
                "qr_base64": base64.b64encode(buffer.getvalue()).decode("utf-8"),
            }
        )
    return [cards[index : index + page_size] for index in range(0, len(cards), page_size)]


@staff_member_required(login_url="/staff/login/")
def wallet_cards_page(request):
    students = (
        Student.objects.filter(is_active=True)
        .select_related("school")
        .order_by("school__name", "class_stream", "full_name")
    )
    school_id = (request.GET.get("school") or "").strip()
    if school_id:
        students = students.filter(school_id=school_id)
    return render(
        request,
        "wallet/wallet_cards.html",
        {"pages": wallet_card_pages(students)},
    )


@never_cache
def policies_page(request):
    return render(request, "wallet/policies.html")


def _is_ip_rate_limited(request, prefix: str) -> bool:
    """Shared IP rate-limiter using Django cache across public lookup endpoints."""
    ip = _client_ip(request)
    cache_key = f"{prefix}:{ip}"
    if cache.add(cache_key, 1, timeout=LOOKUP_RATE_WINDOW_SECONDS):
        attempts = 1
    else:
        try:
            attempts = cache.incr(cache_key)
        except ValueError:
            cache.set(cache_key, 1, timeout=LOOKUP_RATE_WINDOW_SECONDS)
            attempts = 1
    return attempts > LOOKUP_RATE_LIMIT


@ensure_csrf_cookie
@never_cache
def parent_balance_page(request):
    return render(request, "wallet/parent_balance.html")


@require_POST
def parent_balance_lookup(request):
    """
    Allows parents (rate-limited, with 5-attempt PIN lockout) to check their child's
    wallet balance, daily spend allowance, and last 5 wallet activities using Code + 4-digit PIN.
    """
    if _is_ip_rate_limited(request, "parent_bal_rl"):
        return JsonResponse(
            {"error": "Too many balance attempts. Please wait a minute."},
            status=429,
        )

    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request payload."}, status=400)

    reg_no = str(data.get("student_reg_no", "")).strip()
    raw_pin = str(data.get("pin", "")).strip()
    if not reg_no or not raw_pin:
        return JsonResponse(
            {"error": "CanteenPay code and 4-digit PIN are required."},
            status=400,
        )

    try:
        with transaction.atomic():
            student = (
                Student.objects.select_for_update()
                .select_related("school")
                .get(student_reg_no__iexact=reg_no, is_active=True)
            )
            pin_error = _verify_pin_or_lockout(student, raw_pin)
            if pin_error is not None:
                return pin_error

            today = timezone.localdate()
            spent_today = _student_net_spent_today(student, today)
            daily_limit = student.effective_daily_spend_limit()
            remaining_daily = max(daily_limit - spent_today, Decimal("0.00"))

        activity = []
        for order in CanteenOrder.objects.filter(student=student).order_by("-created_at")[:5]:
            activity.append(
                (
                    order.created_at,
                    {
                        "kind": "PURCHASE",
                        "title": order.items_note or "Canteen Purchase",
                        "amount": f"-{int(order.amount):,}",
                        "time": timezone.localtime(order.created_at).strftime("%d %b %H:%M"),
                        "ref": str(order.id)[:8].upper(),
                    },
                )
            )
        for topup in WalletTopUp.objects.filter(student=student, status="SUCCESSFUL").order_by("-created_at")[:5]:
            activity.append(
                (
                    topup.created_at,
                    {
                        "kind": "TOPUP",
                        "title": "Mobile Money Top-Up",
                        "amount": f"+{int(topup.amount):,}",
                        "time": timezone.localtime(topup.created_at).strftime("%d %b %H:%M"),
                        "ref": str(topup.momo_reference_id)[:8].upper(),
                    },
                )
            )
        for cash in BursarCashTopUp.objects.filter(student=student).order_by("-created_at")[:5]:
            activity.append(
                (
                    cash.created_at,
                    {
                        "kind": "CASH",
                        "title": cash.receipt_note or "Bursar Cash Deposit",
                        "amount": f"+{int(cash.amount):,}",
                        "time": timezone.localtime(cash.created_at).strftime("%d %b %H:%M"),
                        "ref": f"CASH-{str(cash.id)[:6].upper()}",
                    },
                )
            )
        activity.sort(key=lambda pair: pair[0], reverse=True)

        return JsonResponse(
            {
                "student_name": student.full_name,
                "class_stream": student.class_stream,
                "student_reg_no": student.student_reg_no,
                "school": student.school.name,
                "wallet_balance": str(student.wallet_balance),
                "daily_limit": str(daily_limit),
                "spent_today": str(spent_today),
                "remaining_daily_limit": str(remaining_daily),
                "recent_activity": [item for _, item in activity[:5]],
            },
            status=200,
        )
    except Student.DoesNotExist:
        return JsonResponse({"error": "Student ID not found or inactive."}, status=404)
    except Exception:
        logger.exception("Unexpected error in parent_balance_lookup for %s", reg_no)
        return JsonResponse(
            {"error": "Unable to check balance right now. Please try again."},
            status=500,
        )


UGANDA_DISTRICTS = [
    "Kampala", "Wakiso", "Mukono", "Yumbe", "Arua", "Gulu", "Lira", "Mbale",
    "Jinja", "Mbarara", "Masaka", "Entebbe", "Fort Portal", "Hoima", "Soroti",
    "Tororo", "Kabale", "Kasese", "Iganga", "Koboko", "Moyo", "Adjumani", "Other",
]

STUDENT_COUNT_BANDS = [
    ("150", "Under 200 students"),
    ("350", "200 – 500 students"),
    ("750", "500 – 1,000 students"),
    ("1500", "1,000+ students"),
]


@ensure_csrf_cookie
@never_cache
def school_register_page(request):
    """Public school onboarding / partnership request form (creates SchoolOnboardingRequest)."""
    submitted_ref = None
    form_error = None
    form_data = {}

    if request.method == "POST":
        form_data = {
            "school_name": (request.POST.get("school_name") or "").strip()[:150],
            "district": (request.POST.get("district") or "").strip()[:80],
            "approx_students": (request.POST.get("approx_students") or "350").strip(),
            "contact_name": (request.POST.get("contact_name") or "").strip()[:120],
            "contact_role": (request.POST.get("contact_role") or "School Bursar").strip()[:60],
            "contact_phone": (request.POST.get("contact_phone") or "").strip(),
            "contact_email": (request.POST.get("contact_email") or "").strip(),
            "notes": (request.POST.get("notes") or "").strip()[:1000],
        }
        normalized_phone, phone_err = _validate_ug_phone(form_data["contact_phone"])
        if not form_data["school_name"] or not form_data["district"] or not form_data["contact_name"]:
            form_error = "Please enter the school name, district, and contact person's name."
        elif phone_err:
            form_error = phone_err
        else:
            try:
                approx_num = max(1, int(form_data["approx_students"]))
            except ValueError:
                approx_num = 300
            req_obj = SchoolOnboardingRequest.objects.create(
                school_name=form_data["school_name"],
                district=form_data["district"],
                approx_students=approx_num,
                contact_name=form_data["contact_name"],
                contact_role=form_data["contact_role"],
                contact_phone=normalized_phone,
                contact_email=form_data["contact_email"],
                notes=form_data["notes"],
            )
            submitted_ref = str(req_obj.id)[:8].upper()
            form_data = {}

    return render(
        request,
        "wallet/school_register.html",
        {
            "districts": UGANDA_DISTRICTS,
            "student_bands": STUDENT_COUNT_BANDS,
            "submitted_ref": submitted_ref,
            "form_error": form_error,
            "form_data": form_data,
        },
    )


@require_GET
def lookup_student(request):
    if not request.user.is_authenticated and _is_ip_rate_limited(request, "lookup_rl"):
        return JsonResponse(
            {"error": "Too many lookup attempts. Please wait a minute."},
            status=429,
        )

    reg_no = request.GET.get("reg_no", "").strip()
    try:
        student = Student.objects.select_related("school").get(
            student_reg_no__iexact=reg_no, is_active=True
        )
        return JsonResponse(
            {
                "full_name": student.full_name,
                "class_stream": student.class_stream,
                "school": student.school.name,
            },
            status=200,
        )
    except Student.DoesNotExist:
        return JsonResponse({"error": "Student ID not found."}, status=404)


def _parse_report_date(date_str):
    if date_str:
        try:
            return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            pass
    return timezone.localdate()


def _run_inline_reconciliation(view_name: str):
    """Best-effort reconciliation pass on page load (bounded for free-tier hosting)."""
    try:
        reconcile_pending_topups(max_batch=10)
    except Exception:
        logger.warning("Inline reconcile failed on %s load", view_name, exc_info=True)


def _build_settlement_csv(
    report_date, schools_data, attendant_breakdown, top_items, recent_cash_topups
) -> HttpResponse:
    """Builds a downloadable CSV settlement export for school auditors."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([f"CanteenPay Daily Settlement Report - {report_date:%Y-%m-%d}"])
    writer.writerow([])
    writer.writerow(
        [
            "School Name",
            "Online Top-Ups (UGX)",
            "Online Txn Count",
            "Bursar Cash Top-Ups (UGX)",
            "Cash Receipt Count",
            "Net Canteen Sales (UGX)",
            "Sale Count",
            "Refunds (UGX)",
            "Platform Fees (UGX)",
        ]
    )
    for row in schools_data:
        writer.writerow(
            [
                row["school_name"],
                int(row["online_topups"]),
                row["online_topup_count"],
                int(row["cash_topups"]),
                row["cash_topup_count"],
                int(row["canteen_sales"]),
                row["order_count"],
                int(row["refund_total"]),
                int(row["platform_revenue"]),
            ]
        )

    writer.writerow([])
    writer.writerow(["Per-Attendant Canteen Sales Breakdown"])
    writer.writerow(["Attendant", "Sales Count", "Gross Sales (UGX)"])
    for att in attendant_breakdown:
        writer.writerow([att["username"], att["order_count"], int(att["total_sales"])])

    writer.writerow([])
    writer.writerow(["Top-Selling Canteen Items"])
    writer.writerow(["Item", "Orders", "Gross Total (UGX)"])
    for item in top_items:
        writer.writerow([item["item_name"], item["order_count"], int(item["total_amount"])])

    writer.writerow([])
    writer.writerow(["Bursar Cash-In Log"])
    writer.writerow(
        ["Time", "Code", "Student", "School", "Amount (UGX)", "Receipt Note", "Received By"]
    )
    for cash in recent_cash_topups:
        writer.writerow(
            [
                timezone.localtime(cash.created_at).strftime("%H:%M"),
                cash.student.student_reg_no,
                cash.student.full_name,
                cash.student.school.name,
                int(cash.amount),
                cash.receipt_note or "",
                cash.received_by.username if cash.received_by else "staff",
            ]
        )

    response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="canteenpay-settlement-{report_date:%Y-%m-%d}.csv"'
    )
    return response


def _handle_bursar_refund_post(request, report_date):
    """Processes an audited in-page full order refund from the Bursar Daily Report."""
    order_id = (request.POST.get("order_id") or "").strip()
    reason = (request.POST.get("refund_reason") or "").strip()[:255]
    if not order_id or not reason:
        messages.error(request, "Provide a refund reason before issuing a refund.")
        return redirect(f"{request.path}?date={report_date:%Y-%m-%d}")

    with transaction.atomic():
        order = (
            CanteenOrder.objects.select_for_update()
            .select_related("student")
            .filter(pk=order_id)
            .first()
        )
        if order is None:
            messages.error(request, "Canteen order not found.")
            return redirect(f"{request.path}?date={report_date:%Y-%m-%d}")
        if CanteenRefund.objects.filter(order=order).exists():
            messages.error(request, "This canteen order has already been refunded.")
            return redirect(f"{request.path}?date={report_date:%Y-%m-%d}")

        CanteenRefund.objects.create(
            order=order,
            amount=order.amount,
            reason=reason,
            refunded_by=request.user,
        )
        order.student.refresh_from_db()
    messages.success(
        request,
        (
            f"Refunded UGX {int(order.amount):,} to {order.student.full_name} "
            f"({order.student.student_reg_no}). New Balance: UGX {int(order.student.wallet_balance):,}."
        ),
    )
    return redirect(f"{request.path}?date={report_date:%Y-%m-%d}")


@staff_member_required(login_url="/staff/login/")
def bursar_daily_report(request):
    """
    Displays today's online top-ups, bursar desk cash top-ups, and canteen sales,
    and processes audited physical cash-in deposits or in-page refunds from the bursar desk.
    Runs a lightweight reconciliation pass on PENDING top-ups each GET load.
    """
    report_date = _parse_report_date(
        request.GET.get("date") or request.POST.get("report_date")
    )

    if request.method == "POST":
        if (request.POST.get("action") or "").strip() == "refund_order":
            return _handle_bursar_refund_post(request, report_date)

        reg_no = (request.POST.get("student_reg_no") or "").strip()
        receipt_note = (request.POST.get("receipt_note") or "").strip()[:255]
        raw_amount = (request.POST.get("amount") or "").replace(",", "").strip()

        try:
            amount = Decimal(raw_amount)
        except (InvalidOperation, ValueError):
            amount = Decimal("0")

        if not reg_no or amount <= 0 or amount != amount.to_integral_value():
            messages.error(
                request, "Enter a valid CanteenPay code and a whole UGX cash amount above 0."
            )
            return redirect(f"{request.path}?date={report_date:%Y-%m-%d}")

        with transaction.atomic():
            student = (
                Student.objects.select_for_update()
                .filter(student_reg_no__iexact=reg_no, is_active=True)
                .first()
            )
            if student is None:
                messages.error(request, f"Active student '{reg_no}' was not found.")
                return redirect(f"{request.path}?date={report_date:%Y-%m-%d}")

            recent_cutoff = timezone.now() - timedelta(seconds=DOUBLE_TAP_WINDOW_SECONDS)
            if BursarCashTopUp.objects.filter(
                student=student, amount=amount, created_at__gte=recent_cutoff
            ).exists():
                messages.error(
                    request,
                    "Duplicate cash deposit detected. Wait 10 seconds before recording the same amount again.",
                )
                return redirect(f"{request.path}?date={report_date:%Y-%m-%d}")

            cash_topup = BursarCashTopUp.objects.create(
                student=student,
                amount=amount,
                receipt_note=receipt_note,
                received_by=request.user,
            )
            student.refresh_from_db()
        messages.success(
            request,
            (
                f"Cash top-up recorded: UGX {int(cash_topup.amount):,} credited to "
                f"{student.full_name} ({student.student_reg_no}). "
                f"New Balance: UGX {int(student.wallet_balance):,}."
            ),
        )
        return redirect(f"{request.path}?date={report_date:%Y-%m-%d}")

    _run_inline_reconciliation("bursar_daily_report")

    schools_data = []
    for school in School.objects.all():
        orders_qs = CanteenOrder.objects.filter(
            student__school=school, created_at__date=report_date
        )
        canteen_summary = orders_qs.aggregate(
            total_sales=Sum("amount"), order_count=Count("id")
        )
        refund_total = (
            CanteenRefund.objects.filter(
                order__student__school=school, order__created_at__date=report_date
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )
        gross_sales = canteen_summary["total_sales"] or Decimal("0.00")
        net_sales = max(gross_sales - refund_total, Decimal("0.00"))

        topups_qs = WalletTopUp.objects.filter(
            student__school=school, status="SUCCESSFUL", created_at__date=report_date
        )
        topup_summary = topups_qs.aggregate(
            total_topups=Sum("amount"),
            total_fees=Sum("platform_fee"),
            topup_count=Count("id"),
        )

        cash_qs = BursarCashTopUp.objects.filter(
            student__school=school, created_at__date=report_date
        )
        cash_summary = cash_qs.aggregate(
            total_cash=Sum("amount"), cash_count=Count("id")
        )

        schools_data.append(
            {
                "school_name": school.name,
                "online_topups": topup_summary["total_topups"] or 0,
                "online_topup_count": topup_summary["topup_count"] or 0,
                "cash_topups": cash_summary["total_cash"] or 0,
                "cash_topup_count": cash_summary["cash_count"] or 0,
                "canteen_sales": net_sales,
                "order_count": canteen_summary["order_count"] or 0,
                "refund_total": refund_total,
                "platform_revenue": topup_summary["total_fees"] or 0,
            }
        )

    recent_cash_topups = (
        BursarCashTopUp.objects.filter(created_at__date=report_date)
        .select_related("student", "student__school", "received_by")
        .order_by("-created_at")[:50]
    )
    total_cash_collected = (
        BursarCashTopUp.objects.filter(created_at__date=report_date).aggregate(
            total=Sum("amount")
        )["total"]
        or Decimal("0.00")
    )

    attendant_rows = (
        CanteenOrder.objects.filter(created_at__date=report_date)
        .values("attendant__username")
        .annotate(total_sales=Sum("amount"), order_count=Count("id"))
        .order_by("-total_sales")
    )
    attendant_breakdown = [
        {
            "username": row["attendant__username"] or "Unassigned",
            "total_sales": row["total_sales"] or Decimal("0.00"),
            "order_count": row["order_count"] or 0,
        }
        for row in attendant_rows
    ]

    item_rows = (
        CanteenOrder.objects.filter(created_at__date=report_date)
        .values("items_note")
        .annotate(total_amount=Sum("amount"), order_count=Count("id"))
        .order_by("-total_amount")[:15]
    )
    top_items = [
        {
            "item_name": (row["items_note"] or "").strip() or "Unspecified Item",
            "total_amount": row["total_amount"] or Decimal("0.00"),
            "order_count": row["order_count"] or 0,
        }
        for row in item_rows
    ]

    daily_orders = (
        CanteenOrder.objects.filter(created_at__date=report_date)
        .select_related("student", "student__school", "attendant", "refund")
        .order_by("-created_at")[:50]
    )

    if (request.GET.get("export") or "").strip().lower() == "csv":
        return _build_settlement_csv(
            report_date, schools_data, attendant_breakdown, top_items, recent_cash_topups
        )

    return render(
        request,
        "wallet/bursar_report.html",
        {
            "report_date": report_date,
            "schools_data": schools_data,
            "attendant_breakdown": attendant_breakdown,
            "top_items": top_items,
            "daily_orders": daily_orders,
            "recent_cash_topups": recent_cash_topups,
            "total_cash_collected": total_cash_collected,
        },
    )


@staff_member_required(login_url="/staff/login/")
def daily_topup_noticeboard(request):
    """
    Generates a privacy-safe morning noticeboard list of students whose
    parents topped up their wallets (online or bursar cash) on the selected date.
    Runs a lightweight reconciliation pass each time the page loads so PENDING
    top-ups get credited automatically even on PythonAnywhere free tier.
    """
    _run_inline_reconciliation("daily_topup_noticeboard")
    report_date = _parse_report_date(request.GET.get("date"))

    online_student_ids = WalletTopUp.objects.filter(
        status="SUCCESSFUL", created_at__date=report_date
    ).values_list("student_id", flat=True)

    cash_student_ids = BursarCashTopUp.objects.filter(
        created_at__date=report_date
    ).values_list("student_id", flat=True)

    credited_students = (
        Student.objects.filter(
            Q(id__in=online_student_ids) | Q(id__in=cash_student_ids),
            is_active=True,
        )
        .select_related("school")
        .distinct()
        .order_by("school__name", "full_name")
    )

    return render(
        request,
        "wallet/topup_noticeboard.html",
        {
            "report_date": report_date,
            "credited_students": credited_students,
        },
    )


@require_GET
@never_cache
def healthz(request):
    """Lightweight health-check endpoint verifying app & database readiness."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok", "database": "ok"}, status=200)
    except Exception:
        logger.exception("Health check database query failed")
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)


@csrf_exempt
@require_POST
def aggregator_callback_webhook(request):
    """
    Receives payment notifications from Flutterwave and verifies the transaction
    server-side before crediting the student's wallet.
    """
    signature = request.headers.get("verif-hash") or ""
    expected_hash = settings.FLW_WEBHOOK_HASH or ""
    if not signature or not expected_hash or not hmac.compare_digest(
        signature, expected_hash
    ):
        return JsonResponse({"error": "Unauthorized webhook signature"}, status=401)

    payload = _parse_json_body(request)
    if payload is None:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)

    event_data = payload.get("data") or {}

    if (
        payload.get("event") == "charge.completed"
        and event_data.get("status") == "successful"
    ):
        reference_id = event_data.get("tx_ref")
        if not reference_id:
            return JsonResponse({"error": "Missing transaction reference"}, status=400)

        try:
            outcome, _, _ = verify_and_credit_flw_topup(reference_id)
            if outcome in ("ALREADY_PROCESSED", "NOT_PENDING"):
                return JsonResponse({"message": "Already processed"}, status=200)
            return JsonResponse({"status": "ok"}, status=200)
        except (WalletTopUp.DoesNotExist, ValidationError, ValueError):
            return JsonResponse({"error": "TopUp reference not found"}, status=404)

    return JsonResponse({"status": "ok"}, status=200)
