import uuid
import json
from decimal import Decimal
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.db.models import Sum, Count
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET
from .models import School, Student, WalletTopUp, CanteenOrder
from .momo_service import send_request_to_pay
from .sms_service import send_topup_receipt_sms
from .airtel_service import send_airtel_ussd_push
from .aggregator_service import initiate_ug_mobile_money_split, verify_flw_transaction_by_ref


def normalize_ug_phone(phone: str) -> str:
    """
    Converts local formats like 0772123456 or +256772123456 into 256772123456.
    """
    cleaned = phone.strip().replace("+", "").replace(" ", "")
    if cleaned.startswith("0") and len(cleaned) == 10:
        return "256" + cleaned[1:]
    return cleaned


@login_required(login_url="/admin/login/")
@csrf_exempt
@require_POST
def canteen_checkout(request):
    """
    Called by the canteen tablet after scanning the student's QR code
    and entering the purchase amount + student's 4-digit PIN.
    """
    try:
        data = json.loads(request.body)
        reg_no = data.get("student_reg_no", "").strip()
        raw_pin = str(data.get("pin", "")).strip()
        amount = Decimal(str(data.get("amount", "0")))
        items_note = data.get("items_note", "")

        if amount <= 0:
            return JsonResponse({"error": "Invalid purchase amount."}, status=400)

        with transaction.atomic():
            # Lock the student row until this transaction finishes
            student = (
                Student.objects.select_for_update()
                .select_related("school")
                .get(student_reg_no=reg_no, is_active=True)
            )

            # 1. Verify 4-digit PIN
            if not student.verify_pin(raw_pin):
                return JsonResponse({"error": "Incorrect student PIN."}, status=401)

            # 2. Check wallet balance
            if student.wallet_balance < amount:
                return JsonResponse(
                    {
                        "error": "Insufficient wallet balance.",
                        "current_balance": str(student.wallet_balance),
                    },
                    status=400,
                )

            # 3. Check school's daily spending limit
            today = timezone.now().date()
            spent_today = (
                CanteenOrder.objects.filter(
                    student=student, created_at__date=today
                ).aggregate(total=Sum("amount"))["total"]
                or Decimal("0.00")
            )

            if (spent_today + amount) > student.school.daily_spend_limit:
                remaining = student.school.daily_spend_limit - spent_today
                return JsonResponse(
                    {
                        "error": "Daily spending limit exceeded.",
                        "remaining_daily_limit": str(max(remaining, Decimal("0.00"))),
                    },
                    status=400,
                )

            # 4. Deduct balance & record canteen receipt
            student.wallet_balance -= amount
            student.save(update_fields=["wallet_balance"])

            order = CanteenOrder.objects.create(
                student=student, amount=amount, items_note=items_note
            )

        return JsonResponse(
            {
                "status": "APPROVED",
                "order_id": str(order.id),
                "student_name": student.full_name,
                "new_balance": str(student.wallet_balance),
            },
            status=200,
        )

    except Student.DoesNotExist:
        return JsonResponse({"error": "Student ID not found or inactive."}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_POST
def momo_callback_webhook(request):
    """
    X-Callback-Url endpoint hit automatically by MTN MoMo when a parent
    completes or cancels a pocket-money top-up on their phone.
    """
    try:
        payload = json.loads(request.body)
        print("MoMo Callback Payload:", payload)  # DEBUG
        reference_id = payload.get("externalId") or payload.get("referenceId")
        momo_status = payload.get("status")  # "SUCCESSFUL" or "FAILED"

        with transaction.atomic():
            topup = WalletTopUp.objects.select_for_update().get(
                momo_reference_id=reference_id
            )

            # Idempotency check: ignore duplicate callbacks if already processed
            if topup.status != "PENDING":
                return JsonResponse({"message": "Already processed"}, status=200)

            if momo_status == "SUCCESSFUL":
                topup.status = "SUCCESSFUL"
                topup.save(update_fields=["status"])

                student = Student.objects.select_for_update().get(id=topup.student_id)
                student.wallet_balance += topup.amount
                student.save(update_fields=["wallet_balance"])

                # Send instant SMS receipt to parent
                send_topup_receipt_sms(
                    parent_phone=student.parent_phone,
                    student_name=student.full_name,
                    amount_credited=str(topup.amount),
                    new_balance=str(student.wallet_balance),
                )
            else:
                topup.status = "FAILED"
                topup.save(update_fields=["status"])

        return JsonResponse({"message": "Callback processed"}, status=200)

    except WalletTopUp.DoesNotExist:
        return JsonResponse({"error": "TopUp reference not found"}, status=404)


@csrf_exempt
@require_POST
def initiate_parent_topup(request):
    """
    Triggers a Mobile Money prompt on the parent's phone.
    Automatically uses Flutterwave Split-Payment when the school has a
    subaccount_id configured so funds settle directly to the school's bank.
    """
    try:
        data = json.loads(request.body)
        reg_no = data.get("student_reg_no", "").strip()
        raw_phone = data.get("payer_phone", "")
        amount = Decimal(str(data.get("amount", "0")))

        if amount < Decimal("1000"):
            return JsonResponse(
                {"error": "Minimum pocket money top-up is UGX 1,000."}, status=400
            )

        student = Student.objects.select_related("school").get(
            student_reg_no__iexact=reg_no, is_active=True
        )

        payer_phone = normalize_ug_phone(raw_phone)
        platform_fee = Decimal("500.00")
        total_charge = amount + platform_fee

        momo_ref_id = uuid.uuid4()
        topup = WalletTopUp.objects.create(
            student=student,
            momo_reference_id=momo_ref_id,
            payer_phone=payer_phone,
            amount=amount,
            platform_fee=platform_fee,
            status="PENDING",
        )

        # ------------------------------------------------------------------
        # ROUTE 1: Flutterwave Direct-to-School Split Payment (Recommended)
        # ------------------------------------------------------------------
        if student.school.subaccount_id:
            flw_res = initiate_ug_mobile_money_split(
                tx_ref=str(momo_ref_id),
                payer_phone=payer_phone,
                student_amount=int(amount),
                platform_fee=int(platform_fee),
                school_subaccount_id=student.school.subaccount_id,
                student_name=student.full_name,
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
                        "message": f"Click the authorization button below to complete your UGX {int(amount):,} top-up for {student.school.name} (+UGX {int(platform_fee)} fee).",
                        "redirect_url": redirect_url,
                        "reference_id": str(momo_ref_id),
                        "student_name": student.full_name,
                        "school": student.school.name,
                    },
                    status=202,
                )
            else:
                topup.status = "FAILED"
                topup.save(update_fields=["status"])
                return JsonResponse(
                    {"error": flw_res.get("message", "Flutterwave gateway rejected request.")},
                    status=502,
                )

        # ------------------------------------------------------------------
        # ROUTE 2: Fallback Direct MTN / Airtel API (If no Subaccount ID set)
        # ------------------------------------------------------------------
        is_airtel = payer_phone.startswith(("25670", "25675", "25674"))
        if is_airtel:
            prompt_sent = send_airtel_ussd_push(
                reference_id=str(momo_ref_id),
                payer_phone=payer_phone,
                total_amount=str(int(total_charge)),
                student_reg_no=student.student_reg_no,
            )
            gateway_msg = "Airtel Money gateway rejected the request."
        else:
            status_code, mtn_response = send_request_to_pay(
                reference_id=str(momo_ref_id),
                payer_phone=payer_phone,
                total_amount=str(int(total_charge)),
                external_id=str(momo_ref_id),
                student_name=student.full_name,
            )
            prompt_sent = status_code == 202
            gateway_msg = f"MTN Sandbox returned {status_code}: {mtn_response}"

        if prompt_sent:
            return JsonResponse(
                {
                    "status": "PROMPT_SENT",
                    "message": f"Please enter your Mobile Money PIN on {payer_phone} to approve UGX {int(total_charge):,}.",
                    "reference_id": str(momo_ref_id),
                    "student_name": student.full_name,
                    "school": student.school.name,
                },
                status=202,
            )
        else:
            topup.status = "FAILED"
            topup.save(update_fields=["status"])
            return JsonResponse({"error": gateway_msg}, status=502)

    except Student.DoesNotExist:
        return JsonResponse(
            {"error": "Student registration number not found."}, status=404
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_POST
def verify_parent_topup(request):
    """
    Called when the parent clicks 'Verify & Credit Wallet' to verify
    the transaction directly with Flutterwave and credit the student's wallet.
    """
    try:
        data = json.loads(request.body)
        reference_id = data.get("reference_id", "").strip()

        with transaction.atomic():
            topup = WalletTopUp.objects.select_for_update().get(
                momo_reference_id=reference_id
            )

            if topup.status == "SUCCESSFUL":
                student = topup.student
                return JsonResponse(
                    {
                        "status": "SUCCESSFUL",
                        "message": f"Confirmed! UGX {int(topup.amount):,} credited to {student.full_name}.",
                        "new_balance": str(student.wallet_balance),
                    },
                    status=200,
                )

            flw_check = verify_flw_transaction_by_ref(str(topup.momo_reference_id))
            tx_data = flw_check.get("data") or {}

            if (
                flw_check.get("status") == "success"
                and tx_data.get("status") == "successful"
                and Decimal(str(tx_data.get("amount", 0))) >= topup.amount
            ):
                topup.status = "SUCCESSFUL"
                topup.save(update_fields=["status"])

                student = Student.objects.select_for_update().get(id=topup.student_id)
                student.wallet_balance += topup.amount
                student.save(update_fields=["wallet_balance"])

                send_topup_receipt_sms(
                    parent_phone=student.parent_phone,
                    student_name=student.full_name,
                    amount_credited=str(topup.amount),
                    new_balance=str(student.wallet_balance),
                )

                return JsonResponse(
                    {
                        "status": "SUCCESSFUL",
                        "message": f"Payment Verified! UGX {int(topup.amount):,} added to {student.full_name}'s wallet.",
                        "new_balance": str(student.wallet_balance),
                    },
                    status=200,
                )

            return JsonResponse(
                {
                    "status": "PENDING",
                    "message": "Payment is still awaiting PIN/OTP approval on Flutterwave.",
                },
                status=202,
            )

    except WalletTopUp.DoesNotExist:
        return JsonResponse({"error": "Transaction reference not found."}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_POST
def check_student_balance(request):
    """
    Allows a student at the canteen counter to scan their ID card
    and enter their 4-digit PIN to check their balance and daily limit.
    """
    try:
        data = json.loads(request.body)
        reg_no = data.get("student_reg_no", "").strip()
        raw_pin = str(data.get("pin", "")).strip()

        student = Student.objects.select_related("school").get(
            student_reg_no__iexact=reg_no, is_active=True
        )

        if not student.verify_pin(raw_pin):
            return JsonResponse({"error": "Incorrect 4-digit PIN."}, status=401)

        today = timezone.now().date()
        spent_today = (
            CanteenOrder.objects.filter(
                student=student, created_at__date=today
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )
        remaining_daily = max(
            student.school.daily_spend_limit - spent_today, Decimal("0.00")
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
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@never_cache
def parent_topup_page(request):
    return render(request, "wallet/parent_topup.html")


@never_cache
@login_required(login_url="/admin/login/")
def canteen_pos_page(request):
    return render(request, "wallet/canteen_pos.html")


@require_GET
def lookup_student(request):
    reg_no = request.GET.get("reg_no", "").strip()
    try:
        student = Student.objects.select_related("school").get(
            student_reg_no__iexact=reg_no, is_active=True
        )
        return JsonResponse(
            {
                "student_reg_no": student.student_reg_no,
                "full_name": student.full_name,
                "school": student.school.name,
                "wallet_balance": str(student.wallet_balance),
            },
            status=200,
        )
    except Student.DoesNotExist:
        return JsonResponse({"error": "Student ID not found."}, status=404)


@csrf_exempt
@require_POST
def airtel_callback_webhook(request):
    """
    Callback endpoint hit by Airtel Money when a parent completes or cancels a top-up.
    """
    try:
        payload = json.loads(request.body)
        print("Airtel Callback Payload:", payload)
        transaction_data = payload.get("transaction", {})
        reference_id = transaction_data.get("id")
        airtel_status = transaction_data.get("status")  # "TS" = success, "TF" = failed

        with transaction.atomic():
            topup = WalletTopUp.objects.select_for_update().get(
                momo_reference_id=reference_id
            )

            if topup.status != "PENDING":
                return JsonResponse({"message": "Already processed"}, status=200)

            if airtel_status == "TS":
                topup.status = "SUCCESSFUL"
                topup.save(update_fields=["status"])

                student = Student.objects.select_for_update().get(id=topup.student_id)
                student.wallet_balance += topup.amount
                student.save(update_fields=["wallet_balance"])

                send_topup_receipt_sms(
                    parent_phone=student.parent_phone,
                    student_name=student.full_name,
                    amount_credited=str(topup.amount),
                    new_balance=str(student.wallet_balance),
                )
            else:
                topup.status = "FAILED"
                topup.save(update_fields=["status"])

        return JsonResponse({"message": "Callback processed"}, status=200)

    except WalletTopUp.DoesNotExist:
        return JsonResponse({"error": "TopUp reference not found"}, status=404)


def bursar_daily_report(request):
    """
    Displays today's canteen sales, parent top-ups, and net school settlement.
    """
    selected_date_str = request.GET.get("date")
    if selected_date_str:
        report_date = timezone.datetime.strptime(selected_date_str, "%Y-%m-%d").date()
    else:
        report_date = timezone.now().date()

    schools_data = []
    for school in School.objects.all():
        orders_qs = CanteenOrder.objects.filter(
            student__school=school, created_at__date=report_date
        )
        canteen_summary = orders_qs.aggregate(
            total_sales=Sum("amount"), order_count=Count("id")
        )

        topups_qs = WalletTopUp.objects.filter(
            student__school=school, status="SUCCESSFUL", created_at__date=report_date
        )
        topup_summary = topups_qs.aggregate(
            total_topups=Sum("amount"),
            total_fees=Sum("platform_fee"),
            topup_count=Count("id"),
        )

        schools_data.append(
            {
                "school_name": school.name,
                "canteen_sales": canteen_summary["total_sales"] or 0,
                "order_count": canteen_summary["order_count"] or 0,
                "parent_topups": topup_summary["total_topups"] or 0,
                "topup_count": topup_summary["topup_count"] or 0,
                "platform_revenue": topup_summary["total_fees"] or 0,
            }
        )

    return render(
        request,
        "wallet/bursar_report.html",
        {"report_date": report_date, "schools_data": schools_data},
    )


@staff_member_required
def daily_topup_noticeboard(request):
    """
    Generates a privacy-safe morning noticeboard list of students whose
    parents topped up their wallets on the selected date (hides exact UGX amounts).
    """
    selected_date_str = request.GET.get("date")
    if selected_date_str:
        report_date = timezone.datetime.strptime(selected_date_str, "%Y-%m-%d").date()
    else:
        report_date = timezone.now().date()

    # Fetch distinct students who received a SUCCESSFUL top-up on report_date
    credited_students = (
        Student.objects.filter(
            topups__status="SUCCESSFUL",
            topups__created_at__date=report_date,
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


@csrf_exempt
@require_POST
def aggregator_callback_webhook(request):
    """
    Receives payment confirmation from Flutterwave after split settlement
    to the school's bank account is triggered.
    """
    signature = request.headers.get("verif-hash")
    if not signature or signature != settings.FLW_WEBHOOK_HASH:
        return JsonResponse({"error": "Unauthorized webhook signature"}, status=401)

    payload = json.loads(request.body)
    event_data = payload.get("data", {})

    if payload.get("event") == "charge.completed" and event_data.get("status") == "successful":
        reference_id = event_data.get("tx_ref")

        try:
            with transaction.atomic():
                topup = WalletTopUp.objects.select_for_update().get(
                    momo_reference_id=reference_id
                )
                if topup.status != "PENDING":
                    return JsonResponse({"message": "Already processed"}, status=200)

                topup.status = "SUCCESSFUL"
                topup.save(update_fields=["status"])

                student = Student.objects.select_for_update().get(id=topup.student_id)
                student.wallet_balance += topup.amount
                student.save(update_fields=["wallet_balance"])

                send_topup_receipt_sms(
                    parent_phone=student.parent_phone,
                    student_name=student.full_name,
                    amount_credited=str(topup.amount),
                    new_balance=str(student.wallet_balance),
                )
        except WalletTopUp.DoesNotExist:
            return JsonResponse({"error": "TopUp reference not found"}, status=404)

    return JsonResponse({"status": "ok"}, status=200)
