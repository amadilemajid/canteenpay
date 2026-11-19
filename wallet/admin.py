import csv
import io
import base64
import qrcode
from django import forms
from django.contrib import admin, messages
from django.contrib.auth.hashers import make_password
from django.shortcuts import render, redirect
from django.urls import path
from .models import School, Student, WalletTopUp, CanteenOrder


class CsvImportForm(forms.Form):
    school = forms.ModelChoiceField(queryset=School.objects.all())
    csv_file = forms.FileField(
        help_text="Columns required: student_reg_no, full_name, parent_phone, pin"
    )


class StudentAdminForm(forms.ModelForm):
    pin = forms.CharField(
        max_length=10, required=False,
        help_text="Enter a new PIN to set or change it. Leave blank to keep existing."
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
    list_display = ("name", "daily_spend_limit", "created_at")


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    form = StudentAdminForm
    list_display = ("student_reg_no", "full_name", "school", "wallet_balance", "is_active")
    search_fields = ("student_reg_no", "full_name", "parent_phone")
    list_filter = ("school", "is_active")
    exclude = ("pin_hash",)
    actions = ["print_qr_stickers"]
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
                csv_file = request.FILES["csv_file"]
                decoded_file = csv_file.read().decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(decoded_file))

                created_count = 0
                updated_count = 0

                for row in reader:
                    reg_no = row["student_reg_no"].strip()
                    full_name = row["full_name"].strip()
                    parent_phone = row["parent_phone"].strip()
                    raw_pin = str(row.get("pin", "1234")).strip()

                    student, created = Student.objects.get_or_create(
                        student_reg_no=reg_no,
                        defaults={
                            "school": school,
                            "full_name": full_name,
                            "parent_phone": parent_phone,
                        },
                    )
                    if not created:
                        student.school = school
                        student.full_name = full_name
                        student.parent_phone = parent_phone
                        updated_count += 1
                    else:
                        created_count += 1

                    student.set_pin(raw_pin)
                    student.save()

                self.message_user(
                    request,
                    f"Import complete: {created_count} created, {updated_count} updated.",
                    level=messages.SUCCESS,
                )
                return redirect("..")
        else:
            form = CsvImportForm()

        context = {
            **self.admin_site.each_context(request),
            "form": form,
            "title": "Bulk Import Students via CSV",
        }
        return render(request, "admin/wallet/csv_form.html", context)

    @admin.action(description="Print QR Sticker Sheet for selected students")
    def print_qr_stickers(self, request, queryset):
        stickers = []
        for student in queryset:
            qr = qrcode.QRCode(box_size=6, border=2)
            qr.add_data(student.student_reg_no)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")

            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            qr_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

            stickers.append(
                {
                    "reg_no": student.student_reg_no,
                    "full_name": student.full_name,
                    "school": student.school.name,
                    "qr_base64": qr_base64,
                }
            )

        return render(
            request,
            "admin/wallet/qr_sticker_sheet.html",
            {"stickers": stickers},
        )


@admin.register(WalletTopUp)
class WalletTopUpAdmin(admin.ModelAdmin):
    list_display = ("student", "amount", "platform_fee", "status", "created_at")
    list_filter = ("status",)


@admin.register(CanteenOrder)
class CanteenOrderAdmin(admin.ModelAdmin):
    list_display = ("student", "amount", "items_note", "created_at")
