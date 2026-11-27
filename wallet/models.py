import uuid
from decimal import Decimal
from django.db import models, transaction
from django.contrib.auth.hashers import make_password, check_password


class School(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    code_prefix = models.CharField(
        max_length=5,
        unique=True,
        help_text="Short 3-letter school prefix, e.g. YMB or ARG",
    )
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

    def save(self, *args, **kwargs):
        self.code_prefix = (self.code_prefix or "").strip().upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


def allocate_student_reg_no(school):
    """
    Next globally unique code for this school: PREFIX-0001, PREFIX-0002, ...
    The school row is locked so two imports cannot issue the same serial.
    """
    prefix = (school.code_prefix or "").strip().upper()
    if not prefix:
        raise ValueError("Set the school code prefix before issuing CanteenPay codes.")

    with transaction.atomic():
        locked = School.objects.select_for_update().get(pk=school.pk)
        prefix = locked.code_prefix.strip().upper()
        stem = f"{prefix}-"
        max_serial = 0
        existing = Student.objects.filter(student_reg_no__startswith=stem).values_list(
            "student_reg_no", flat=True
        )
        for reg_no in existing:
            suffix = reg_no[len(stem):]
            if suffix.isdigit():
                max_serial = max(max_serial, int(suffix))

        serial = max_serial + 1
        while True:
            candidate = f"{prefix}-{serial:04d}"
            if not Student.objects.filter(student_reg_no=candidate).exists():
                return candidate
            serial += 1


class Student(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="students")
    student_reg_no = models.CharField(
        max_length=50,
        unique=True,
        blank=True,
        help_text="Leave blank on create to auto-issue PREFIX-0001. Unique across every school.",
    )
    full_name = models.CharField(max_length=120)
    class_stream = models.CharField(
        max_length=40,
        blank=True,
        default="",
        help_text="e.g. S.1 East, S.4 A",
    )
    parent_phone = models.CharField(max_length=20, blank=True, default="")
    pin_hash = models.CharField(max_length=255)
    wallet_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    is_active = models.BooleanField(default=True)

    def set_pin(self, raw_pin: str):
        self.pin_hash = make_password(raw_pin)

    def verify_pin(self, raw_pin: str) -> bool:
        return check_password(raw_pin, self.pin_hash)

    def save(self, *args, **kwargs):
        reg_no = (self.student_reg_no or "").strip().upper()
        if reg_no:
            self.student_reg_no = reg_no
            super().save(*args, **kwargs)
            return
        if not self.school_id:
            raise ValueError("A school is required before a CanteenPay code can be issued.")
        # Hold the school row lock until this insert commits so two creates
        # cannot be handed the same PREFIX-serial.
        with transaction.atomic():
            self.student_reg_no = allocate_student_reg_no(self.school)
            super().save(*args, **kwargs)

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
