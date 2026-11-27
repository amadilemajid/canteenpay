from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from wallet.aggregator_service import verify_and_credit_flw_topup
from wallet.models import WalletTopUp


class Command(BaseCommand):
    help = (
        "Checks PENDING top-ups older than 2 minutes against Flutterwave, "
        "credits verified ones, and marks unpaid ones older than 15 minutes as FAILED."
    )

    def handle(self, *args, **options):
        now = timezone.now()
        two_minutes_ago = now - timedelta(minutes=2)
        fifteen_minutes_ago = now - timedelta(minutes=15)

        pending_refs = list(
            WalletTopUp.objects.filter(
                status="PENDING", created_at__lte=two_minutes_ago
            )
            .order_by("created_at")
            .values_list("momo_reference_id", flat=True)
        )

        credited_count = 0
        failed_count = 0
        still_pending_count = 0

        for ref_id in pending_refs:
            outcome, topup, _ = verify_and_credit_flw_topup(ref_id)
            if outcome == "CREDITED":
                credited_count += 1
            elif outcome == "FAILED":
                failed_count += 1
            elif outcome == "PENDING" and topup.created_at <= fifteen_minutes_ago:
                with transaction.atomic():
                    locked = WalletTopUp.objects.select_for_update().get(pk=topup.pk)
                    if locked.status == "PENDING":
                        locked.status = "FAILED"
                        locked.save(update_fields=["status"])
                        failed_count += 1
            elif outcome == "PENDING":
                still_pending_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Reconciled {len(pending_refs)} top-ups: "
                f"{credited_count} credited, {failed_count} failed, {still_pending_count} still pending."
            )
        )
