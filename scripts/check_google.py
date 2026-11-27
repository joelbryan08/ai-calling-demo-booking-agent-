"""Checks your Google setup, one presenter at a time, and tells you what to fix.
Run from the project folder:   python scripts/check_google.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings                                              # noqa: E402
from app.google_clients import GoogleCalendar, check_sheet, service_account_email   # noqa: E402


def main():
    if settings.calendar_backend != "google":
        print("CALENDAR_BACKEND is not 'google' in .env, so the app is using the fake calendar.")
        return 1
    sa = service_account_email()
    print(f"Service account: {sa or '(not found: check GOOGLE_SERVICE_ACCOUNT_FILE)'}   Mode setting: {settings.google_mode}\n")
    cal, bad = GoogleCalendar(), 0
    for email in settings.presenters:
        d = cal.diagnose(email)
        if d["ok"]:
            if d["mode"] == "delegated":
                print(f"[OK ] {email}: delegated. Meet links and invites will work.")
            else:
                print(f"[OK ] {email}: shared calendar. Free/busy and booking work; no auto Meet link or invites.")
                if not d["link"]:
                    print("      Tip: set a meeting link for this presenter (MEETING_LINKS or DEFAULT_MEETING_LINK in .env).")
        else:
            bad += 1
            print(f"[FAIL] {email}: {d['detail']}")
            print(f"      Fix: ask them to share their calendar with {sa or 'the service account'} (Settings and sharing,\n"
                  "      Share with specific people, permission: Make changes to events), or have an admin enable delegation.")
    if settings.sheet_id:
        err = check_sheet()
        print(f"\n[{'OK ' if not err else 'FAIL'}] Google Sheet: {err or 'reachable'}")
        if err:
            bad += 1
            print(f"      Fix: share the sheet with {sa or 'the service account'} as Editor, and check SHEET_ID.")
    else:
        print("\n[SKIP] SHEET_ID is empty, so calls are not written to a Google Sheet.")
    print("\nAll good." if not bad else f"\n{bad} problem(s) above.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
