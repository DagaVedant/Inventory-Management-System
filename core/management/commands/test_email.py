"""Prove the mail configuration works before trusting it with a reset link.

    python manage.py test_email you@example.com

Reports which backend is actually in use, then sends a real message and shows
the real error if it fails. Guessing at SMTP settings by triggering password
resets and waiting is a miserable way to spend an evening.
"""

from django.conf import settings
from django.core.mail import EmailMessage, mail_admins
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Send a test email to check the mail configuration."

    def add_arguments(self, parser):
        parser.add_argument(
            "recipient",
            nargs="?",
            default=None,
            help="Where to send the test message. Omit with --admins.",
        )
        parser.add_argument(
            "--admins",
            action="store_true",
            help="Send to ADMINS instead, testing the crash-report path.",
        )

    def handle(self, *args, **options):
        backend = settings.MAILERS["default"]["BACKEND"]
        to_admins = options["admins"]

        self.stdout.write(f"EMAIL_CONFIGURED: {settings.EMAIL_CONFIGURED}")
        self.stdout.write(f"backend:          {backend}")
        self.stdout.write(f"from:             {settings.DEFAULT_FROM_EMAIL}")
        self.stdout.write(f"errors to:        {settings.ADMINS or '(nobody)'}")

        if to_admins:
            if not settings.ADMINS:
                raise CommandError(
                    "ADMINS is empty, so crash reports go nowhere. Set the "
                    "ERROR_EMAIL environment variable."
                )
            self.stdout.write("\nSending a crash report to ADMINS...")
            mail_admins(
                "Inventory: error mail is working",
                "If you're reading this, real crash reports will reach you too.\n",
            )
            self.stdout.write(
                self.style.SUCCESS("Sent.")
                if settings.EMAIL_CONFIGURED
                else self.style.WARNING("Printed above; nothing left this machine.")
            )
            return

        recipient = options["recipient"]
        if not recipient:
            raise CommandError("Give an address, or pass --admins.")

        if not settings.EMAIL_CONFIGURED:
            self.stdout.write(
                self.style.WARNING(
                    "\nEMAIL_HOST isn't set, so this will print to the console "
                    "rather than send anything. Password reset is disabled in "
                    "this state - the reset page says so rather than pretending."
                )
            )
        else:
            options_used = settings.MAILERS["default"].get("OPTIONS", {})
            self.stdout.write(
                f"host:             {options_used.get('host')}:"
                f"{options_used.get('port')} "
                f"(tls={options_used.get('use_tls')}, "
                f"user={options_used.get('username') or '(none)'})"
            )

        self.stdout.write(f"\nSending to {recipient}...")

        message = EmailMessage(
            subject="Inventory: mail is working",
            body=(
                "If you're reading this, the mail configuration works and "
                "password reset will too.\n"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )

        try:
            sent = message.send()
        except Exception as exc:
            raise CommandError(
                f"\nSend failed: {type(exc).__name__}: {exc}\n\n"
                f"Common causes: wrong port (587 for TLS, 465 for SSL), an "
                f"account password used where a provider-specific app password "
                f"is required, or a From address the provider won't let you "
                f"send as."
            ) from exc

        if not sent:
            self.stdout.write(
                self.style.ERROR("The backend reported sending 0 messages.")
            )
        elif settings.EMAIL_CONFIGURED:
            self.stdout.write(self.style.SUCCESS("Sent. Check that inbox."))
        else:
            self.stdout.write(
                self.style.WARNING(
                    "Printed above - nothing left this machine. Set EMAIL_HOST "
                    "and run this again to send for real."
                )
            )
