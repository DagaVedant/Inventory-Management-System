"""Fill an account with believable data so the app is worth looking at.

    python manage.py seed_demo --user demo --password something

Wipes and rebuilds that user's parts and projects, so it is safe to re-run.
Deliberately leaves the inventory mid-story: one project on the bench holding
parts, one already torn down with real losses recorded.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Part, Project, ProjectPart

User = get_user_model()

PARTS = [
    # name, value, package, pins, voltage, qty, tags
    ("DHT22 temperature/humidity", "", "module", 4, "3V3-5V", 4, "sensor, temperature, humidity"),
    ("BMP280 pressure", "", "module", 4, "3V3", 2, "sensor, pressure, i2c"),
    ("MPU-6050 6-axis IMU", "", "module", 8, "3V3-5V", 3, "sensor, imu, i2c"),
    ("HC-SR04 ultrasonic", "", "module", 4, "5V", 5, "sensor, distance"),
    ("ESP32 devkit v1", "", "module", 30, "5V", 3, "mcu, wifi"),
    ("Arduino Nano", "", "module", 30, "5V", 2, "mcu"),
    ("SSD1306 OLED 128x64", "", "module", 4, "3V3-5V", 2, "display, i2c"),
    ("Resistor", "10k", "through-hole", 2, "", 180, "passive, resistor"),
    ("Resistor", "220R", "through-hole", 2, "", 140, "passive, resistor"),
    ("Resistor", "4.7k", "through-hole", 2, "", 95, "passive, resistor, i2c-pullup"),
    ("Ceramic capacitor", "100nF", "through-hole", 2, "", 120, "passive, capacitor, decoupling"),
    ("Electrolytic capacitor", "470uF", "through-hole", 2, "16V", 24, "passive, capacitor"),
    ("LED", "red 5mm", "through-hole", 2, "", 60, "led, indicator"),
    ("LED", "green 5mm", "through-hole", 2, "", 45, "led, indicator"),
    ("AMS1117-3.3 regulator", "", "SOT-223", 3, "3V3", 8, "power, regulator, 3v3"),
    ("2N2222 transistor", "", "TO-92", 3, "", 30, "transistor, npn"),
    ("1N4148 diode", "", "through-hole", 2, "", 50, "diode"),
    ("Tactile push button", "", "through-hole", 4, "", 40, "button, input"),
    ("Micro servo SG90", "", "module", 3, "5V", 4, "actuator, servo"),
    ("Perfboard 70x90mm", "", "board", 0, "", 6, "board, perfboard"),
]


class Command(BaseCommand):
    help = "Populate an account with demo parts and projects."

    def add_arguments(self, parser):
        parser.add_argument("--user", default="demo")
        parser.add_argument("--password", default=None)

    @transaction.atomic
    def handle(self, *args, **options):
        username = options["user"]
        password = options["password"]

        user, created = User.objects.get_or_create(
            username=username, defaults={"email": ""}
        )
        if password:
            user.set_password(password)
            user.save(update_fields=["password"])
        elif created:
            self.stdout.write(
                self.style.WARNING(
                    f"{username} created with no usable password. "
                    f"Re-run with --password to set one."
                )
            )

        ProjectPart.objects.filter(project__user=user).delete()
        Project.objects.filter(user=user).delete()
        Part.objects.filter(user=user).delete()

        parts = {}
        for name, value, package, pins, voltage, qty, tags in PARTS:
            key = f"{name} {value}".strip()
            parts[key] = Part.objects.create(
                user=user,
                name=name,
                value=value,
                package=package,
                pin_count=pins or None,
                voltage=voltage,
                qty_owned=qty,
                tags=tags,
            )

        # --- on the bench right now, holding parts ---------------------------
        weather = Project.objects.create(
            user=user,
            name="Weather station",
            description=(
                "ESP32 reading temperature, humidity and pressure, posting to "
                "the network. On perfboard, still half wired."
            ),
        )
        for key, qty in [
            ("ESP32 devkit v1", 1),
            ("DHT22 temperature/humidity", 1),
            ("BMP280 pressure", 1),
            ("Resistor 4.7k", 2),
            ("Ceramic capacitor 100nF", 3),
            ("AMS1117-3.3 regulator", 1),
            ("Perfboard 70x90mm", 1),
        ]:
            ProjectPart.objects.create(
                project=weather, part=parts[key], qty_allocated=qty
            )

        parking = Project.objects.create(
            user=user,
            name="Parking sensor",
            description="Ultrasonic distance to an OLED. Breadboard for now.",
        )
        for key, qty in [
            ("Arduino Nano", 1),
            ("HC-SR04 ultrasonic", 2),
            ("SSD1306 OLED 128x64", 1),
            ("Resistor 220R", 2),
            ("LED red 5mm", 2),
        ]:
            ProjectPart.objects.create(
                project=parking, part=parts[key], qty_allocated=qty
            )
        # one already handed back mid-build
        line = parking.lines.get(part=parts["HC-SR04 ultrasonic"])
        line.qty_returned = 1
        line.save(update_fields=["qty_returned"])

        # --- torn down, with real losses -------------------------------------
        balancer = Project.objects.create(
            user=user,
            name="Self-balancing robot",
            description=(
                "IMU plus two servos. Worked for about four seconds, then the "
                "regulator let the smoke out."
            ),
        )
        outcomes = []
        for key, qty, returned, soldered, broken in [
            ("MPU-6050 6-axis IMU", 1, 1, 0, 0),
            ("Arduino Nano", 1, 1, 0, 0),
            ("Micro servo SG90", 2, 1, 0, 1),
            ("AMS1117-3.3 regulator", 2, 0, 1, 1),
            ("Resistor 10k", 4, 4, 0, 0),
            ("Perfboard 70x90mm", 1, 0, 1, 0),
        ]:
            line = ProjectPart.objects.create(
                project=balancer, part=parts[key], qty_allocated=qty
            )
            outcomes.append((line, returned, soldered, broken))
        balancer.tear_down(outcomes)

        blinky = Project.objects.create(
            user=user, name="LED matrix test", description="Scrap build, all reusable."
        )
        outcomes = []
        for key, qty in [("Arduino Nano", 1), ("LED green 5mm", 8), ("Resistor 220R", 8)]:
            line = ProjectPart.objects.create(
                project=blinky, part=parts[key], qty_allocated=qty
            )
            outcomes.append((line, qty, 0, 0))
        blinky.tear_down(outcomes)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {username}: {len(parts)} parts, "
                f"{Project.objects.filter(user=user).count()} projects "
                f"(2 active, 2 torn down)."
            )
        )
