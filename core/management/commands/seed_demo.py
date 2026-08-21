"""Fill an account with believable data so the app is worth looking at.

    python manage.py seed_demo --user demo --password something

Wipes and rebuilds that user's parts and projects, so it is safe to re-run.

The four projects are chosen to show every state the model has: one holding a
lot, one with parts already handed back mid-build, one torn down with real
losses, and one torn down clean. One part is fully committed so `available: 0`
appears somewhere on the list.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Part, Project, ProjectPart

User = get_user_model()

PARTS = [
    # name, value, package, pins, voltage, qty, tags
    ("ESP32 devkit v1", "", "module", 30, "5V", 2, "mcu, wifi, bluetooth"),
    ("Arduino Nano", "", "module", 30, "5V", 4, "mcu"),
    ("Raspberry Pi Pico", "", "module", 40, "3V3", 3, "mcu, rp2040"),
    ("DHT22 temperature/humidity", "", "module", 4, "3V3-5V", 4, "sensor, temperature, humidity"),
    ("BMP280 pressure", "", "module", 4, "3V3", 3, "sensor, pressure, i2c"),
    ("MPU-6050 6-axis IMU", "", "module", 8, "3V3-5V", 3, "sensor, imu, i2c"),
    ("HC-SR04 ultrasonic", "", "module", 4, "5V", 5, "sensor, distance"),
    ("LDR photoresistor", "", "through-hole", 2, "", 22, "sensor, light"),
    ("DS18B20 temperature", "", "TO-92", 3, "3V3-5V", 6, "sensor, temperature, onewire"),
    ("SSD1306 OLED 128x64", "", "module", 4, "3V3-5V", 2, "display, i2c"),
    ("16x2 character LCD", "", "module", 16, "5V", 2, "display"),
    ("WS2812B LED strip 1m", "", "strip", 3, "5V", 3, "led, addressable"),
    ("Resistor", "220R", "through-hole", 2, "", 140, "passive, resistor"),
    ("Resistor", "1k", "through-hole", 2, "", 160, "passive, resistor"),
    ("Resistor", "4.7k", "through-hole", 2, "", 95, "passive, resistor, i2c-pullup"),
    ("Resistor", "10k", "through-hole", 2, "", 180, "passive, resistor"),
    ("Ceramic capacitor", "100nF", "through-hole", 2, "", 120, "passive, capacitor, decoupling"),
    ("Electrolytic capacitor", "470uF", "through-hole", 2, "16V", 24, "passive, capacitor"),
    ("Electrolytic capacitor", "1000uF", "through-hole", 2, "25V", 12, "passive, capacitor"),
    ("LED", "red 5mm", "through-hole", 2, "", 60, "led, indicator"),
    ("LED", "green 5mm", "through-hole", 2, "", 45, "led, indicator"),
    ("LED", "blue 5mm", "through-hole", 2, "", 30, "led, indicator"),
    ("AMS1117-3.3 regulator", "", "SOT-223", 3, "3V3", 8, "power, regulator, 3v3"),
    ("LM7805 regulator", "", "TO-220", 3, "5V", 5, "power, regulator, 5v"),
    ("2N2222 transistor", "", "TO-92", 3, "", 30, "transistor, npn"),
    ("IRLZ44N MOSFET", "", "TO-220", 3, "", 8, "transistor, mosfet, logic-level"),
    ("1N4148 diode", "", "through-hole", 2, "", 50, "diode"),
    ("1N4007 diode", "", "through-hole", 2, "1000V", 35, "diode, rectifier"),
    ("Tactile push button", "", "through-hole", 4, "", 40, "button, input"),
    ("Rotary encoder KY-040", "", "module", 5, "5V", 4, "input, encoder"),
    ("Micro servo SG90", "", "module", 3, "5V", 6, "actuator, servo"),
    ("28BYJ-48 stepper + driver", "", "module", 5, "5V", 3, "actuator, stepper"),
    ("Piezo buzzer", "", "through-hole", 2, "5V", 9, "audio, buzzer"),
    ("Perfboard 70x90mm", "", "board", 0, "", 6, "board, perfboard"),
    ("Screw terminal 2-way", "", "through-hole", 2, "", 28, "connector"),
    ("Pin header 40-way", "", "strip", 40, "", 15, "connector, header"),
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

        # 1. On the bench, holding a lot. Both ESP32s are committed, so this
        #    part shows available: 0 on the parts list.
        weather = Project.objects.create(
            user=user,
            name="Weather station",
            description=(
                "ESP32 reading temperature, humidity and pressure, posting to "
                "the network every five minutes. On perfboard, still half wired."
            ),
        )
        for key, qty in [
            ("ESP32 devkit v1", 1),
            ("DHT22 temperature/humidity", 1),
            ("BMP280 pressure", 1),
            ("DS18B20 temperature", 2),
            ("Resistor 4.7k", 3),
            ("Ceramic capacitor 100nF", 4),
            ("Electrolytic capacitor 470uF", 1),
            ("AMS1117-3.3 regulator", 1),
            ("Screw terminal 2-way", 2),
            ("Perfboard 70x90mm", 1),
        ]:
            ProjectPart.objects.create(
                project=weather, part=parts[key], qty_allocated=qty
            )

        # 2. On the bench, with parts already handed back mid-build.
        parking = Project.objects.create(
            user=user,
            name="Garage parking sensor",
            description=(
                "Ultrasonic distance to a traffic light of LEDs so I stop "
                "hitting the shelf. Breadboard for now."
            ),
        )
        for key, qty in [
            ("ESP32 devkit v1", 1),
            ("HC-SR04 ultrasonic", 2),
            ("SSD1306 OLED 128x64", 1),
            ("LED red 5mm", 2),
            ("LED green 5mm", 2),
            ("Resistor 220R", 4),
            ("Piezo buzzer", 1),
            ("Pin header 40-way", 1),
        ]:
            ProjectPart.objects.create(
                project=parking, part=parts[key], qty_allocated=qty
            )
        # Second ultrasonic turned out to be unnecessary - back on the shelf.
        line = parking.lines.get(part=parts["HC-SR04 ultrasonic"])
        line.qty_returned = 1
        line.save(update_fields=["qty_returned"])

        # 3. Torn down, with real losses. This is the interesting one.
        balancer = Project.objects.create(
            user=user,
            name="Self-balancing robot",
            description=(
                "IMU plus two steppers on a perfboard chassis. Balanced for "
                "about four seconds, then the regulator let the smoke out and "
                "took a stepper driver with it."
            ),
        )
        outcomes = []
        for key, qty, returned, soldered, broken in [
            ("MPU-6050 6-axis IMU", 1, 1, 0, 0),
            ("Arduino Nano", 1, 0, 1, 0),
            ("28BYJ-48 stepper + driver", 2, 1, 0, 1),
            ("AMS1117-3.3 regulator", 2, 0, 1, 1),
            ("Electrolytic capacitor 1000uF", 2, 1, 1, 0),
            ("Resistor 10k", 4, 4, 0, 0),
            ("IRLZ44N MOSFET", 2, 1, 0, 1),
            ("Perfboard 70x90mm", 1, 0, 1, 0),
        ]:
            line = ProjectPart.objects.create(
                project=balancer, part=parts[key], qty_allocated=qty
            )
            outcomes.append((line, returned, soldered, broken))
        balancer.tear_down(outcomes)

        # 4. Torn down clean - everything came back.
        matrix = Project.objects.create(
            user=user,
            name="Desk lamp colour test",
            description=(
                "Breadboarded a WS2812B strip to work out the colour curve. "
                "Never meant to survive, nothing soldered."
            ),
        )
        outcomes = []
        for key, qty in [
            ("Raspberry Pi Pico", 1),
            ("WS2812B LED strip 1m", 1),
            ("Rotary encoder KY-040", 1),
            ("Resistor 1k", 2),
            ("Electrolytic capacitor 1000uF", 1),
            ("Pin header 40-way", 1),
        ]:
            line = ProjectPart.objects.create(
                project=matrix, part=parts[key], qty_allocated=qty
            )
            outcomes.append((line, qty, 0, 0))
        matrix.tear_down(outcomes)

        committed = [
            p for p in Part.objects.filter(user=user).with_availability()
            if p.available == 0
        ]
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {username}: {len(parts)} parts, "
                f"{Project.objects.filter(user=user).count()} projects "
                f"(2 on the bench, 2 torn down), "
                f"{len(committed)} part(s) fully committed."
            )
        )
