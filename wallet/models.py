import uuid
from decimal import Decimal
from django.db import models
from django.contrib.auth.hashers import make_password, check_password


class School(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    subaccount_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="Aggregator Split Subaccount ID (e.g., Flutterwave RS_xxxx) so funds go straight to the school.",
    )
    settlement_account_details = models.CharField(
        max_length=200,
        blank=True,
        help_text="School Bank Name & Account Number or Official MoMo Merchant Code",
    )
    momo_merchant_code = models.CharField(max_length=50, blank=True)
    daily_spend_limit = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("5000.00")
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Student(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="students")
    student_reg_no = models.CharField(max_length=50, unique=True)
    full_name = models.CharField(max_length=120)
    parent_phone = models.CharField(max_length=20)
    pin_hash = models.CharField(max_length=255)
    wallet_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    is_active = models.BooleanField(default=True)

    def set_pin(self, raw_pin: str):
        self.pin_hash = make_password(raw_pin)

    def verify_pin(self, raw_pin: str) -> bool:
        return check_password(raw_pin, self.pin_hash)

    def __str__(self):
        return f"{self.full_name} ({self.student_reg_no})"


class WalletTopUp(models.Model):
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("SUCCESSFUL", "Successful"),
        ("FAILED", "Failed"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="topups")
    momo_reference_id = models.UUIDField(unique=True, default=uuid.uuid4)
    payer_phone = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    platform_fee = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.student_reg_no} - UGX {self.amount} ({self.status})"


class CanteenOrder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="orders")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    items_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.full_name} - UGX {self.amount}"
