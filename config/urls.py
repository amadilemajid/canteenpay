from django.contrib import admin
from django.urls import path
from wallet.views import (
    parent_topup_page,
    canteen_pos_page,
    bursar_daily_report,
    daily_topup_noticeboard,
    lookup_student,
    initiate_parent_topup,
    canteen_checkout,
    momo_callback_webhook,
    airtel_callback_webhook,
    aggregator_callback_webhook,
    check_student_balance,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    # Web Pages
    path("", parent_topup_page, name="parent_topup_page"),
    path("pos/", canteen_pos_page, name="canteen_pos_page"),
    path("report/daily/", bursar_daily_report, name="bursar_daily_report"),
    path("report/topup-noticeboard/", daily_topup_noticeboard, name="daily_topup_noticeboard"),
    path("report/noticeboard/", daily_topup_noticeboard, name="daily_topup_noticeboard"),
    # API Endpoints
    path("api/student/lookup/", lookup_student, name="lookup_student"),
    path("api/parent/topup/", initiate_parent_topup, name="initiate_parent_topup"),
    path("api/canteen/checkout/", canteen_checkout, name="canteen_checkout"),
    path("api/student/balance/", check_student_balance, name="check_student_balance"),
    path("api/momo/callback/", momo_callback_webhook, name="momo_callback"),
    path("api/airtel/callback/", airtel_callback_webhook, name="airtel_callback"),
    path("api/aggregator/callback/", aggregator_callback_webhook, name="aggregator_callback"),
]
