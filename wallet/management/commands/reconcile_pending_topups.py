from django.core.management.base import BaseCommand

from wallet.aggregator_service import reconcile_pending_topups


class Command(BaseCommand):
    help = (
        "Checks PENDING top-ups older than 2 minutes against Flutterwave, "
        "credits verified ones, and marks unpaid ones older than 15 minutes as FAILED."
    )

    def handle(self, *args, **options):
        summary = reconcile_pending_topups()
        self.stdout.write(
            self.style.SUCCESS(
                f"Reconciled {summary['checked']} top-ups: "
                f"{summary['credited']} credited, {summary['failed']} failed, "
                f"{summary['still_pending']} still pending."
            )
        )
