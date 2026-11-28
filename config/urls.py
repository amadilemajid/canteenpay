from django.contrib import admin
from django.contrib.auth.views import (
    LoginView,
    LogoutView,
    PasswordChangeDoneView,
    PasswordChangeView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.urls import path
from wallet.views import (
    parent_topup_page,
    parent_balance_page,
    parent_balance_lookup,
    parent_wallet_settings,
    school_register_page,
    canteen_pos_page,
    pos_manifest,
    pos_service_worker,
    assetlinks_json,
    policies_page,
    bursar_daily_report,
    bursar_import_students,
    daily_topup_noticeboard,
    wallet_cards_page,
    lookup_student,
    pos_student_search,
    initiate_parent_topup,
    canteen_checkout,
    aggregator_callback_webhook,
    verify_parent_topup,
    check_student_balance,
    healthz,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    # Staff Auth & Password Management
    path(
        "staff/login/",
        LoginView.as_view(
            template_name="wallet/staff_login.html",
            redirect_authenticated_user=True,
        ),
        name="staff_login",
    ),
    path(
        "staff/logout/",
        LogoutView.as_view(next_page="/staff/login/"),
        name="staff_logout",
    ),
    path(
        "staff/password-reset/",
        PasswordResetView.as_view(
            template_name="wallet/password_reset_form.html",
            email_template_name="wallet/password_reset_email.txt",
            subject_template_name="wallet/password_reset_subject.txt",
            success_url="/staff/password-reset/done/",
        ),
        name="password_reset",
    ),
    path(
        "staff/password-reset/done/",
        PasswordResetDoneView.as_view(
            template_name="wallet/password_reset_done.html",
        ),
        name="password_reset_done",
    ),
    path(
        "staff/reset/<uidb64>/<token>/",
        PasswordResetConfirmView.as_view(
            template_name="wallet/password_reset_confirm.html",
            success_url="/staff/reset/done/",
        ),
        name="password_reset_confirm",
    ),
    path(
        "staff/reset/done/",
        PasswordResetCompleteView.as_view(
            template_name="wallet/password_reset_complete.html",
        ),
        name="password_reset_complete",
    ),
    path(
        "staff/password-change/",
        PasswordChangeView.as_view(
            template_name="wallet/password_change_form.html",
            success_url="/staff/password-change/done/",
        ),
        name="password_change",
    ),
    path(
        "staff/password-change/done/",
        PasswordChangeDoneView.as_view(
            template_name="wallet/password_change_done.html",
        ),
        name="password_change_done",
    ),
    # Health check
    path("healthz/", healthz, name="healthz"),
    # Web Pages
    path("", parent_topup_page, name="parent_topup_page"),
    path("parent/balance/", parent_balance_page, name="parent_balance_page"),
    path("schools/register/", school_register_page, name="school_register_page"),
    path("pos/", canteen_pos_page, name="canteen_pos_page"),
    path("manifest.json", pos_manifest, name="pos_manifest"),
    path("sw.js", pos_service_worker, name="pos_service_worker"),
    path(".well-known/assetlinks.json", assetlinks_json, name="assetlinks_json"),
    path("policies/", policies_page, name="policies_page"),
    path("report/daily/", bursar_daily_report, name="bursar_daily_report"),
    path("report/import-students/", bursar_import_students, name="bursar_import_students"),
    path("report/topup-noticeboard/", daily_topup_noticeboard, name="daily_topup_noticeboard"),
    path("report/noticeboard/", daily_topup_noticeboard, name="daily_topup_noticeboard"),
    path("report/cards/", wallet_cards_page, name="wallet_cards_page"),
    # API Endpoints
    path("api/student/lookup/", lookup_student, name="lookup_student"),
    path("api/parent/balance/", parent_balance_lookup, name="parent_balance_lookup"),
    path("api/parent/settings/", parent_wallet_settings, name="parent_wallet_settings"),
    path("api/pos/students/search/", pos_student_search, name="pos_student_search"),
    path("api/parent/topup/", initiate_parent_topup, name="initiate_parent_topup"),
    path("api/canteen/checkout/", canteen_checkout, name="canteen_checkout"),
    path("api/student/balance/", check_student_balance, name="check_student_balance"),
    path("api/aggregator/callback/", aggregator_callback_webhook, name="aggregator_callback"),
    path("api/parent/topup/verify/", verify_parent_topup, name="verify_parent_topup"),
]
