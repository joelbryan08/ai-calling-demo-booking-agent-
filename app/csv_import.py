import csv
import io
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import phonenumbers

ALIASES = {
    "phone": ["phone", "mobile", "number", "phone_number", "phone number", "contact", "mobile_number"],
    "name": ["name", "full_name", "full name", "contact_name"],
    "company": ["company", "organization", "organisation", "business"],
    "email": ["email", "email_id", "email address", "e-mail"],
    "timezone": ["timezone", "time_zone", "tz"],
    "notes": ["notes", "note", "comments"],
}


def normalize_phone(raw: str, region: str = "IN") -> str | None:
    try:
        n = phonenumbers.parse((raw or "").strip(), region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(n):
        return None
    return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)


def _map_headers(fieldnames):
    mapping = {}
    for f in fieldnames or []:
        key = (f or "").strip().lower()
        for canon, options in ALIASES.items():
            if key in options and canon not in mapping.values():
                mapping[f] = canon
    return mapping


def parse_leads_csv(data: bytes, region: str = "IN"):
    """Returns (valid_rows, rejected, duplicates_in_file, total_rows)."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    mapping = _map_headers(reader.fieldnames)
    if "phone" not in mapping.values():
        raise ValueError("CSV needs a phone column (phone / mobile / number)")

    valid, rejected, seen, dupes, total = [], [], set(), 0, 0
    for i, raw in enumerate(reader, start=2):  # row 1 is the header
        total += 1
        row = {canon: (raw.get(orig) or "").strip() for orig, canon in mapping.items()}
        phone = normalize_phone(row.get("phone", ""), region)
        if not phone:
            rejected.append({"row": i, "reason": "invalid phone number"})
            continue
        tz = row.get("timezone") or None
        if tz:
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError):
                rejected.append({"row": i, "reason": f"invalid timezone '{tz}'"})
                continue
        if phone in seen:
            dupes += 1
            continue
        seen.add(phone)
        valid.append({
            "phone": phone,
            "name": row.get("name") or None,
            "company": row.get("company") or None,
            "email": row.get("email") or None,
            "timezone": tz,
            "notes": row.get("notes") or None,
        })
    return valid, rejected, dupes, total
