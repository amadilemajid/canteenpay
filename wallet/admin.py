from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.shortcuts import render
from django.urls import path

from .import_service import build_pin_slip, import_students_csv, mint_pin
from .models import (
    BursarCashTopUp,
    CanteenOrder,
    CanteenRefund,
    School,
    SchoolOnboardingRequest,
    StaffProfile,
    Student,
    WalletTopUp,
)
from .sms_service import is_valid_phone, send_welcome_pin_sms
from .views import _user_scoped_school, wallet_card_pages


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


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ("name", "code_prefix", "daily_spend_limit", "created_at")
    search_fields = ("name", "code_prefix")

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj=obj))
        if not request.user.is_superuser:
            if "subaccount_id" not in readonly:
                readonly.append("subaccount_id")
            if obj is not None and "code_prefix" not in readonly:
                readonly.append("code_prefix")
        return tuple(readonly)


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

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        scoped_school = _user_scoped_school(request.user)
        if scoped_school is not None:
            return qs.filter(school=scoped_school)
        return qs

    def import_csv(self, request):
        scoped_school = _user_scoped_school(request.user)
        if request.method == "POST":
            form = CsvImportForm(request.POST, request.FILES)
            if scoped_school is not None:
                form.fields["school"].queryset = School.objects.filter(pk=scoped_school.pk)
            if form.is_valid():
                school = scoped_school or form.cleaned_data["school"]
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

                result = import_students_csv(
                    school, decoded_file, send_sms_fn=send_welcome_pin_sms
                )
                if result["header_error"]:
                    self.message_user(
                        request,
                        result["header_error"],
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

                return self._render_pin_slips(
                    request,
                    title="One-Time Take-Home PIN Slips",
                    slips=result["slips"],
                    errors=result["errors"],
                    created_count=result["created_count"],
                    updated_count=result["updated_count"],
                    single=False,
                )
        else:
            form = CsvImportForm()
            if scoped_school is not None:
                form.fields["school"].queryset = School.objects.filter(pk=scoped_school.pk)
                form.fields["school"].initial = scoped_school

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

    def _render_pin_slips(
        self,
        request,
        *,
        title: str,
        slips: list,
        errors: list,
        created_count: int,
        updated_count: int,
        single: bool,
    ):
        return render(
            request,
            "wallet/pin_slip.html",
            {
                **self.admin_site.each_context(request),
                "title": title,
                "slips": slips,
                "errors": errors,
                "created_count": created_count,
                "updated_count": updated_count,
                "single": single,
            },
        )

    @admin.action(description="Reset PIN & Print Single PIN Slip")
    def reset_pin_and_print_slip(self, request, queryset):
        slips = []
        for student in queryset.select_related("school"):
            minted_pin = mint_pin()
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
            slips.append(build_pin_slip(student, minted_pin))

        return self._render_pin_slips(
            request,
            title="One-Time PIN Slip",
            slips=slips,
            errors=[],
            created_count=len(slips),
            updated_count=0,
            single=True,
        )


@admin.register(WalletTopUp)
class WalletTopUpAdmin(admin.ModelAdmin):
    list_display = ("student", "amount", "platform_fee", "status", "created_at")
    list_filter = ("status",)
    readonly_fields = (
        "student",
        "momo_reference_id",
        "payer_phone",
        "amount",
        "platform_fee",
        "status",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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

    def has_module_permission(self, request):
        return (
            request.user.is_active
            and request.user.is_staff
            and super().has_module_permission(request)
        )

    def has_view_permission(self, request, obj=None):
        return (
            request.user.is_active
            and request.user.is_staff
            and super().has_view_permission(request, obj=obj)
        )

    def has_add_permission(self, request):
        return (
            request.user.is_active
            and request.user.is_staff
            and super().has_add_permission(request)
        )

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


@admin.register(SchoolOnboardingRequest)
class SchoolOnboardingRequestAdmin(admin.ModelAdmin):
    list_display = (
        "school_name",
        "district",
        "approx_students",
        "contact_name",
        "contact_role",
        "contact_phone",
        "status",
        "created_at",
    )
    list_filter = ("status", "district")
    search_fields = ("school_name", "district", "contact_name", "contact_phone", "contact_email")
    readonly_fields = ("created_at",)


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "school", "role", "created_at")
    list_filter = ("role", "school")
    search_fields = ("user__username", "school__name", "school__code_prefix")
    readonly_fields = ("created_at",)


class StaffProfileInline(admin.StackedInline):
    model = StaffProfile
    can_delete = True
    extra = 0


try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass


@admin.register(User)
class CanteenPayUserAdmin(BaseUserAdmin):
    inlines = (StaffProfileInline,)
