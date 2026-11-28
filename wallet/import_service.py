import csv
import io
import secrets

from .models import School, Student
from .sms_service import is_valid_phone, send_welcome_pin_sms


def mint_pin() -> str:
    return f"{secrets.randbelow(10000):04d}"


def format_4digit_pin(raw_pin: str) -> str:
    """Zero-pads numeric PINs up to 4 digits (e.g. Excel-stripped '42' -> '0042')."""
    cleaned = (raw_pin or "").strip()
    if cleaned.isdigit() and len(cleaned) <= 4:
        return cleaned.zfill(4)
    return cleaned


def build_pin_slip(student: Student, pin: str) -> dict:
    return {
        "school_name": student.school.name,
        "full_name": student.full_name,
        "class_stream": student.class_stream,
        "student_reg_no": student.student_reg_no,
        "pin": format_4digit_pin(pin),
    }


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


def import_students_csv(school: School, decoded_csv: str, send_sms_fn=None) -> dict:
    """
    Shared CSV student importer used by both Django Admin and Bursar Portal.
    Returns dict with keys: created_count, updated_count, slips, errors, header_error.
    """
    sms_sender = send_sms_fn or send_welcome_pin_sms
    reader = csv.DictReader(io.StringIO(decoded_csv))
    fieldnames = [(name or "").strip().lower() for name in (reader.fieldnames or [])]
    has_name = "full_name" in fieldnames or "name" in fieldnames
    has_class = "class_stream" in fieldnames or "class" in fieldnames
    if not has_name or not has_class:
        return {
            "header_error": "CSV must include full_name and class_stream columns. parent_phone and pin are optional.",
            "created_count": 0,
            "updated_count": 0,
            "slips": [],
            "errors": [],
        }

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
            parent_phone = _row_value(normalized, "parent_phone", "phone", "parent_mobile")
            raw_pin = _row_value(normalized, "pin")
            reg_no = _row_value(normalized, "student_reg_no").upper()

            if not full_name or not class_stream:
                raise ValueError("full_name and class_stream are required.")

            student = None
            if reg_no:
                student = Student.objects.filter(student_reg_no__iexact=reg_no).first()
            if student is None:
                student = Student.objects.filter(
                    school=school,
                    full_name__iexact=full_name,
                    class_stream__iexact=class_stream,
                ).first()

            if student is not None:
                if student.school_id != school.id:
                    raise ValueError(
                        f"Student code {student.student_reg_no} belongs to another school."
                    )
                if is_valid_phone(parent_phone) and student.parent_phone != parent_phone:
                    student.parent_phone = parent_phone
                    student.save(update_fields=["parent_phone"])
                updated_count += 1
                continue

            minted_pin = format_4digit_pin(raw_pin) if raw_pin else mint_pin()
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
            slips.append(build_pin_slip(student, minted_pin))
            if is_valid_phone(student.parent_phone):
                sms_sender(
                    parent_phone=student.parent_phone,
                    student_name=student.full_name,
                    class_stream=student.class_stream,
                    code=student.student_reg_no,
                    pin=minted_pin,
                )
        except Exception as exc:
            errors.append({"line": line_no, "message": str(exc)})

    return {
        "header_error": None,
        "created_count": created_count,
        "updated_count": updated_count,
        "slips": slips,
        "errors": errors,
    }
