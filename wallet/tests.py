import json
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User
from wallet.models import School, Student, WalletTopUp, CanteenOrder


class CanteenPayProductionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.attendant = User.objects.create_user(
            username="canteen_staff", password="StrongPassword123!"
        )
        self.school = School.objects.create(
            name="Kampala High School", daily_spend_limit=Decimal("5000.00")
        )
        self.student = Student.objects.create(
            school=self.school,
            student_reg_no="STD-2026-001",
            full_name="Namukasa Grace",
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

    def test_8_duplicate_webhook_idempotency(self):
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
        # Balance must be 15,000 (not 20,000)
        self.assertEqual(self.student.wallet_balance, Decimal("15000.00"))