import csv
import io
import secrets

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.hashers import make_password
from django.shortcuts import render
from django.urls import path

from .models import (
    BursarCashTopUp,
    CanteenOrder,
    CanteenRefund,
    School,
    Student,
    WalletTopUp,
)
from .sms_service import is_valid_phone, send_welcome_pin_sms
from .views import wallet_card_pages


class CsvImportForm(forms.Form):
    school = forms.ModelChoiceField(queryset=School.objects.all())
    csv_file = forms.FileField(
        help_text=(
            "Required columns: full_name, class_stream. "
            "Optional: parent_phone, pin, student_reg_no. "
            "School admission-number columns are ignored."
        )
    )


class StudentAdminForm(forms.ModelForm):
    pin = forms.CharField(
        max_length=10,
        required=False,
        help_text="Enter a new PIN to set or change it. Leave blank to keep the existing PIN.",
    )

    class Meta:
        model = Student
        fields = "__all__"

    def save(self, commit=True):
        instance = super().save(commit=False)
        raw_pin = self.cleaned_data.get("pin")
        if raw_pin:
            instance.pin_hash = make_password(raw_pin)
        if commit:
            instance.save()
        return instance


def _mint_pin():
    return f"{secrets.randbelow(10000):04d}"


def _normalized_row(row):
    return {
        (key or "").strip().lower(): (value or "").strip()
        for key, value in row.items()
        if key
    }


def _row_value(normalized, *keys):
    for key in keys:
        value = normalized.get(key, "")
        if value:
            return value
    return ""


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ("name", "code_prefix", "daily_spend_limit", "created_at")
    search_fields = ("name", "code_prefix")


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    form = StudentAdminForm
    list_display = (
        "student_reg_no",
        "full_name",
        "class_stream",
        "school",
        "wallet_balance",
        "is_active",
    )
    search_fields = ("student_reg_no", "full_name", "class_stream", "parent_phone")
    list_filter = ("school", "is_active")
    exclude = ("pin_hash",)
    readonly_fields = ("wallet_balance", "failed_pin_attempts", "pin_locked_until")
    actions = ["print_wallet_cards", "reset_pin_and_print_slip"]
    change_list_template = "admin/wallet/student_changelist.html"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "import-csv/",
                self.admin_site.admin_view(self.import_csv),
                name="wallet_student_import_csv",
            ),
        ]
        return custom_urls + urls

    def import_csv(self, request):
        if request.method == "POST":
            form = CsvImportForm(request.POST, request.FILES)
            if form.is_valid():
                school = form.cleaned_data["school"]
                try:
                    decoded_file = request.FILES["csv_file"].read().decode("utf-8-sig")
                except UnicodeDecodeError:
                    self.message_user(
                        request,
                        "Could not read that file as UTF-8. Save the CSV as UTF-8 and try again.",
                        level=messages.ERROR,
                    )
                    return render(
                        request,
                        "admin/wallet/csv_form.html",
                        {
                            **self.admin_site.each_context(request),
                            "form": form,
                            "title": "Bulk Import Students via CSV",
                        },
                    )

                reader = csv.DictReader(io.StringIO(decoded_file))
                fieldnames = [
                    (name or "").strip().lower() for name in (reader.fieldnames or [])
                ]
                has_name = "full_name" in fieldnames or "name" in fieldnames
                has_class = "class_stream" in fieldnames or "class" in fieldnames
                if not has_name or not has_class:
                    self.message_user(
                        request,
                        "CSV must include full_name and class_stream columns. parent_phone and pin are optional.",
                        level=messages.ERROR,
                    )
                    return render(
                        request,
                        "admin/wallet/csv_form.html",
                        {
                            **self.admin_site.each_context(request),
                            "form": form,
                            "title": "Bulk Import Students via CSV",
                        },
                    )

                created_count = 0
                updated_count = 0
                slips = []
                errors = []

                for row in reader:
                    line_no = reader.line_num
                    try:
                        normalized = _normalized_row(row)
                        if not any(normalized.values()):
                            continue

                        full_name = _row_value(normalized, "full_name", "name")
                        class_stream = _row_value(normalized, "class_stream", "class")
                        parent_phone = _row_value(
                            normalized, "parent_phone", "phone", "parent_mobile"
                        )
                        raw_pin = _row_value(normalized, "pin")
                        reg_no = _row_value(normalized, "student_reg_no").upper()

                        if not full_name or not class_stream:
                            raise ValueError("full_name and class_stream are required.")

                        student = None
                        if reg_no:
                            student = Student.objects.filter(
                                student_reg_no__iexact=reg_no
                            ).first()
                        if student is None:
                            student = Student.objects.filter(
                                school=school,
                                full_name__iexact=full_name,
                                class_stream__iexact=class_stream,
                            ).first()

                        if student is not None:
                            if is_valid_phone(parent_phone) and student.parent_phone != parent_phone:
                                student.parent_phone = parent_phone
                                student.save(update_fields=["parent_phone"])
                            updated_count += 1
                            continue

                        minted_pin = raw_pin or _mint_pin()
                        student = Student(
                            school=school,
                            student_reg_no=reg_no,
                            full_name=full_name,
                            class_stream=class_stream,
                            parent_phone=parent_phone if is_valid_phone(parent_phone) else "",
                        )
                        student.set_pin(minted_pin)
                        student.save()
                        created_count += 1
                        slips.append(
                            {
                                "full_name": student.full_name,
                                "class_stream": student.class_stream,
                                "student_reg_no": student.student_reg_no,
                                "pin": minted_pin,
                            }
                        )
                        if is_valid_phone(student.parent_phone):
                            send_welcome_pin_sms(
                                parent_phone=student.parent_phone,
                                student_name=student.full_name,
                                class_stream=student.class_stream,
                                code=student.student_reg_no,
                                pin=minted_pin,
                            )
                    except Exception as exc:
                        errors.append({"line": line_no, "message": str(exc)})

                return render(
                    request,
                    "wallet/pin_slip.html",
                    {
                        **self.admin_site.each_context(request),
                        "title": "One-Time Take-Home PIN Slips",
                        "slips": slips,
                        "errors": errors,
                        "created_count": created_count,
                        "updated_count": updated_count,
                        "single": False,
                    },
                )
        else:
            form = CsvImportForm()

        context = {
            **self.admin_site.each_context(request),
            "form": form,
            "title": "Bulk Import Students via CSV",
        }
        return render(request, "admin/wallet/csv_form.html", context)

    @admin.action(description="Print CanteenPay Wallet Cards")
    def print_wallet_cards(self, request, queryset):
        students = queryset.select_related("school")
        return render(
            request,
            "wallet/wallet_cards.html",
            {"pages": wallet_card_pages(students)},
        )

    @admin.action(description="Reset PIN & Print Single PIN Slip")
    def reset_pin_and_print_slip(self, request, queryset):
        slips = []
        for student in queryset.select_related("school"):
            minted_pin = _mint_pin()
            student.set_pin(minted_pin)
            student.failed_pin_attempts = 0
            student.pin_locked_until = None
            student.save(
                update_fields=["pin_hash", "failed_pin_attempts", "pin_locked_until"]
            )
            if is_valid_phone(student.parent_phone):
                send_welcome_pin_sms(
                    parent_phone=student.parent_phone,
                    student_name=student.full_name,
                    class_stream=student.class_stream,
                    code=student.student_reg_no,
                    pin=minted_pin,
                )
            slips.append(
                {
                    "full_name": student.full_name,
                    "class_stream": student.class_stream,
                    "student_reg_no": student.student_reg_no,
                    "pin": minted_pin,
                }
            )

        return render(
            request,
            "wallet/pin_slip.html",
            {
                **self.admin_site.each_context(request),
                "title": "One-Time PIN Slip",
                "slips": slips,
                "errors": [],
                "created_count": len(slips),
                "updated_count": 0,
                "single": True,
            },
        )


