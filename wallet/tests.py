import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from wallet import sms_service
from wallet.models import (
    BursarCashTopUp,
    CanteenOrder,
    CanteenRefund,
    School,
    Student,
    WalletTopUp,
)


class CanteenPayProductionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.attendant = User.objects.create_user(
            username="canteen_staff", password="StrongPassword123!"
        )
        self.bursar = User.objects.create_user(
            username="school_bursar",
            password="StrongPassword123!",
            is_staff=True,
        )
        self.school = School.objects.create(
            name="Kampala High School",
            code_prefix="KHS",
            subaccount_id="RS_TEST_SUBACCOUNT",
            daily_spend_limit=Decimal("5000.00"),
        )
        self.student = Student.objects.create(
            school=self.school,
            student_reg_no="STD-2026-001",
            full_name="Namukasa Grace",
            class_stream="S.2 East",
            parent_phone="256772111222",
            wallet_balance=Decimal("10000.00"),
            is_active=True,
        )
        self.student.set_pin("1234")
        self.student.save()

    def test_6_wrong_pin_rejected(self):
        self.client.login(username="canteen_staff", password="StrongPassword123!")
        res = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps({
                "student_reg_no": "STD-2026-001",
                "amount": 1000,
                "pin": "9999",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 401)
        self.student.refresh_from_db()
        self.assertEqual(self.student.wallet_balance, Decimal("10000.00"))
        self.assertEqual(self.student.failed_pin_attempts, 1)

    def test_6_daily_limit_enforced(self):
        self.client.login(username="canteen_staff", password="StrongPassword123!")
        res = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps({
                "student_reg_no": "STD-2026-001",
                "amount": 6000,  # Exceeds 5,000 daily cap
                "pin": "1234",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Daily spending limit", res.json()["error"])

    def test_7_unauthenticated_pos_redirected(self):
        res = self.client.get("/pos/")
        self.assertEqual(res.status_code, 302)  # Redirects to login

    @patch("wallet.views.send_topup_receipt_sms")
    def test_8_duplicate_webhook_idempotency(self, _mock_sms):
        topup = WalletTopUp.objects.create(
            student=self.student,
            payer_phone="256772111222",
            amount=Decimal("5000.00"),
            platform_fee=Decimal("500.00"),
            status="PENDING",
        )
        payload = json.dumps({
            "externalId": str(topup.momo_reference_id),
            "status": "SUCCESSFUL",
        })

        # Send webhook twice
        self.client.post("/api/momo/callback/", data=payload, content_type="application/json")
        res2 = self.client.post("/api/momo/callback/", data=payload, content_type="application/json")

        self.assertEqual(res2.json()["message"], "Already processed")
        self.student.refresh_from_db()
        self.assertEqual(self.student.wallet_balance, Decimal("15000.00"))

    def test_sms_placeholder_phones_skipped(self):
        for bad_phone in [None, "", "   ", "N/A", "n/a", "-", "none", "NONE"]:
            self.assertFalse(sms_service.is_valid_phone(bad_phone))
            self.assertIsNone(
                sms_service.send_topup_receipt_sms(
                    bad_phone, "Grace", "5000", "15000", tx_ref="ABCD1234"
                )
            )

    @override_settings(
        LIVE_PAYMENTS_ENABLED=True,
        FLW_SECRET_KEY="FLWSECK_TEST-YOUR-SECRET-KEY-HERE",
    )
    def test_live_payments_guard_blocks_test_key(self):
        res = self.client.post(
            "/api/parent/topup/",
            data=json.dumps({
                "student_reg_no": "STD-2026-001",
                "amount": 5000,
                "payer_phone": "0772111222",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 503)
        self.assertEqual(
            res.json()["error"], "Online payments are temporarily unavailable."
        )
        self.assertEqual(WalletTopUp.objects.count(), 0)

    def test_subaccount_required_blocks_unsplit_school(self):
        no_sub_school = School.objects.create(
            name="No Subaccount School",
            code_prefix="NSS",
            subaccount_id="",
        )
        other_student = Student.objects.create(
            school=no_sub_school,
            full_name="Okello Peter",
            class_stream="S.1 A",
        )
        other_student.set_pin("4321")
        other_student.save()

        res = self.client.post(
            "/api/parent/topup/",
            data=json.dumps({
                "student_reg_no": other_student.student_reg_no,
                "amount": 5000,
                "payer_phone": "0772111222",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            res.json()["error"],
            "This school is not yet accepting online pocket money.",
        )
        self.assertEqual(WalletTopUp.objects.count(), 0)

    @patch("wallet.aggregator_service.verify_flw_transaction_by_ref")
    def test_verify_requires_ugx_and_full_fee_amount(self, mock_verify):
        topup = WalletTopUp.objects.create(
            student=self.student,
            payer_phone="256772111222",
            amount=Decimal("5000.00"),
            platform_fee=Decimal("500.00"),
            status="PENDING",
        )

        # Wrong currency
        mock_verify.return_value = {
            "status": "success",
            "data": {"status": "successful", "currency": "USD", "amount": 5500},
        }
        res_usd = self.client.post(
            "/api/parent/topup/verify/",
            data=json.dumps({"reference_id": str(topup.momo_reference_id)}),
            content_type="application/json",
        )
        self.assertEqual(res_usd.status_code, 202)

        # Underpaid (missing UGX 500 fee)
        mock_verify.return_value = {
            "status": "success",
            "data": {"status": "successful", "currency": "UGX", "amount": 5000},
        }
        res_short = self.client.post(
            "/api/parent/topup/verify/",
            data=json.dumps({"reference_id": str(topup.momo_reference_id)}),
            content_type="application/json",
        )
        self.assertEqual(res_short.status_code, 202)

        self.student.refresh_from_db()
        topup.refresh_from_db()
        self.assertEqual(topup.status, "PENDING")
        self.assertEqual(self.student.wallet_balance, Decimal("10000.00"))

    @override_settings(FLW_WEBHOOK_HASH="secret-webhook-hash")
    @patch("wallet.aggregator_service.verify_flw_transaction_by_ref")
    def test_aggregator_webhook_verifies_server_side(self, mock_verify):
        topup = WalletTopUp.objects.create(
            student=self.student,
            payer_phone="256772111222",
            amount=Decimal("5000.00"),
            platform_fee=Decimal("500.00"),
            status="PENDING",
        )
        mock_verify.return_value = {
            "status": "success",
            "data": {"status": "successful", "currency": "UGX", "amount": 5500},
        }

        payload = json.dumps({
            "event": "charge.completed",
            "data": {
                "tx_ref": str(topup.momo_reference_id),
                "status": "successful",
            },
        })
        bad_sig = self.client.post(
            "/api/aggregator/callback/",
            data=payload,
            content_type="application/json",
            HTTP_VERIF_HASH="wrong-hash",
        )
        self.assertEqual(bad_sig.status_code, 401)

        ok_res = self.client.post(
            "/api/aggregator/callback/",
            data=payload,
            content_type="application/json",
            HTTP_VERIF_HASH="secret-webhook-hash",
        )
        self.assertEqual(ok_res.status_code, 200)
        self.student.refresh_from_db()
        self.assertEqual(self.student.wallet_balance, Decimal("15000.00"))

    @patch("wallet.aggregator_service.verify_flw_transaction_by_ref")
    def test_reconcile_pending_topups_command(self, mock_verify):
        now = timezone.now()
        old_paid = WalletTopUp.objects.create(
            student=self.student,
            payer_phone="256772111222",
            amount=Decimal("4000.00"),
            platform_fee=Decimal("500.00"),
            status="PENDING",
        )
        old_unpaid = WalletTopUp.objects.create(
            student=self.student,
            payer_phone="256772111222",
            amount=Decimal("3000.00"),
            platform_fee=Decimal("500.00"),
            status="PENDING",
        )
        fresh = WalletTopUp.objects.create(
            student=self.student,
            payer_phone="256772111222",
            amount=Decimal("2000.00"),
            platform_fee=Decimal("500.00"),
            status="PENDING",
        )
        WalletTopUp.objects.filter(pk=old_paid.pk).update(
            created_at=now - timedelta(minutes=5)
        )
        WalletTopUp.objects.filter(pk=old_unpaid.pk).update(
            created_at=now - timedelta(minutes=20)
        )
        WalletTopUp.objects.filter(pk=fresh.pk).update(
            created_at=now - timedelta(seconds=30)
        )

        def fake_verify(ref_id):
            if ref_id == str(old_paid.momo_reference_id):
                return {
                    "status": "success",
                    "data": {"status": "successful", "currency": "UGX", "amount": 4500},
                }
            return {"status": "error", "message": "not found"}

        mock_verify.side_effect = fake_verify
        call_command("reconcile_pending_topups")

        old_paid.refresh_from_db()
        old_unpaid.refresh_from_db()
        fresh.refresh_from_db()
        self.student.refresh_from_db()
        self.assertEqual(old_paid.status, "SUCCESSFUL")
        self.assertEqual(old_unpaid.status, "FAILED")
        self.assertEqual(fresh.status, "PENDING")
        self.assertEqual(self.student.wallet_balance, Decimal("14000.00"))

    def test_public_lookup_strips_balance_and_balance_requires_auth(self):
        lookup = self.client.get("/api/student/lookup/?reg_no=STD-2026-001")
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(
            set(lookup.json().keys()),
            {"full_name", "class_stream", "school"},
        )

        unauth_bal = self.client.post(
            "/api/student/balance/",
            data=json.dumps({"student_reg_no": "STD-2026-001", "pin": "1234"}),
            content_type="application/json",
        )
        self.assertEqual(unauth_bal.status_code, 302)

    def test_shared_pin_lockout_across_checkout_and_balance(self):
        self.client.login(username="canteen_staff", password="StrongPassword123!")

        for _ in range(3):
            res = self.client.post(
                "/api/canteen/checkout/",
                data=json.dumps({
                    "student_reg_no": "STD-2026-001",
                    "amount": 1000,
                    "pin": "0000",
                }),
                content_type="application/json",
            )
            self.assertEqual(res.status_code, 401)

        res4 = self.client.post(
            "/api/student/balance/",
            data=json.dumps({"student_reg_no": "STD-2026-001", "pin": "0000"}),
            content_type="application/json",
        )
        self.assertEqual(res4.status_code, 401)

        res5 = self.client.post(
            "/api/student/balance/",
            data=json.dumps({"student_reg_no": "STD-2026-001", "pin": "0000"}),
            content_type="application/json",
        )
        self.assertEqual(res5.status_code, 423)
        self.assertIn("locked", res5.json()["error"].lower())

        # Correct PIN is also blocked while locked
        res_locked_valid = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps({
                "student_reg_no": "STD-2026-001",
                "amount": 1000,
                "pin": "1234",
            }),
            content_type="application/json",
        )
        self.assertEqual(res_locked_valid.status_code, 423)

        # Once lockout expires, valid PIN works and resets counters
        Student.objects.filter(pk=self.student.pk).update(
            pin_locked_until=timezone.now() - timedelta(minutes=1)
        )
        res_unlocked = self.client.post(
            "/api/student/balance/",
            data=json.dumps({"student_reg_no": "STD-2026-001", "pin": "1234"}),
            content_type="application/json",
        )
        self.assertEqual(res_unlocked.status_code, 200)
        self.student.refresh_from_db()
        self.assertEqual(self.student.failed_pin_attempts, 0)
        self.assertIsNone(self.student.pin_locked_until)

    def test_csrf_enforced_on_browser_post_endpoints(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username="canteen_staff", password="StrongPassword123!")

        for url in [
            "/api/parent/topup/",
            "/api/parent/topup/verify/",
            "/api/canteen/checkout/",
            "/api/student/balance/",
        ]:
            res = csrf_client.post(
                url,
                data=json.dumps({}),
                content_type="application/json",
            )
            self.assertEqual(res.status_code, 403, f"Expected 403 CSRF check on {url}")

        page = csrf_client.get("/pos/")
        self.assertEqual(page.status_code, 200)
        token = csrf_client.cookies["csrftoken"].value
        ok_res = csrf_client.post(
            "/api/student/balance/",
            data=json.dumps({"student_reg_no": "STD-2026-001", "pin": "1234"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(ok_res.status_code, 200)

    def test_pos_idempotency_double_tap_and_attendant_audit(self):
        self.client.login(username="canteen_staff", password="StrongPassword123!")
        payload = {
            "student_reg_no": "STD-2026-001",
            "amount": 1500,
            "items_note": "Chapati",
            "pin": "1234",
            "idempotency_key": "idem-key-1",
        }
        res1 = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(res1.status_code, 200)

        # Retry same idempotency_key -> safe duplicate response, no extra charge
        res2 = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(res2.status_code, 200)
        self.assertTrue(res2.json().get("duplicate"))

        # Different key, same student + amount within 10s -> blocked with 409
        payload["idempotency_key"] = "idem-key-2"
        res3 = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(res3.status_code, 409)

        self.student.refresh_from_db()
        self.assertEqual(self.student.wallet_balance, Decimal("8500.00"))
        order = CanteenOrder.objects.get(idempotency_key="idem-key-1")
        self.assertEqual(order.attendant, self.attendant)

    def test_canteen_refund_restores_balance(self):
        order = CanteenOrder.objects.create(
            student=self.student,
            attendant=self.attendant,
            amount=Decimal("2000.00"),
            items_note="Soda",
        )
        with self.assertRaises(ValidationError):
            CanteenRefund.objects.create(
                order=order,
                amount=Decimal("3000.00"),
                reason="Too much",
                refunded_by=self.bursar,
            )

        CanteenRefund.objects.create(
            order=order,
            amount=Decimal("2000.00"),
            reason="Out of stock",
            refunded_by=self.bursar,
        )
        self.student.refresh_from_db()
        self.assertEqual(self.student.wallet_balance, Decimal("12000.00"))

    def test_bursar_report_auth_and_cash_topup(self):
        anon_res = self.client.get("/report/daily/")
        self.assertEqual(anon_res.status_code, 302)

        self.client.login(username="canteen_staff", password="StrongPassword123!")
        non_staff_res = self.client.get("/report/daily/")
        self.assertEqual(non_staff_res.status_code, 302)

        self.client.login(username="school_bursar", password="StrongPassword123!")
        post_res = self.client.post(
            "/report/daily/",
            {
                "student_reg_no": "STD-2026-001",
                "amount": "5000",
                "receipt_note": "Visiting Day #12",
                "report_date": timezone.now().date().strftime("%Y-%m-%d"),
            },
        )
        self.assertEqual(post_res.status_code, 302)
        self.student.refresh_from_db()
        self.assertEqual(self.student.wallet_balance, Decimal("15000.00"))
        cash = BursarCashTopUp.objects.get(student=self.student)
        self.assertEqual(cash.received_by, self.bursar)

        report = self.client.get("/report/daily/")
        self.assertEqual(report.status_code, 200)
        html = report.content.decode()
        self.assertIn("Online Top-Ups Settled to School Bank", html)
        self.assertIn("Cash Top-Ups Collected at Bursar Desk", html)
        self.assertIn("Canteen Sales (Vendor Payout Due)", html)
        self.assertIn("Visiting Day #12", html)

    def test_pos_page_loads_local_qrcode_js(self):
        self.client.login(username="canteen_staff", password="StrongPassword123!")
        res = self.client.get("/pos/")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn("wallet/js/html5-qrcode.min.js", html)
        self.assertNotIn("unpkg.com", html)


class OnCommitSMSTests(TransactionTestCase):
    @patch("wallet.aggregator_service.send_topup_receipt_sms")
    @patch("wallet.aggregator_service.verify_flw_transaction_by_ref")
    def test_on_commit_sms_fires_with_short_ref(self, mock_verify, mock_sms):
        school = School.objects.create(
            name="Yumbe School",
            code_prefix="YMB",
            subaccount_id="RS_123",
        )
        student = Student.objects.create(
            school=school,
            full_name="Amina",
            parent_phone="0772111222",
            wallet_balance=Decimal("0.00"),
        )
        student.set_pin("1234")
        student.save()
        topup = WalletTopUp.objects.create(
            student=student,
            payer_phone="256772111222",
            amount=Decimal("5000.00"),
            platform_fee=Decimal("500.00"),
            status="PENDING",
        )
        mock_verify.return_value = {
            "status": "success",
            "data": {"status": "successful", "currency": "UGX", "amount": 5500},
        }

        res = self.client.post(
            "/api/parent/topup/verify/",
            data=json.dumps({"reference_id": str(topup.momo_reference_id)}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        mock_sms.assert_called_once()
        kwargs = mock_sms.call_args.kwargs
        self.assertEqual(kwargs["tx_ref"], str(topup.momo_reference_id)[:8].upper())
