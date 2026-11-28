import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from wallet import sms_service
from wallet.views import LOOKUP_RATE_LIMIT
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

    @patch("wallet.aggregator_service.send_topup_receipt_sms")
    @patch("wallet.aggregator_service.verify_flw_transaction_by_ref")
    def test_8_duplicate_webhook_idempotency(self, mock_verify, _mock_sms):
        """
        Verifies that crediting is idempotent: calling verify_and_credit_flw_topup
        twice only credits the student once (the legacy /api/momo/callback/ route
        has been removed; the aggregator service is tested directly).
        """
        from wallet.aggregator_service import verify_and_credit_flw_topup

        topup = WalletTopUp.objects.create(
            student=self.student,
            payer_phone="256772111222",
            amount=Decimal("5000.00"),
            platform_fee=Decimal("500.00"),
            status="PENDING",
        )
        mock_verify.return_value = {
            "status": "success",
            "data": {
                "status": "successful",
                "currency": "UGX",
                "amount": 5500,  # student_amount(5000) + platform_fee(500)
                "tx_ref": str(topup.momo_reference_id),
            },
        }

        # First call: should credit
        outcome1, _, _ = verify_and_credit_flw_topup(str(topup.momo_reference_id))
        self.assertEqual(outcome1, "CREDITED")

        # Second call: already processed
        outcome2, _, _ = verify_and_credit_flw_topup(str(topup.momo_reference_id))
        self.assertEqual(outcome2, "ALREADY_PROCESSED")

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
        lookup_data = lookup.json()
        self.assertEqual(
            set(lookup_data.keys()),
            {"full_name", "class_stream", "school"},
        )
        self.assertEqual(lookup_data["class_stream"], "S.2 East")

        parent_page = self.client.get("/").content.decode()
        self.assertIn("data.class_stream", parent_page)
        self.assertIn("${data.full_name}${cls}", parent_page)

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

    @patch("wallet.views.timezone")
    def test_pos_idempotency_double_tap_and_attendant_audit(self, mock_tz):
        from django.utils import timezone as real_tz

        # Use a fixed "now" so the double-tap 10-second window is deterministic
        fixed_now = real_tz.now()
        mock_tz.now.return_value = fixed_now
        mock_tz.localdate.return_value = fixed_now.date()

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
                "report_date": timezone.localdate().strftime("%Y-%m-%d"),
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
        self.assertIn("lookupCashStudent()", html)
        self.assertIn("/api/student/lookup/", html)

        # Cash top-up student must appear on the morning noticeboard without exposing amounts
        notice = self.client.get("/report/noticeboard/")
        self.assertEqual(notice.status_code, 200)
        notice_html = notice.content.decode()
        self.assertIn("STD-2026-001", notice_html)
        self.assertIn("Namukasa Grace", notice_html)
        self.assertNotIn("15000", notice_html)
        self.assertNotIn("5000", notice_html)

    @patch("wallet.aggregator_service.verify_flw_transaction_by_ref")
    def test_legacy_callback_routes_removed_and_inline_reconciliation(self, mock_verify):
        # Legacy MoMo & Airtel unsplit callback routes must remain disabled (404)
        self.assertEqual(
            self.client.post("/api/momo/callback/", data="{}", content_type="application/json").status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/api/airtel/callback/", data="{}", content_type="application/json").status_code,
            404,
        )

        # Inline reconciliation on /report/daily/ and /report/noticeboard/ credits >2m PENDING top-ups
        self.client.login(username="school_bursar", password="StrongPassword123!")
        stale_topup = WalletTopUp.objects.create(
            student=self.student,
            payer_phone="256772111222",
            amount=Decimal("2500.00"),
            platform_fee=Decimal("500.00"),
            status="PENDING",
        )
        WalletTopUp.objects.filter(pk=stale_topup.pk).update(
            created_at=timezone.now() - timedelta(minutes=5)
        )
        mock_verify.return_value = {
            "status": "success",
            "data": {"status": "successful", "currency": "UGX", "amount": 3000},
        }

        report_res = self.client.get("/report/daily/")
        self.assertEqual(report_res.status_code, 200)
        stale_topup.refresh_from_db()
        self.student.refresh_from_db()
        self.assertEqual(stale_topup.status, "SUCCESSFUL")
        self.assertEqual(self.student.wallet_balance, Decimal("12500.00"))

        stale_topup_2 = WalletTopUp.objects.create(
            student=self.student,
            payer_phone="256772111222",
            amount=Decimal("1500.00"),
            platform_fee=Decimal("500.00"),
            status="PENDING",
        )
        WalletTopUp.objects.filter(pk=stale_topup_2.pk).update(
            created_at=timezone.now() - timedelta(minutes=5)
        )
        mock_verify.return_value = {
            "status": "success",
            "data": {"status": "successful", "currency": "UGX", "amount": 2000},
        }

        notice_res = self.client.get("/report/noticeboard/")
        self.assertEqual(notice_res.status_code, 200)
        stale_topup_2.refresh_from_db()
        self.student.refresh_from_db()
        self.assertEqual(stale_topup_2.status, "SUCCESSFUL")
        self.assertEqual(self.student.wallet_balance, Decimal("14000.00"))
        self.assertIn("Namukasa Grace", notice_res.content.decode())

    def test_pos_page_loads_local_qrcode_js(self):
        self.client.login(username="canteen_staff", password="StrongPassword123!")
        res = self.client.get("/pos/")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn("wallet/js/html5-qrcode.min.js", html)
        self.assertNotIn("unpkg.com", html)

    def test_pos_pwa_manifest_service_worker_and_meta_tags(self):
        self.client.login(username="canteen_staff", password="StrongPassword123!")
        pos_res = self.client.get("/pos/")
        self.assertEqual(pos_res.status_code, 200)
        html = pos_res.content.decode()
        self.assertIn('rel="manifest" href="/manifest.json"', html)
        self.assertIn('navigator.serviceWorker', html)
        self.assertIn('"/sw.js"', html)

        manifest_res = self.client.get("/manifest.json")
        self.assertEqual(manifest_res.status_code, 200)
        self.assertEqual(manifest_res["Content-Type"], "application/manifest+json")
        manifest = manifest_res.json()
        self.assertEqual(manifest["name"], "CanteenPay POS")
        self.assertEqual(manifest["start_url"], "/pos/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertTrue(any(i["sizes"] == "192x192" for i in manifest["icons"]))
        self.assertTrue(any(i["sizes"] == "512x512" for i in manifest["icons"]))

        sw_res = self.client.get("/sw.js")
        self.assertEqual(sw_res.status_code, 200)
        self.assertEqual(sw_res["Content-Type"], "application/javascript")
        sw_body = sw_res.content.decode()
        self.assertIn("/static/wallet/", sw_body)
        self.assertIn("html5-qrcode.min.js", sw_body)

    def test_auto_code_allocation_csv_import_cards_noticeboard_and_pin_reset(self):
        school = School.objects.create(name="Yumbe Model", code_prefix="ymb")
        self.assertEqual(school.code_prefix, "YMB")
        s1 = Student(
            school=school,
            full_name="Amina Okello",
            class_stream="S.1 East",
            parent_phone="",
            failed_pin_attempts=4,
            pin_locked_until=timezone.now() + timedelta(minutes=10),
        )
        s1.set_pin("1111")
        s1.save()
        s2 = Student(
            school=school,
            full_name="Peter Drani",
            class_stream="S.4 A",
            parent_phone="",
        )
        s2.set_pin("2222")
        s2.save()
        self.assertEqual(s1.student_reg_no, "YMB-0001")
        self.assertEqual(s2.student_reg_no, "YMB-0002")

        # Parent top-up page stays code-only (no name search)
        parent_res = self.client.get("/")
        self.assertNotIn(b"nameSearch", parent_res.content)
        self.assertNotIn(b"pos/students/search", parent_res.content)

        # Unauthenticated POS search is redirected; authenticated POS search returns student
        self.assertEqual(self.client.get("/api/pos/students/search/?q=Amina").status_code, 302)
        self.client.login(username="canteen_staff", password="StrongPassword123!")
        search_res = self.client.get("/api/pos/students/search/?q=Amina")
        self.assertEqual(search_res.status_code, 200)
        results = search_res.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["student_reg_no"], "YMB-0001")
        self.assertEqual(results[0]["class_stream"], "S.1 East")
        self.assertNotIn("pin", results[0])

        # CSV import updates existing student phone without altering PIN/code, skips invalid row, mints new PIN
        admin_user = User.objects.create_superuser("admin_bursar", "b@example.com", "StrongPassword123!")
        self.client.force_login(admin_user)
        csv_body = (
            "full_name,class_stream,parent_phone,admission_no,pin\n"
            "Amina Okello,S.1 East,0772000111,OLD-1,\n"
            "New Learner,S.3 West,,ADM-9,\n"
            ",S.1 East,0772,ADM-10,\n"
            "Zero Pad,S.2 North,,ADM-11,42\n"
        )
        upload = SimpleUploadedFile("class.csv", csv_body.encode("utf-8"), content_type="text/csv")
        with patch("wallet.admin.send_welcome_pin_sms") as mock_welcome_sms:
            imp_res = self.client.post(
                "/admin/wallet/student/import-csv/",
                {"school": str(school.id), "csv_file": upload},
            )
        self.assertEqual(imp_res.status_code, 200)
        imp_html = imp_res.content.decode()
        self.assertIn("One-time take-home PIN slip", imp_html)
        self.assertIn("Do not file these slips", imp_html)
        self.assertIn("Line 4", imp_html)
        self.assertIn("Yumbe Model", imp_html)
        self.assertIn("New Learner", imp_html)
        self.assertIn("S.3 West", imp_html)
        self.assertIn("YMB-0003", imp_html)
        self.assertIn("YMB-0004", imp_html)
        self.assertIn('<span class="pin">0042</span>', imp_html)
        s4 = Student.objects.get(student_reg_no="YMB-0004")
        self.assertTrue(check_password("0042", s4.pin_hash))
        mock_welcome_sms.assert_not_called()

        s1.refresh_from_db()
        self.assertTrue(check_password("1111", s1.pin_hash))
        self.assertEqual(s1.parent_phone, "0772000111")
        self.assertEqual(s1.student_reg_no, "YMB-0001")

        # Reset PIN & Print Single PIN Slip clears lockout and prints fresh PIN
        with patch("wallet.admin.send_welcome_pin_sms") as mock_reset_sms:
            reset_res = self.client.post(
                "/admin/wallet/student/",
                {
                    "action": "reset_pin_and_print_slip",
                    "_selected_action": [str(s1.pk)],
                },
            )
        self.assertEqual(reset_res.status_code, 200)
        reset_html = reset_res.content.decode()
        self.assertIn("One-time take-home PIN slip", reset_html)
        self.assertIn("Yumbe Model", reset_html)
        self.assertIn("Amina Okello", reset_html)
        self.assertIn("S.1 East", reset_html)
        self.assertIn("YMB-0001", reset_html)
        self.assertRegex(reset_html, r'<span class="pin">\d{4}</span>')
        mock_reset_sms.assert_called_once()
        s1.refresh_from_db()
        self.assertEqual(s1.failed_pin_attempts, 0)
        self.assertIsNone(s1.pin_locked_until)

        # 8-up cards & noticeboard
        cards_res = self.client.get("/report/cards/")
        self.assertEqual(cards_res.status_code, 200)
        cards_html = cards_res.content.decode()
        self.assertIn("YMB-0001", cards_html)
        self.assertIn("S.1 East", cards_html)
        self.assertNotIn("One-time PIN", cards_html)
        self.assertNotIn("school id", cards_html.lower())

        notice_res = self.client.get("/report/noticeboard/")
        self.assertEqual(notice_res.status_code, 200)
        self.assertIn(b"CanteenPay Card", notice_res.content)
        self.assertNotIn(b"School ID", notice_res.content)

    def test_admin_subaccount_lock_and_refund_staff_restrictions(self):
        rf = RequestFactory()
        school_admin = django_admin.site._registry[School]
        student_admin = django_admin.site._registry[Student]
        topup_admin = django_admin.site._registry[WalletTopUp]
        refund_admin = django_admin.site._registry[CanteenRefund]

        # Non-superuser bursar sees subaccount_id as read-only; superuser can edit it
        bursar_req = rf.get("/admin/wallet/school/")
        bursar_req.user = self.bursar
        self.assertIn(
            "subaccount_id",
            school_admin.get_readonly_fields(bursar_req, obj=self.school),
        )
        self.assertIn(
            "code_prefix",
            school_admin.get_readonly_fields(bursar_req, obj=self.school),
        )

        super_user = User.objects.create_superuser("owner", "owner@example.com", "StrongPassword123!")
        super_req = rf.get("/admin/wallet/school/")
        super_req.user = super_user
        self.assertNotIn(
            "subaccount_id",
            school_admin.get_readonly_fields(super_req, obj=self.school),
        )

        # Student wallet_balance stays strictly read-only in StudentAdmin
        self.assertIn("wallet_balance", student_admin.readonly_fields)

        # WalletTopUpAdmin is read-only audit log
        self.assertFalse(topup_admin.has_add_permission(super_req))
        self.assertFalse(topup_admin.has_change_permission(super_req))
        self.assertFalse(topup_admin.has_delete_permission(super_req))

        # Attendant (is_staff=False) cannot add/view CanteenRefund even if given model permissions
        add_refund_perm = Permission.objects.get(codename="add_canteenrefund")
        view_refund_perm = Permission.objects.get(codename="view_canteenrefund")
        self.attendant.user_permissions.add(add_refund_perm, view_refund_perm)
        self.attendant.refresh_from_db()
        attendant_req = rf.get("/admin/wallet/canteenrefund/")
        attendant_req.user = self.attendant
        self.assertFalse(refund_admin.has_module_permission(attendant_req))
        self.assertFalse(refund_admin.has_view_permission(attendant_req))
        self.assertFalse(refund_admin.has_add_permission(attendant_req))

        # Bursar (is_staff=True) with permission can add/view CanteenRefund, never change/delete
        self.bursar.user_permissions.add(add_refund_perm, view_refund_perm)
        self.bursar = User.objects.get(pk=self.bursar.pk)
        bursar_refund_req = rf.get("/admin/wallet/canteenrefund/")
        bursar_refund_req.user = self.bursar
        self.assertTrue(refund_admin.has_view_permission(bursar_refund_req))
        self.assertTrue(refund_admin.has_add_permission(bursar_refund_req))
        self.assertFalse(refund_admin.has_change_permission(bursar_refund_req))
        self.assertFalse(refund_admin.has_delete_permission(bursar_refund_req))

    def test_lookup_student_rate_limited(self):
        cache.clear()
        try:
            for _ in range(20):
                res = self.client.get(
                    "/api/student/lookup/?reg_no=STD-2026-001",
                    HTTP_X_FORWARDED_FOR="198.51.100.77, 10.0.0.1",
                )
                self.assertEqual(res.status_code, 200)
                self.assertEqual(
                    set(res.json().keys()),
                    {"full_name", "class_stream", "school"},
                )

            blocked = self.client.get(
                "/api/student/lookup/?reg_no=STD-2026-001",
                HTTP_X_FORWARDED_FOR="198.51.100.77",
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(
                blocked.json()["error"],
                "Too many lookup attempts. Please wait a minute.",
            )

            # A different IP is still allowed
            other_ip = self.client.get(
                "/api/student/lookup/?reg_no=STD-2026-001",
                HTTP_X_FORWARDED_FOR="203.0.113.5",
            )
            self.assertEqual(other_ip.status_code, 200)
        finally:
            cache.clear()

    def test_pos_confirm_banner_and_bursar_cash_double_submit_guard(self):
        self.client.login(username="canteen_staff", password="StrongPassword123!")
        pos_res = self.client.get("/pos/")
        self.assertEqual(pos_res.status_code, 200)
        pos_html = pos_res.content.decode()
        self.assertIn("confirmChargeBanner", pos_html)
        self.assertIn("Confirm Charge: UGX ", pos_html)

        self.client.login(username="school_bursar", password="StrongPassword123!")
        cash_payload = {
            "student_reg_no": "STD-2026-001",
            "amount": "7000",
            "receipt_note": "Cash #77",
            "report_date": timezone.localdate().strftime("%Y-%m-%d"),
        }
        first = self.client.post("/report/daily/", cash_payload, follow=True)
        second = self.client.post("/report/daily/", cash_payload, follow=True)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn("Duplicate cash deposit detected", second.content.decode())
        self.student.refresh_from_db()
        self.assertEqual(self.student.wallet_balance, Decimal("17000.00"))

    # -------------------------------------------------------------------------
    # Per-student daily spend limit
    # -------------------------------------------------------------------------

    def test_per_student_daily_limit_overrides_school_default(self):
        """Student-level daily_spend_limit overrides school default."""
        self.student.daily_spend_limit = Decimal("2000.00")
        self.student.save(update_fields=["daily_spend_limit"])
        # School default is 5000; student cap is 2000.
        self.client.login(username="canteen_staff", password="StrongPassword123!")

        # UGX 2500 exceeds the student's 2000 cap
        res = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps(
                {"student_reg_no": self.student.student_reg_no, "amount": 2500, "pin": "1234"}
            ),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)
        body = res.json()
        self.assertIn("Daily spending limit exceeded", body["error"])
        self.assertIn("remaining_daily_limit", body)
        self.assertEqual(Decimal(body["remaining_daily_limit"]), Decimal("2000.00"))

        # UGX 1500 is within the 2000 cap — should succeed
        res2 = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps(
                {"student_reg_no": self.student.student_reg_no, "amount": 1500, "pin": "1234"}
            ),
            content_type="application/json",
        )
        self.assertEqual(res2.status_code, 200)
        self.student.refresh_from_db()
        self.assertEqual(self.student.wallet_balance, Decimal("8500.00"))

    # -------------------------------------------------------------------------
    # Whole-UGX enforcement
    # -------------------------------------------------------------------------

    def test_whole_ugx_enforcement_at_checkout(self):
        """Fractional amounts are rejected at POS checkout."""
        self.client.login(username="canteen_staff", password="StrongPassword123!")

        res = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps(
                {"student_reg_no": self.student.student_reg_no, "amount": 1500.50, "pin": "1234"}
            ),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("whole UGX", res.json()["error"])

        # Exact integer amount should succeed
        res2 = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps(
                {"student_reg_no": self.student.student_reg_no, "amount": 1500, "pin": "1234"}
            ),
            content_type="application/json",
        )
        self.assertEqual(res2.status_code, 200)

    @patch("wallet.aggregator_service.requests.post")
    def test_whole_ugx_enforcement_at_topup(self, mock_post):
        """Fractional top-up amounts are rejected before hitting Flutterwave."""
        res = self.client.post(
            "/api/parent/topup/",
            data=json.dumps(
                {
                    "student_reg_no": self.student.student_reg_no,
                    "amount": 10000.50,
                    "payer_phone": "256772000001",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("whole UGX", res.json()["error"])
        mock_post.assert_not_called()

    # -------------------------------------------------------------------------
    # Authenticated staff exempt from rate limit on lookup
    # -------------------------------------------------------------------------

    @override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
    def test_authenticated_staff_not_rate_limited_on_lookup(self):
        """Logged-in staff are never blocked by the lookup rate limiter."""
        cache.clear()
        url = f"/api/student/lookup/?reg_no={self.student.student_reg_no}"

        # Exhaust the anonymous rate limit from a spoofed IP
        for _ in range(LOOKUP_RATE_LIMIT + 2):
            self.client.get(url, HTTP_X_FORWARDED_FOR="10.0.0.1")

        # Anonymous request at the same IP should now be blocked
        anon_res = self.client.get(url, HTTP_X_FORWARDED_FOR="10.0.0.1")
        self.assertEqual(anon_res.status_code, 429)

        # Authenticated staff from the same IP must still get 200
        self.client.login(username="canteen_staff", password="StrongPassword123!")
        staff_res = self.client.get(url, HTTP_X_FORWARDED_FOR="10.0.0.1")
        self.assertEqual(staff_res.status_code, 200)
        body = staff_res.json()
        self.assertEqual(body["full_name"], self.student.full_name)
        self.assertNotIn("wallet_balance", body)

    # -------------------------------------------------------------------------
    # POS error includes remaining_daily_limit in JSON
    # -------------------------------------------------------------------------

    def test_pos_error_shows_remaining_daily_limit_in_json(self):
        """Daily-limit exceeded response carries remaining_daily_limit field."""
        self.client.login(username="canteen_staff", password="StrongPassword123!")

        # Spend exactly at the school limit first
        res = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps(
                {"student_reg_no": self.student.student_reg_no, "amount": 5000, "pin": "1234"}
            ),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)

        # Next purchase must exceed the limit — remaining should be 0
        res2 = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps(
                {"student_reg_no": self.student.student_reg_no, "amount": 100, "pin": "1234"}
            ),
            content_type="application/json",
        )
        self.assertEqual(res2.status_code, 400)
        body = res2.json()
        self.assertIn("Daily spending limit exceeded", body["error"])
        self.assertIn("remaining_daily_limit", body)
        self.assertEqual(Decimal(body["remaining_daily_limit"]), Decimal("0.00"))

    @patch("wallet.aggregator_service.requests.post")
    def test_split_init_rejects_missing_or_placeholder_flw_key(self, mock_post):
        """Missing or placeholder FLW_SECRET_KEY returns a clear error without HTTP calls."""
        from wallet.aggregator_service import initiate_ug_mobile_money_split

        for bad_key in ("", "FLWSECK_TEST-YOUR-SECRET-KEY-HERE", "PLACEHOLDER_KEY"):
            with override_settings(FLW_SECRET_KEY=bad_key):
                res = initiate_ug_mobile_money_split(
                    tx_ref="ref-123",
                    payer_phone="256772000001",
                    student_amount=5000,
                    platform_fee=500,
                    school_subaccount_id="RS_TEST",
                    student_name="Namukasa Grace",
                )
            self.assertEqual(res["status"], "error")
            self.assertEqual(
                res["message"],
                "Flutterwave API key is not configured on the server.",
            )
        mock_post.assert_not_called()

    @override_settings(
        LIVE_PAYMENTS_ENABLED=True,
        FLW_SECRET_KEY="FLWSECK-LIVE-999999-X",
    )
    @patch("wallet.views.initiate_ug_mobile_money_split")
    def test_initiate_topup_logs_error_and_surfaces_gateway_message(self, mock_split):
        """Gateway failure logs via logger.error and returns 502 with gateway message."""
        mock_split.return_value = {
            "status": "error",
            "message": "Invalid subaccount id",
        }
        with self.assertLogs("wallet.views", level="ERROR") as logs:
            res = self.client.post(
                "/api/parent/topup/",
                data=json.dumps({
                    "student_reg_no": self.student.student_reg_no,
                    "amount": 5000,
                    "payer_phone": "0772111222",
                }),
                content_type="application/json",
            )
        self.assertEqual(res.status_code, 502)
        self.assertIn("Invalid subaccount id", res.json()["error"])
        self.assertTrue(any("Invalid subaccount id" in msg for msg in logs.output))

    @patch("wallet.aggregator_service.requests.get")
    @patch("wallet.aggregator_service.requests.post")
    def test_flutterwave_v3_urls_used_for_charge_and_verify(self, mock_post, mock_get):
        """Charge and verification calls always target Flutterwave /v3 endpoints."""
        from wallet.aggregator_service import (
            initiate_ug_mobile_money_split,
            verify_flw_transaction_by_ref,
        )

        mock_post.return_value.json.return_value = {"status": "success"}
        mock_get.return_value.json.return_value = {"status": "success"}

        for base_url in ("https://api.flutterwave.com", "https://api.flutterwave.com/v3/"):
            with override_settings(FLW_BASE_URL=base_url, FLW_SECRET_KEY="FLWSECK-LIVE-999"):
                initiate_ug_mobile_money_split(
                    tx_ref="tx-abc",
                    payer_phone="256772000001",
                    student_amount=5000,
                    platform_fee=500,
                    school_subaccount_id="RS_TEST",
                    student_name="Namukasa Grace",
                )
                verify_flw_transaction_by_ref("tx-abc")

            self.assertEqual(
                mock_post.call_args.args[0],
                "https://api.flutterwave.com/v3/charges?type=mobile_money_uganda",
            )
            self.assertEqual(
                mock_get.call_args.args[0],
                "https://api.flutterwave.com/v3/transactions/verify_by_reference?tx_ref=tx-abc",
            )

    @patch("wallet.views.initiate_ug_mobile_money_split")
    def test_phone_validation_rejects_invalid_and_accepts_valid_ug_numbers(self, mock_split):
        """Strict Uganda phone validation rejects short/long/unknown numbers and accepts valid MTN/Airtel formats."""
        mock_split.return_value = {"status": "success", "meta": {"authorization": {"redirect": "https://pay.test/otp"}}}

        # Too short
        res_short = self.client.post(
            "/api/parent/topup/",
            data=json.dumps({"student_reg_no": self.student.student_reg_no, "amount": "5000", "payer_phone": "077123"}),
            content_type="application/json",
        )
        self.assertEqual(res_short.status_code, 400)
        self.assertIn("too short", res_short.json()["error"].lower())

        # Too long
        res_long = self.client.post(
            "/api/parent/topup/",
            data=json.dumps({"student_reg_no": self.student.student_reg_no, "amount": "5000", "payer_phone": "077123456789"}),
            content_type="application/json",
        )
        self.assertEqual(res_long.status_code, 400)
        self.assertIn("too long", res_long.json()["error"].lower())

        # Invalid international length
        res_intl_bad = self.client.post(
            "/api/parent/topup/",
            data=json.dumps({"student_reg_no": self.student.student_reg_no, "amount": "5000", "payer_phone": "25677123"}),
            content_type="application/json",
        )
        self.assertEqual(res_intl_bad.status_code, 400)
        self.assertIn("international format", res_intl_bad.json()["error"].lower())

        # Unrecognised network prefix
        res_bad_net = self.client.post(
            "/api/parent/topup/",
            data=json.dumps({"student_reg_no": self.student.student_reg_no, "amount": "5000", "payer_phone": "0712345678"}),
            content_type="application/json",
        )
        self.assertEqual(res_bad_net.status_code, 400)
        self.assertIn("unrecognised network", res_bad_net.json()["error"].lower())
        mock_split.assert_not_called()

        # Valid local + international formats succeed (202 Accepted) and normalize to 256XXXXXXXXX
        for valid_phone, expected_norm in (
            ("0772123456", "256772123456"),
            ("+256752123456", "256752123456"),
            ("256 782 123 456", "256782123456"),
        ):
            res_ok = self.client.post(
                "/api/parent/topup/",
                data=json.dumps({"student_reg_no": self.student.student_reg_no, "amount": "5000", "payer_phone": valid_phone}),
                content_type="application/json",
            )
            self.assertEqual(res_ok.status_code, 202, f"Expected 202 for {valid_phone}: {res_ok.content}")
            topup = WalletTopUp.objects.get(momo_reference_id=res_ok.json()["reference_id"])
            self.assertEqual(topup.payer_phone, expected_norm)

    def test_parent_and_pos_ui_enhancements_rendered(self):
        """Parent top-up and POS pages render toasts, theme toggle, UGX formatting, and offline guards."""
        parent_html = self.client.get("/").content.decode()
        for snippet in ("toastContainer", "themeToggle", "validateUgPhone", "toLocaleString", "offlineBanner"):
            self.assertIn(snippet, parent_html)

        self.client.login(username="canteen_staff", password="StrongPassword123!")
        pos_html = self.client.get("/pos/").content.decode()
        for snippet in (
            "toastContainer",
            "themeToggle",
            "itemsCounter",
            "startAutoResetCountdown",
            "offlineBanner",
            "sessionBanner",
            "quickSetAmount",
            "quickAddItem",
            "recentSalesList",
            "torchBtn",
        ):
            self.assertIn(snippet, pos_html)

    @patch("wallet.aggregator_service.verify_flw_transaction_by_ref")
    def test_tier1_login_healthz_receipt_pos_shift_and_bursar_csv_export(self, mock_verify):
        """Verifies branded staff login, /healthz/, digital receipt payload, POS shift tracking, and CSV export."""
        # 1. Health check endpoint
        hz = self.client.get("/healthz/")
        self.assertEqual(hz.status_code, 200)
        self.assertEqual(hz.json(), {"status": "ok", "database": "ok"})

        # 2. Branded staff login & redirect to /pos/
        unauth_pos = self.client.get("/pos/")
        self.assertEqual(unauth_pos.status_code, 302)
        self.assertIn("/staff/login/", unauth_pos.url)
        login_page = self.client.get("/staff/login/")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn("Staff &amp; Bursar Sign In", login_page.content.decode())

        login_res = self.client.post(
            "/staff/login/",
            {"username": "canteen_staff", "password": "StrongPassword123!"},
        )
        self.assertRedirects(login_res, "/pos/", fetch_redirect_response=False)

        # 3. POS checkout returns order_ref/amount and /pos/ displays shift summary & recent sale
        chk = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps({
                "student_reg_no": self.student.student_reg_no,
                "amount": 2000,
                "items_note": "Rolex",
                "pin": "1234",
            }),
            content_type="application/json",
        )
        self.assertEqual(chk.status_code, 200)
        chk_data = chk.json()
        self.assertIn("order_ref", chk_data)
        self.assertEqual(Decimal(chk_data["amount"]), Decimal("2000"))

        pos_html = self.client.get("/pos/").content.decode()
        self.assertIn('<span id="shiftTotal">2000</span>', pos_html)
        self.assertIn('<span id="shiftCount">1</span>', pos_html)
        self.assertIn("Rolex", pos_html)

        # 4. Parent top-up verification returns full digital receipt fields & parent UI renders receipt/chips
        parent_html = self.client.get("/").content.decode()
        for snippet in ("receiptCard", "quickPickAmount", "recentStudentBar", "copyReceiptText"):
            self.assertIn(snippet, parent_html)

        topup = WalletTopUp.objects.create(
            student=self.student,
            payer_phone="256772111222",
            amount=Decimal("10000.00"),
            platform_fee=Decimal("500.00"),
            status="PENDING",
        )
        mock_verify.return_value = {
            "status": "success",
            "data": {"status": "successful", "currency": "UGX", "amount": 10500},
        }
        v_res = self.client.post(
            "/api/parent/topup/verify/",
            data=json.dumps({"reference_id": str(topup.momo_reference_id)}),
            content_type="application/json",
        )
        self.assertEqual(v_res.status_code, 200)
        v_data = v_res.json()
        self.assertEqual(v_data["short_ref"], str(topup.momo_reference_id)[:8].upper())
        self.assertEqual(v_data["student_reg_no"], self.student.student_reg_no)
        self.assertEqual(Decimal(v_data["total_paid"]), Decimal("10500.00"))

        # 5. Bursar report attendant breakdown, comma-formatted cash submit, and CSV export
        self.client.login(username="school_bursar", password="StrongPassword123!")
        cash_post = self.client.post(
            "/report/daily/",
            {
                "student_reg_no": self.student.student_reg_no,
                "amount": "20,000",
                "receipt_note": "Term Cash #5",
                "report_date": timezone.localdate().strftime("%Y-%m-%d"),
            },
        )
        self.assertEqual(cash_post.status_code, 302)

        report_html = self.client.get("/report/daily/").content.decode()
        self.assertIn("Per-Attendant Canteen Sales Breakdown", report_html)
        self.assertIn("canteen_staff", report_html)
        self.assertIn("export=csv", report_html)

        csv_res = self.client.get("/report/daily/?export=csv")
        self.assertEqual(csv_res.status_code, 200)
        self.assertIn("text/csv", csv_res["Content-Type"])
        csv_text = csv_res.content.decode()
        self.assertIn("Per-Attendant Canteen Sales Breakdown", csv_text)
        self.assertIn("canteen_staff,1,2000", csv_text)
        self.assertIn("Term Cash #5", csv_text)

        # 6. Policies page includes fixed numbering & theme toggle
        pol_html = self.client.get("/policies/").content.decode()
        self.assertIn("4. Privacy Policy", pol_html)
        self.assertIn("themeToggle", pol_html)

    def test_dropdowns_school_onboarding_password_reset_parent_balance_and_bursar_refund(self):
        """Verifies dropdown presets, school onboarding, password reset, parent mini-statement, and bursar inline refunds."""
        from django.core import mail
        from wallet.models import SchoolOnboardingRequest

        # 1. Standardized dropdowns on Parent & POS pages
        parent_html = self.client.get("/").content.decode()
        for snippet in ("amountPreset", "networkSelect", "/parent/balance/", "/schools/register/"):
            self.assertIn(snippet, parent_html)

        self.client.login(username="canteen_staff", password="StrongPassword123!")
        pos_html = self.client.get("/pos/").content.decode()
        for snippet in ("itemPreset", "amountPreset", "/staff/password-change/"):
            self.assertIn(snippet, pos_html)

        # 2. School onboarding request validates Uganda phone and saves record
        bad_reg = self.client.post(
            "/schools/register/",
            {
                "school_name": "Yumbe High",
                "district": "Yumbe",
                "approx_students": "350",
                "contact_name": "Bursar Ali",
                "contact_role": "School Bursar",
                "contact_phone": "07712",
            },
        )
        self.assertEqual(bad_reg.status_code, 200)
        self.assertIn("too short", bad_reg.content.decode().lower())
        self.assertEqual(SchoolOnboardingRequest.objects.count(), 0)

        ok_reg = self.client.post(
            "/schools/register/",
            {
                "school_name": "Yumbe High",
                "district": "Yumbe",
                "approx_students": "750",
                "contact_name": "Bursar Ali",
                "contact_role": "School Bursar",
                "contact_phone": "0772999888",
                "contact_email": "bursar@yumbehigh.ug",
            },
        )
        self.assertEqual(ok_reg.status_code, 200)
        self.assertIn("Application received", ok_reg.content.decode())
        req_obj = SchoolOnboardingRequest.objects.get(school_name="Yumbe High")
        self.assertEqual(req_obj.contact_phone, "256772999888")

        # 3. Password reset email flow
        self.bursar.email = "bursar@khs.ug"
        self.bursar.save(update_fields=["email"])
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            reset_res = self.client.post("/staff/password-reset/", {"email": "bursar@khs.ug"})
            self.assertRedirects(reset_res, "/staff/password-reset/done/")
            self.assertEqual(len(mail.outbox), 1)
            self.assertIn("/staff/reset/", mail.outbox[0].body)

        # 4. Parent balance & mini-statement lookup (requires valid PIN)
        bal_page = self.client.get("/parent/balance/")
        self.assertEqual(bal_page.status_code, 200)
        bad_pin = self.client.post(
            "/api/parent/balance/",
            data=json.dumps({"student_reg_no": self.student.student_reg_no, "pin": "0000"}),
            content_type="application/json",
        )
        self.assertEqual(bad_pin.status_code, 401)

        order = CanteenOrder.objects.create(
            student=self.student,
            attendant=self.attendant,
            amount=Decimal("1500.00"),
            items_note="Chapati",
        )
        self.student.wallet_balance = Decimal("8500.00")
        self.student.save(update_fields=["wallet_balance"])

        ok_bal = self.client.post(
            "/api/parent/balance/",
            data=json.dumps({"student_reg_no": self.student.student_reg_no, "pin": "1234"}),
            content_type="application/json",
        )
        self.assertEqual(ok_bal.status_code, 200)
        bal_json = ok_bal.json()
        self.assertEqual(Decimal(bal_json["wallet_balance"]), Decimal("8500.00"))
        self.assertTrue(any(a["title"] == "Chapati" for a in bal_json["recent_activity"]))

        # 5. Bursar report Top-Selling Items & in-page audited Refund action
        self.client.login(username="school_bursar", password="StrongPassword123!")
        rep_before = self.client.get("/report/daily/").content.decode()
        self.assertIn("Top-Selling Canteen Items", rep_before)
        self.assertIn("Chapati", rep_before)

        refund_res = self.client.post(
            "/report/daily/",
            {
                "action": "refund_order",
                "order_id": str(order.id),
                "refund_reason": "Wrong scan",
                "report_date": timezone.localdate().strftime("%Y-%m-%d"),
            },
            follow=True,
        )
        self.assertEqual(refund_res.status_code, 200)
        self.assertIn("Refunded UGX 1,500", refund_res.content.decode())
        self.student.refresh_from_db()
        self.assertEqual(self.student.wallet_balance, Decimal("10000.00"))
        self.assertTrue(CanteenRefund.objects.filter(order=order, refunded_by=self.bursar).exists())


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

    @patch("wallet.models.send_refund_receipt_sms")
    @patch("wallet.models.send_cash_topup_receipt_sms")
    @patch("wallet.views.send_canteen_purchase_sms")
    def test_on_commit_sms_for_sale_cash_deposit_and_refund(
        self, mock_sale_sms, mock_cash_sms, mock_refund_sms
    ):
        school = School.objects.create(
            name="Yumbe School",
            code_prefix="YMB",
            subaccount_id="RS_123",
            daily_spend_limit=Decimal("10000.00"),
        )
        attendant = User.objects.create_user(
            username="pos_user", password="StrongPassword123!"
        )
        bursar = User.objects.create_user(
            username="bursar_user", password="StrongPassword123!", is_staff=True
        )
        student = Student.objects.create(
            school=school,
            full_name="Amina Okello",
            class_stream="S.2 A",
            parent_phone="0772111222",
            wallet_balance=Decimal("0.00"),
        )
        student.set_pin("1234")
        student.save()

        # 1. Bursar Cash Deposit SMS
        self.client.login(username="bursar_user", password="StrongPassword123!")
        cash_res = self.client.post(
            "/report/daily/",
            {
                "student_reg_no": student.student_reg_no,
                "amount": "8000",
                "receipt_note": "Visiting Day #15",
            },
        )
        self.assertEqual(cash_res.status_code, 302)
        mock_cash_sms.assert_called_once()
        cash_kwargs = mock_cash_sms.call_args.kwargs
        self.assertEqual(cash_kwargs["student_name"], "Amina Okello")
        self.assertEqual(Decimal(cash_kwargs["amount_deposited"]), Decimal("8000"))
        self.assertEqual(cash_kwargs["receipt_note"], "Visiting Day #15")
        self.assertEqual(Decimal(cash_kwargs["new_balance"]), Decimal("8000"))
        self.assertEqual(cash_kwargs["recorded_by"], "bursar_user")

        # 2. Canteen Purchase SMS
        self.client.login(username="pos_user", password="StrongPassword123!")
        sale_res = self.client.post(
            "/api/canteen/checkout/",
            data=json.dumps({
                "student_reg_no": student.student_reg_no,
                "amount": 2500,
                "items_note": "Chapati + Soda",
                "pin": "1234",
            }),
            content_type="application/json",
        )
        self.assertEqual(sale_res.status_code, 200)
        mock_sale_sms.assert_called_once()
        sale_kwargs = mock_sale_sms.call_args.kwargs
        self.assertEqual(sale_kwargs["student_name"], "Amina Okello")
        self.assertEqual(Decimal(sale_kwargs["amount_spent"]), Decimal("2500"))
        self.assertEqual(sale_kwargs["items_note"], "Chapati + Soda")
        self.assertEqual(Decimal(sale_kwargs["new_balance"]), Decimal("5500"))
        order = CanteenOrder.objects.get(student=student)
        self.assertEqual(sale_kwargs["order_ref"], str(order.id)[:8].upper())

        # 3. Canteen Refund SMS
        CanteenRefund.objects.create(
            order=order,
            amount=Decimal("2500.00"),
            reason="Soda out of stock",
            refunded_by=bursar,
        )
        mock_refund_sms.assert_called_once()
        ref_kwargs = mock_refund_sms.call_args.kwargs
        self.assertEqual(ref_kwargs["student_name"], "Amina Okello")
        self.assertEqual(Decimal(ref_kwargs["amount_refunded"]), Decimal("2500"))
        self.assertEqual(ref_kwargs["reason"], "Soda out of stock")
        self.assertEqual(Decimal(ref_kwargs["new_balance"]), Decimal("8000"))
        self.assertEqual(ref_kwargs["order_ref"], str(order.id)[:8].upper())

        # 4. Placeholder/blank phone quietly skips all new SMS helpers
        no_phone_student = Student.objects.create(
            school=school,
            full_name="Peter Drani",
            parent_phone="N/A",
            wallet_balance=Decimal("5000.00"),
        )
        no_phone_student.set_pin("1234")
        no_phone_student.save()
        self.assertIsNone(
            sms_service.send_canteen_purchase_sms("N/A", "Peter Drani", "1000", "Tea", "4000", "ORD1")
        )
        self.assertIsNone(
            sms_service.send_cash_topup_receipt_sms("", "Peter Drani", "5000", "Cash", "9000", "bursar")
        )
        self.assertIsNone(
            sms_service.send_refund_receipt_sms("-", "Peter Drani", "1000", "5000", "Mistake", "ORD1")
        )