@admin.register(WalletTopUp)
class WalletTopUpAdmin(admin.ModelAdmin):
    list_display = ("student", "amount", "platform_fee", "status", "created_at")
    list_filter = ("status",)


@admin.register(CanteenOrder)
class CanteenOrderAdmin(admin.ModelAdmin):
    list_display = ("student", "amount", "items_note", "attendant", "created_at")
    search_fields = ("student__student_reg_no", "student__full_name", "attendant__username")
    readonly_fields = ("student", "attendant", "idempotency_key", "amount", "items_note", "created_at")


@admin.register(CanteenRefund)
class CanteenRefundAdmin(admin.ModelAdmin):
    list_display = ("order", "amount", "reason", "refunded_by", "created_at")
    readonly_fields = ("refunded_by", "created_at")

    def save_model(self, request, obj, form, change):
        if not change and not obj.refunded_by:
            obj.refunded_by = request.user
        super().save_model(request, obj, form, change)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BursarCashTopUp)
class BursarCashTopUpAdmin(admin.ModelAdmin):
    list_display = ("student", "amount", "receipt_note", "received_by", "created_at")
    search_fields = ("student__student_reg_no", "student__full_name", "receipt_note")
    readonly_fields = ("received_by", "created_at")

    def save_model(self, request, obj, form, change):
        if not change and not obj.received_by:
            obj.received_by = request.user
        super().save_model(request, obj, form, change)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
