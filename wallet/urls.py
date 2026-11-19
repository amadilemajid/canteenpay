from django.urls import path
from . import views

urlpatterns = [
    path("checkout/", views.canteen_checkout, name="canteen_checkout"),
    path("momo/callback/", views.momo_callback_webhook, name="momo_callback"),
]
