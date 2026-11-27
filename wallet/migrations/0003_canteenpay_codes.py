import re

from django.db import migrations, models


def assign_code_prefixes(apps, schema_editor):
    """Give every existing school a unique prefix, e.g. Test School -> TES."""
    School = apps.get_model("wallet", "School")
    used = set()
    for school in School.objects.all().order_by("created_at"):
        letters = re.sub(r"[^A-Za-z]", "", school.name or "").upper()
        base = (letters[:3] or "SCH")
        prefix = base
        serial = 2
        while prefix in used or School.objects.filter(code_prefix=prefix).exclude(pk=school.pk).exists():
            prefix = f"{base[:3]}{serial}"[:5]
            serial += 1
        used.add(prefix)
        school.code_prefix = prefix
        school.save(update_fields=["code_prefix"])


class Migration(migrations.Migration):

    dependencies = [
        ("wallet", "0002_school_settlement_account_details_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="school",
            name="code_prefix",
            field=models.CharField(
                help_text="Short 3-letter school prefix, e.g. YMB or ARG",
                max_length=5,
                null=True,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name="student",
            name="class_stream",
            field=models.CharField(
                blank=True,
                default="",
                help_text="e.g. S.1 East, S.4 A",
                max_length=40,
            ),
        ),
        migrations.AlterField(
            model_name="student",
            name="parent_phone",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.RunPython(assign_code_prefixes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="school",
            name="code_prefix",
            field=models.CharField(
                help_text="Short 3-letter school prefix, e.g. YMB or ARG",
                max_length=5,
                unique=True,
            ),
        ),
    ]
