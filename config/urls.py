from django.contrib import admin
from django.urls import path
from wallet.views import (
    parent_topup_page,
    canteen_pos_page,
    pos_manifest,
    pos_service_worker,
    policies_page,
    bursar_daily_report,
    daily_topup_noticeboard,
    wallet_cards_page,
    lookup_student,
    pos_student_search,
    initiate_parent_topup,
    canteen_checkout,
    aggregator_callback_webhook,
    verify_parent_topup,
    check_student_balance,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    # Web Pages
    path("", parent_topup_page, name="parent_topup_page"),
    path("pos/", canteen_pos_page, name="canteen_pos_page"),
    path("manifest.json", pos_manifest, name="pos_manifest"),
    path("sw.js", pos_service_worker, name="pos_service_worker"),
    path("policies/", policies_page, name="policies_page"),
    path("report/daily/", bursar_daily_report, name="bursar_daily_report"),
    path("report/topup-noticeboard/", daily_topup_noticeboard, name="daily_topup_noticeboard"),
    path("report/noticeboard/", daily_topup_noticeboard, name="daily_topup_noticeboard"),
    path("report/cards/", wallet_cards_page, name="wallet_cards_page"),
    # API Endpoints
    path("api/student/lookup/", lookup_student, name="lookup_student"),
    path("api/pos/students/search/", pos_student_search, name="pos_student_search"),
    path("api/parent/topup/", initiate_parent_topup, name="initiate_parent_topup"),
    path("api/canteen/checkout/", canteen_checkout, name="canteen_checkout"),
    path("api/student/balance/", check_student_balance, name="check_student_balance"),
    path("api/aggregator/callback/", aggregator_callback_webhook, name="aggregator_callback"),
    path("api/parent/topup/verify/", verify_parent_topup, name="verify_parent_topup"),
]
