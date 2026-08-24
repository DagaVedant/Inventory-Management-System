from datetime import UTC, datetime

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse, reverse_lazy

from .forms import parse_parts_text
from .models import (
    MovementReason,
    Part,
    Project,
    ProjectPart,
    ProjectStatus,
    match_key,
)

User = get_user_model()


class ClearsThrottle:
    def setUp(self):
        cache.clear()
        super().setUp()


class BaseCase(ClearsThrottle, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser("owner", "o@example.com", "pw12345!")
        self.client = Client()
        self.client.force_login(self.user)

    def part(self, name="10k resistor", qty=10, **kw):
        return Part.objects.create(user=self.user, name=name, qty_owned=qty, **kw)

    def project(self, name="Weather Station", **kw):
        return Project.objects.create(user=self.user, name=name, **kw)


class AvailabilityTests(BaseCase):
    def test_worked_example_from_the_plan(self):
        p = self.part(qty=10)
        proj = self.project()
        self.assertEqual(p.compute_available(), 10)

        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        self.assertEqual(p.compute_held(), 4)
        self.assertEqual(p.compute_available(), 6)

        line.qty_returned = 1
        line.save()
        self.assertEqual(p.compute_available(), 7)

        proj.tear_down([(line, 0, 2, 1)])
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 7)
        self.assertEqual(p.compute_held(), 0)
        self.assertEqual(p.compute_available(), 7)

    def test_annotation_matches_per_object_computation(self):
        p = self.part(qty=10)
        ProjectPart.objects.create(project=self.project(), part=p, qty_allocated=4)
        annotated = Part.objects.with_availability().get(pk=p.pk)
        self.assertEqual(annotated.held, p.compute_held())
        self.assertEqual(annotated.available, p.compute_available())

    def test_archived_projects_hold_nothing(self):
        p = self.part(qty=10)
        proj = self.project(status=ProjectStatus.ARCHIVED)
        ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        self.assertEqual(p.compute_held(), 0)
        self.assertEqual(p.compute_available(), 10)

    def test_part_with_no_allocations_has_zero_held(self):
        self.assertEqual(self.part(qty=3).compute_held(), 0)


class ConstraintTests(BaseCase):
    def test_cannot_account_for_more_than_allocated(self):
        p, proj = self.part(), self.project()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectPart.objects.create(
                project=proj, part=p, qty_allocated=1, qty_returned=5
            )

    def test_a_line_must_want_something(self):
        p, proj = self.part(), self.project()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectPart.objects.create(
                project=proj, part=p, qty_wanted=0, qty_allocated=0
            )

    def test_allocating_nothing_is_fine_when_you_wanted_something(self):
        p, proj = self.part(qty=0), self.project()
        line = ProjectPart.objects.create(
            project=proj, part=p, qty_wanted=3, qty_allocated=0
        )
        self.assertEqual(line.short, 3)
        self.assertEqual(p.compute_held(), 0)

    def test_cannot_allocate_more_than_was_wanted(self):
        p, proj = self.part(), self.project()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectPart.objects.create(
                project=proj, part=p, qty_wanted=2, qty_allocated=5
            )

    def test_one_line_per_part_per_project(self):
        p, proj = self.part(), self.project()
        ProjectPart.objects.create(project=proj, part=p, qty_allocated=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectPart.objects.create(project=proj, part=p, qty_allocated=1)

    def test_qty_owned_cannot_drop_below_held(self):
        p = self.part(qty=10)
        ProjectPart.objects.create(project=self.project(), part=p, qty_allocated=6)
        p.qty_owned = 5
        with self.assertRaises(ValidationError):
            p.full_clean()


class AllocationGuardTests(BaseCase):
    def test_blocks_allocating_more_than_available(self):
        p = self.part(qty=10)
        ProjectPart.objects.create(project=self.project("A"), part=p, qty_allocated=4)
        with self.assertRaises(ValidationError):
            ProjectPart(project=self.project("B"), part=p, qty_allocated=7).full_clean()

    def test_line_does_not_count_against_itself_when_edited(self):
        p = self.part(qty=10)
        line = ProjectPart.objects.create(
            project=self.project("A"), part=p, qty_allocated=10
        )
        line.qty_allocated = 10
        line.full_clean()
        line.qty_allocated = 8
        line.full_clean()
        line.qty_allocated = 11
        with self.assertRaises(ValidationError):
            line.full_clean()

    def test_returning_parts_frees_capacity(self):
        p = self.part(qty=10)
        line = ProjectPart.objects.create(
            project=self.project("A"), part=p, qty_allocated=10
        )
        with self.assertRaises(ValidationError):
            ProjectPart(project=self.project("B"), part=p, qty_allocated=5).full_clean()

        line.qty_returned = 6
        line.save()
        ProjectPart(project=self.project("C"), part=p, qty_allocated=5).full_clean()


class TeardownTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.p = self.part(qty=10)
        self.proj = self.project()
        self.line = ProjectPart.objects.create(
            project=self.proj, part=self.p, qty_allocated=4
        )
        self.url = reverse("project_teardown", args=[self.proj.pk])

    def post(self, rows):
        data = {
            "form-TOTAL_FORMS": str(len(rows)),
            "form-INITIAL_FORMS": str(len(rows)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        for i, (line, returned, soldered, broken) in enumerate(rows):
            data[f"form-{i}-line_id"] = str(line.pk)
            data[f"form-{i}-qty_returned"] = str(returned)
            data[f"form-{i}-qty_soldered"] = str(soldered)
            data[f"form-{i}-qty_broken"] = str(broken)
        return self.client.post(self.url, data)

    def test_page_prefills_returned_with_everything_still_held(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10k resistor")
        self.assertContains(response, 'value="4"')

    def test_lines_must_add_up_exactly(self):
        response = self.post([(self.line, 1, 1, 0)])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Account for exactly 4")
        self.proj.refresh_from_db()
        self.p.refresh_from_db()
        self.assertEqual(self.proj.status, ProjectStatus.ACTIVE)
        self.assertEqual(self.p.qty_owned, 10)

    def test_soldered_and_broken_leave_inventory_permanently(self):
        self.post([(self.line, 1, 2, 1)])
        self.proj.refresh_from_db()
        self.p.refresh_from_db()
        self.assertEqual(self.proj.status, ProjectStatus.ARCHIVED)
        self.assertIsNotNone(self.proj.archived_at)
        self.assertEqual(self.p.qty_owned, 7)
        self.assertEqual(self.p.compute_available(), 7)

    def test_returning_everything_leaves_inventory_untouched(self):
        self.post([(self.line, 4, 0, 0)])
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_owned, 10)
        self.assertEqual(self.p.compute_available(), 10)

    def test_partial_teardown_is_rejected_when_a_line_is_missing(self):
        other = self.part("DHT22", qty=5)
        ProjectPart.objects.create(project=self.proj, part=other, qty_allocated=2)
        response = self.post([(self.line, 4, 0, 0)])
        self.assertEqual(response.status_code, 200)
        self.proj.refresh_from_db()
        self.assertEqual(self.proj.status, ProjectStatus.ACTIVE)

    def test_cannot_tear_down_twice(self):
        self.post([(self.line, 4, 0, 0)])
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_empty_project_has_nothing_to_tear_down(self):
        empty = self.project("Nothing here")
        url = reverse("project_teardown", args=[empty.pk])
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_a_failed_teardown_changes_nothing_at_all(self):
        other = self.part("DHT22", qty=5)
        line2 = ProjectPart.objects.create(
            project=self.proj, part=other, qty_allocated=2
        )
        self.post([(self.line, 4, 0, 0), (line2, 1, 0, 0)])
        self.line.refresh_from_db()
        self.p.refresh_from_db()
        self.proj.refresh_from_db()
        self.assertEqual(self.line.qty_returned, 0)
        self.assertEqual(self.p.qty_owned, 10)
        self.assertEqual(self.proj.status, ProjectStatus.ACTIVE)


class ScopingTests(ClearsThrottle, TestCase):
    def test_users_cannot_touch_each_others_projects(self):
        alice = User.objects.create_superuser("alice", "a@e.com", "pw12345!")
        bob = User.objects.create_user("bob", "b@e.com", "pw12345!", is_staff=True)
        part = Part.objects.create(user=alice, name="10k", qty_owned=5)
        proj = Project.objects.create(user=alice, name="a build by alice")
        ProjectPart.objects.create(project=proj, part=part, qty_allocated=1)

        client = Client()
        client.force_login(bob)
        url = reverse("project_teardown", args=[proj.pk])
        self.assertIn(client.get(url).status_code, (302, 403, 404))


class AdminSmokeTests(BaseCase):
    def test_every_admin_page_renders(self):
        p = self.part(value="10k", package="0805")
        proj = self.project()
        ProjectPart.objects.create(project=proj, part=p, qty_allocated=2)
        for url in [
            "/admin/",
            "/admin/core/part/",
            "/admin/core/part/?q=10k",
            "/admin/core/part/?o=6",
            "/admin/core/part/add/",
            f"/admin/core/part/{p.pk}/change/",
            "/admin/core/project/",
            "/admin/core/project/add/",
            f"/admin/core/project/{proj.pk}/change/",
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class ViewTests(BaseCase):
    def test_everything_requires_login(self):
        anon = Client()
        p = self.part()
        proj = self.project()
        for name, args in [
            ("part_list", []),
            ("part_create", []),
            ("part_update", [p.pk]),
            ("part_delete", [p.pk]),
            ("project_list", []),
            ("project_create", []),
            ("project_detail", [proj.pk]),
            ("project_teardown", [proj.pk]),
        ]:
            with self.subTest(name=name):
                response = anon.get(reverse(name, args=args))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_part_list_shows_availability(self):
        p = self.part(qty=10)
        ProjectPart.objects.create(project=self.project(), part=p, qty_allocated=4)
        response = self.client.get(reverse("part_list"))
        self.assertContains(response, "10k resistor")
        self.assertEqual(response.context["parts"][0].held, 4)
        self.assertEqual(response.context["parts"][0].available, 6)

    def test_part_search_matches_tags_and_value(self):
        self.part("DHT22", qty=2, tags="sensor, temperature")
        self.part("Resistor", qty=99, value="10k")
        url = reverse("part_list")
        self.assertContains(self.client.get(url, {"q": "temperature"}), "DHT22")
        self.assertNotContains(self.client.get(url, {"q": "temperature"}), "Resistor")
        self.assertContains(self.client.get(url, {"q": "10k"}), "Resistor")

    def test_part_list_only_shows_your_own_parts(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        Part.objects.create(user=stranger, name="Not yours", qty_owned=1)
        self.part("Mine")
        response = self.client.get(reverse("part_list"))
        self.assertContains(response, "Mine")
        self.assertNotContains(response, "Not yours")

    def test_save_and_add_another_returns_to_an_empty_form(self):
        response = self.client.post(
            reverse("part_create"),
            {
                "name": "DHT22",
                "qty_owned": 3,
                "value": "",
                "package": "",
                "pin_count": "",
                "voltage": "",
                "tags": "",
                "notes": "",
                "_addanother": "1",
            },
        )
        self.assertRedirects(response, reverse("part_create"))
        self.assertTrue(Part.objects.filter(name="DHT22").exists())

    def test_created_part_belongs_to_you(self):
        self.client.post(
            reverse("part_create"),
            {
                "name": "DHT22",
                "qty_owned": 3,
                "value": "",
                "package": "",
                "pin_count": "",
                "voltage": "",
                "tags": "",
                "notes": "",
            },
        )
        self.assertEqual(Part.objects.get(name="DHT22").user, self.user)

    def test_cannot_delete_a_part_a_project_names(self):
        p = self.part()
        ProjectPart.objects.create(project=self.project(), part=p, qty_allocated=1)
        self.client.post(reverse("part_delete", args=[p.pk]))
        self.assertTrue(Part.objects.filter(pk=p.pk).exists())

    def test_unused_part_can_be_deleted(self):
        p = self.part()
        self.client.post(reverse("part_delete", args=[p.pk]))
        self.assertFalse(Part.objects.filter(pk=p.pk).exists())


class AllocationViewTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.p = self.part(qty=10)
        self.proj = self.project()
        self.url = reverse("project_detail", args=[self.proj.pk])

    def allocate(self, qty, part=None):
        return self.client.post(
            self.url,
            {"part": (part or self.p).pk, "qty_wanted": qty, "note": ""},
        )

    def test_allocating_reduces_available_but_not_owned(self):
        self.allocate(4)
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_owned, 10)
        self.assertEqual(self.p.compute_available(), 6)
        self.assertEqual(self.proj.lines.first().short, 0)

    def test_asking_for_more_than_exists_takes_what_there_is(self):
        self.allocate(14)
        line = self.proj.lines.first()
        self.assertEqual(line.qty_wanted, 14)
        self.assertEqual(line.qty_allocated, 10)
        self.assertEqual(line.short, 4)
        self.p.refresh_from_db()
        self.assertEqual(self.p.compute_available(), 0)

    def test_a_part_you_have_none_of_is_pure_shortfall(self):
        empty = self.part("Nothing left", qty=0)
        self.allocate(3, part=empty)
        line = self.proj.lines.get(part=empty)
        self.assertEqual(line.qty_allocated, 0)
        self.assertEqual(line.short, 3)

    def test_allocating_the_same_part_twice_tops_up_the_line(self):
        self.allocate(2)
        self.allocate(3)
        self.assertEqual(self.proj.lines.count(), 1)
        line = self.proj.lines.first()
        self.assertEqual(line.qty_wanted, 5)
        self.assertEqual(line.qty_allocated, 5)

    def test_topping_up_past_what_exists_records_the_rest_as_short(self):
        self.allocate(8)
        self.allocate(5)
        line = self.proj.lines.first()
        self.assertEqual(line.qty_wanted, 13)
        self.assertEqual(line.qty_allocated, 10)
        self.assertEqual(line.short, 3)

    def test_another_project_cannot_take_what_this_one_holds(self):
        self.allocate(10)
        other = self.project("Second build")
        self.client.post(
            reverse("project_detail", args=[other.pk]),
            {"part": self.p.pk, "qty_wanted": 4, "note": ""},
        )
        line = other.lines.first()
        self.assertEqual(line.qty_allocated, 0)
        self.assertEqual(line.short, 4)

    def test_returning_early_frees_parts_without_teardown(self):
        line = ProjectPart.objects.create(
            project=self.proj, part=self.p, qty_allocated=4
        )
        self.client.post(
            reverse("line_return", args=[self.proj.pk, line.pk]), {"qty": 3}
        )
        line.refresh_from_db()
        self.assertEqual(line.qty_returned, 3)
        self.assertEqual(self.p.compute_available(), 9)

    def test_cannot_return_more_than_is_held(self):
        line = ProjectPart.objects.create(
            project=self.proj, part=self.p, qty_allocated=4
        )
        self.client.post(
            reverse("line_return", args=[self.proj.pk, line.pk]), {"qty": 99}
        )
        line.refresh_from_db()
        self.assertEqual(line.qty_returned, 0)

    def test_removing_an_untouched_line_un_allocates_it(self):
        line = ProjectPart.objects.create(
            project=self.proj, part=self.p, qty_allocated=4
        )
        self.client.post(reverse("line_remove", args=[self.proj.pk, line.pk]))
        self.assertEqual(self.proj.lines.count(), 0)
        self.assertEqual(self.p.compute_available(), 10)

    def test_cannot_remove_a_line_with_history(self):
        line = ProjectPart.objects.create(
            project=self.proj, part=self.p, qty_allocated=4, qty_returned=1
        )
        self.client.post(reverse("line_remove", args=[self.proj.pk, line.pk]))
        self.assertEqual(self.proj.lines.count(), 1)

    def test_cannot_allocate_to_an_archived_project(self):
        self.proj.status = ProjectStatus.ARCHIVED
        self.proj.save()
        self.allocate(1)
        self.assertEqual(self.proj.lines.count(), 0)

    def test_part_picker_only_offers_your_own_parts(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        theirs = Part.objects.create(user=stranger, name="Not yours", qty_owned=5)
        response = self.client.post(
            self.url, {"part": theirs.pk, "qty_wanted": 1, "note": ""}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.proj.lines.count(), 0)


class TeardownViewTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.p = self.part(qty=10)
        self.proj = self.project()
        self.line = ProjectPart.objects.create(
            project=self.proj, part=self.p, qty_allocated=4
        )
        self.url = reverse("project_teardown", args=[self.proj.pk])

    def post(self, rows):
        data = {
            "form-TOTAL_FORMS": str(len(rows)),
            "form-INITIAL_FORMS": str(len(rows)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        for i, (line, returned, soldered, broken) in enumerate(rows):
            data[f"form-{i}-line_id"] = str(line.pk)
            data[f"form-{i}-qty_returned"] = str(returned)
            data[f"form-{i}-qty_soldered"] = str(soldered)
            data[f"form-{i}-qty_broken"] = str(broken)
        return self.client.post(self.url, data)

    def test_page_renders_with_defaults(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10k resistor")
        self.assertContains(response, 'value="4"')

    def test_teardown_archives_and_settles_the_numbers(self):
        self.post([(self.line, 1, 2, 1)])
        self.proj.refresh_from_db()
        self.p.refresh_from_db()
        self.assertEqual(self.proj.status, ProjectStatus.ARCHIVED)
        self.assertEqual(self.p.qty_owned, 7)
        self.assertEqual(self.p.compute_available(), 7)

    def test_mismatched_line_is_rejected_and_nothing_changes(self):
        response = self.post([(self.line, 1, 1, 0)])
        self.assertContains(response, "Account for exactly 4")
        self.proj.refresh_from_db()
        self.assertEqual(self.proj.status, ProjectStatus.ACTIVE)

    def test_another_users_project_is_not_reachable(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        theirs = Project.objects.create(user=stranger, name="Theirs")
        response = self.client.get(reverse("project_teardown", args=[theirs.pk]))
        self.assertEqual(response.status_code, 404)

    def test_a_foreign_line_id_reveals_nothing_about_it(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        their_part = Part.objects.create(user=stranger, name="Secret", qty_owned=99)
        their_project = Project.objects.create(user=stranger, name="Secret build")
        their_line = ProjectPart.objects.create(
            project=their_project, part=their_part, qty_allocated=37
        )

        response = self.post([(their_line, 0, 0, 0)])
        body = response.content.decode()
        self.assertNotIn("37", body)
        self.assertNotIn("Account for exactly", body)
        self.assertContains(response, "no longer exists")

        their_line.refresh_from_db()
        their_project.refresh_from_db()
        self.assertEqual(their_line.qty_returned, 0)
        self.assertEqual(their_project.status, ProjectStatus.ACTIVE)


class SignupTests(ClearsThrottle, TestCase):
    url = reverse_lazy("signup")

    def credentials(self, **extra):
        data = {
            "username": "newcomer",
            "password1": "a-long-enough-passphrase",
            "password2": "a-long-enough-passphrase",
        }
        data.update(extra)
        return data

    def test_page_renders(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create an account")

    def test_login_page_offers_signup(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, reverse("signup"))

    def test_signing_up_creates_the_account_and_logs_you_straight_in(self):
        response = self.client.post(self.url, self.credentials())
        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(User.objects.filter(username="newcomer").exists())
        self.assertEqual(
            int(self.client.session["_auth_user_id"]),
            User.objects.get(username="newcomer").pk,
        )

    def test_a_new_account_starts_empty(self):
        owner = User.objects.create_user("owner", "o@e.com", "pw12345!")
        Part.objects.create(user=owner, name="Someone else's DHT22", qty_owned=3)
        self.client.post(self.url, self.credentials())
        response = self.client.get(reverse("part_list"))
        self.assertEqual(len(response.context["parts"]), 0)
        self.assertNotContains(response, "Someone else")

    def test_mismatched_passwords_are_rejected(self):
        response = self.client.post(
            self.url, self.credentials(password2="something-else-entirely")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newcomer").exists())

    def test_already_logged_in_users_are_sent_to_the_app(self):
        user = User.objects.create_user("owner", "o@e.com", "pw12345!")
        self.client.force_login(user)
        self.assertRedirects(self.client.get(self.url), reverse("dashboard"))

    def test_no_code_field_when_signup_is_open(self):
        self.assertNotContains(self.client.get(self.url), "Invite code")

    @override_settings(SIGNUP_CODE="letmein")
    def test_invite_code_is_required_when_set(self):
        self.assertContains(self.client.get(self.url), "Invite code")

        response = self.client.post(self.url, self.credentials())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newcomer").exists())

        response = self.client.post(self.url, self.credentials(signup_code="wrong"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newcomer").exists())

        response = self.client.post(self.url, self.credentials(signup_code="letmein"))
        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(User.objects.filter(username="newcomer").exists())


class ProjectDeleteTests(BaseCase):
    def test_deleting_an_active_project_hands_its_parts_back(self):
        p = self.part(qty=10)
        proj = self.project()
        ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        self.assertEqual(p.compute_available(), 6)

        response = self.client.post(reverse("project_delete", args=[proj.pk]))
        self.assertRedirects(response, reverse("project_list"))
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 10)
        self.assertEqual(p.compute_available(), 10)
        self.assertFalse(Project.objects.filter(pk=proj.pk).exists())

    def test_deleting_a_torn_down_project_leaves_quantities_alone(self):
        p = self.part(qty=10)
        proj = self.project()
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        proj.tear_down([(line, 1, 2, 1)])
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 7)

        self.client.post(reverse("project_delete", args=[proj.pk]))
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 7)
        self.assertEqual(p.compute_available(), 7)

    def test_confirmation_page_says_what_will_happen(self):
        p = self.part(qty=10)
        proj = self.project()
        ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        response = self.client.get(reverse("project_delete", args=[proj.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "straight back to the shelf")
        self.assertContains(response, "10k resistor")

    def test_cannot_delete_someone_elses_project(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        theirs = Project.objects.create(user=stranger, name="Theirs")
        response = self.client.post(reverse("project_delete", args=[theirs.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Project.objects.filter(pk=theirs.pk).exists())

    def test_delete_requires_login(self):
        proj = self.project()
        response = Client().get(reverse("project_delete", args=[proj.pk]))
        self.assertEqual(response.status_code, 302)


class PresentationTests(BaseCase):
    def test_dates_render_in_the_configured_zone_not_utc(self):
        proj = self.project()
        Project.objects.filter(pk=proj.pk).update(
            created_at=datetime(2026, 8, 20, 1, 30, tzinfo=UTC)
        )
        response = self.client.get(reverse("project_list"))
        self.assertContains(response, "19 Aug 2026")
        self.assertNotContains(response, "20 Aug 2026")

    def test_pages_declare_a_favicon(self):
        response = self.client.get(reverse("part_list"))
        self.assertContains(response, "favicon.svg")


class ParserTests(TestCase):
    def test_name_and_quantity_are_enough(self):
        rows, errors = parse_parts_text("DHT22, 4")
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["name"], "DHT22")
        self.assertEqual(rows[0]["qty_owned"], 4)

    def test_everything_after_the_fourth_field_becomes_tags(self):
        rows, _ = parse_parts_text(
            "Resistor, 180, 10k, through-hole, passive, resistor, cheap"
        )
        self.assertEqual(rows[0]["value"], "10k")
        self.assertEqual(rows[0]["package"], "through-hole")
        self.assertEqual(rows[0]["tags"], "passive, resistor, cheap")

    def test_blank_lines_and_comments_are_skipped(self):
        rows, errors = parse_parts_text("# my bin\n\nDHT22, 4\n\n  \n# end")
        self.assertEqual(len(rows), 1)
        self.assertEqual(errors, [])

    def test_errors_carry_the_line_number(self):
        rows, errors = parse_parts_text("DHT22, 4\nBMP280\nLED, lots")
        self.assertEqual(len(rows), 1)
        self.assertEqual([number for number, _ in errors], [2, 3])
        self.assertIn("name and a quantity", errors[0][1])
        self.assertIn("whole number", errors[1][1])

    def test_negative_quantity_is_rejected(self):
        _, errors = parse_parts_text("DHT22, -4")
        self.assertIn("negative", errors[0][1])

    def test_zero_quantity_is_allowed(self):
        rows, errors = parse_parts_text("DHT22, 0")
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["qty_owned"], 0)

    def test_duplicate_lines_point_at_each_other(self):
        _, errors = parse_parts_text("Resistor, 10, 10k\nResistor, 5, 10k")
        self.assertIn("line 1", errors[0][1])

    def test_same_name_different_value_is_not_a_duplicate(self):
        rows, errors = parse_parts_text("Resistor, 10, 10k\nResistor, 5, 220R")
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 2)


class BulkImportTests(BaseCase):
    url = reverse_lazy("part_import")

    def test_imports_every_line(self):
        response = self.client.post(
            self.url, {"text": "DHT22, 4\nBMP280, 2, , module\nResistor, 180, 10k"}
        )
        self.assertRedirects(response, reverse("part_list"))
        self.assertEqual(Part.objects.filter(user=self.user).count(), 3)
        self.assertEqual(Part.objects.get(name="Resistor").value, "10k")

    def test_imported_parts_belong_to_you(self):
        self.client.post(self.url, {"text": "DHT22, 4"})
        self.assertEqual(Part.objects.get(name="DHT22").user, self.user)

    def test_one_bad_line_rejects_the_whole_paste(self):
        response = self.client.post(
            self.url, {"text": "DHT22, 4\nBMP280, heaps\nResistor, 180"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Line 2")
        self.assertEqual(Part.objects.filter(user=self.user).count(), 0)

    def test_clashing_with_an_existing_part_is_refused(self):
        self.part("DHT22", qty=1)
        response = self.client.post(self.url, {"text": "DHT22, 4"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "you already")
        self.assertEqual(Part.objects.filter(name="DHT22").count(), 1)

    def test_a_near_duplicate_is_caught_too(self):
        self.part("Resistor", qty=100, value="10k")
        response = self.client.post(self.url, {"text": "resistor, 50, 10 K"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "you already")
        self.assertEqual(Part.objects.filter(user=self.user).count(), 1)

    def test_genuinely_different_values_still_import(self):
        self.part("Resistor", qty=100, value="4.7k")
        response = self.client.post(self.url, {"text": "Resistor, 50, 47k"})
        self.assertRedirects(response, reverse("part_list"))
        self.assertEqual(Part.objects.filter(user=self.user).count(), 2)

    def test_near_duplicates_within_one_paste_are_caught(self):
        response = self.client.post(
            self.url, {"text": "Resistor, 10, 10k\nresistor, 5, 10 K"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Same part as line 1")
        self.assertEqual(Part.objects.filter(user=self.user).count(), 0)

    def test_someone_elses_part_of_the_same_name_is_not_a_clash(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        Part.objects.create(user=stranger, name="DHT22", qty_owned=9)
        response = self.client.post(self.url, {"text": "DHT22, 4"})
        self.assertRedirects(response, reverse("part_list"))
        self.assertEqual(Part.objects.filter(name="DHT22").count(), 2)

    def test_import_requires_login(self):
        self.assertEqual(Client().get(self.url).status_code, 302)


class PartDetailTests(BaseCase):
    def test_shows_who_is_holding_it_and_what_ate_it(self):
        p = self.part(qty=10)
        live = self.project("On the bench")
        ProjectPart.objects.create(project=live, part=p, qty_allocated=3)

        dead = self.project("Finished build")
        line = ProjectPart.objects.create(project=dead, part=p, qty_allocated=4)
        dead.tear_down([(line, 1, 2, 1)])

        response = self.client.get(reverse("part_detail", args=[p.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "On the bench")
        self.assertContains(response, "Finished build")
        self.assertEqual(response.context["total_lost"], 3)
        self.assertEqual(len(response.context["holding"]), 1)
        self.assertEqual(len(response.context["history"]), 1)

    def test_reports_the_derived_numbers(self):
        p = self.part(qty=10)
        ProjectPart.objects.create(project=self.project(), part=p, qty_allocated=4)
        part = self.client.get(reverse("part_detail", args=[p.pk])).context["part"]
        self.assertEqual(part.held, 4)
        self.assertEqual(part.available, 6)

    def test_a_fully_returned_line_is_not_listed_as_held(self):
        p = self.part(qty=10)
        proj = self.project()
        ProjectPart.objects.create(
            project=proj, part=p, qty_allocated=4, qty_returned=4
        )
        response = self.client.get(reverse("part_detail", args=[p.pk]))
        self.assertEqual(response.context["holding"], [])

    def test_you_cannot_view_someone_elses_part(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        theirs = Part.objects.create(user=stranger, name="Not yours", qty_owned=1)
        response = self.client.get(reverse("part_detail", args=[theirs.pk]))
        self.assertEqual(response.status_code, 404)

    def test_parts_list_links_to_the_detail_page(self):
        p = self.part()
        response = self.client.get(reverse("part_list"))
        self.assertContains(response, reverse("part_detail", args=[p.pk]))


class AddStockTests(BaseCase):
    def test_adding_stock_raises_owned_and_available(self):
        p = self.part(qty=10)
        ProjectPart.objects.create(project=self.project(), part=p, qty_allocated=4)
        self.client.post(reverse("part_add_stock", args=[p.pk]), {"qty": 50})
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 60)
        self.assertEqual(p.compute_available(), 56)

    def test_zero_and_negative_are_refused(self):
        p = self.part(qty=10)
        for bad in (0, -5):
            self.client.post(reverse("part_add_stock", args=[p.pk]), {"qty": bad})
            p.refresh_from_db()
            self.assertEqual(p.qty_owned, 10)

    def test_get_is_not_allowed(self):
        p = self.part()
        self.assertEqual(
            self.client.get(reverse("part_add_stock", args=[p.pk])).status_code, 405
        )

    def test_you_cannot_add_stock_to_someone_elses_part(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        theirs = Part.objects.create(user=stranger, name="Not yours", qty_owned=1)
        response = self.client.post(
            reverse("part_add_stock", args=[theirs.pk]), {"qty": 5}
        )
        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.qty_owned, 1)


class ImportTopUpTests(BaseCase):
    url = reverse_lazy("part_import")

    def test_without_the_box_an_existing_part_is_still_refused(self):
        self.part("DHT22", qty=4)
        response = self.client.post(self.url, {"text": "DHT22, 6"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tick the box")
        self.assertEqual(Part.objects.get(name="DHT22").qty_owned, 4)

    def test_with_the_box_the_quantity_is_added(self):
        p = self.part("DHT22", qty=4)
        response = self.client.post(self.url, {"text": "DHT22, 6", "top_up": "on"})
        self.assertRedirects(response, reverse("part_list"))
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 10)
        self.assertEqual(Part.objects.filter(user=self.user).count(), 1)

    def test_a_top_up_lands_in_the_ledger_as_a_delivery(self):
        p = self.part("DHT22", qty=4)
        self.client.post(self.url, {"text": "DHT22, 6", "top_up": "on"})
        movement = p.movements.first()
        self.assertEqual(movement.reason, MovementReason.PURCHASE)
        self.assertEqual(movement.delta, 6)

    def test_a_top_up_clears_the_shopping_list(self):
        p = self.part("DHT22", qty=0)
        Part.objects.filter(pk=p.pk).update(qty_to_buy=6)
        self.client.post(self.url, {"text": "DHT22, 6", "top_up": "on"})
        p.refresh_from_db()
        self.assertEqual(p.qty_to_buy, 0)

    def test_one_paste_can_create_and_top_up_at_once(self):
        self.part("DHT22", qty=4)
        response = self.client.post(
            self.url, {"text": "DHT22, 6\nBMP280, 3", "top_up": "on"}
        )
        self.assertRedirects(response, reverse("part_list"))
        self.assertEqual(Part.objects.get(name="DHT22").qty_owned, 10)
        self.assertEqual(Part.objects.get(name="BMP280").qty_owned, 3)

    def test_a_near_duplicate_tops_up_the_part_it_matches(self):
        p = self.part("Resistor", qty=100, value="10k")
        self.client.post(self.url, {"text": "resistor, 50, 10 K", "top_up": "on"})
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 150)
        self.assertEqual(Part.objects.filter(user=self.user).count(), 1)


class ImportLimitsTests(BaseCase):
    url = reverse_lazy("part_import")

    def test_an_over_long_value_is_reported_not_truncated(self):
        response = self.client.post(self.url, {"text": f"Thing, 5, {'x' * 60}"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "value too long")
        self.assertEqual(Part.objects.filter(user=self.user).count(), 0)

    def test_an_over_long_package_is_reported(self):
        response = self.client.post(self.url, {"text": f"Thing, 5, 10k, {'x' * 60}"})
        self.assertContains(response, "package too long")

    def test_over_long_tags_are_reported(self):
        tags = ", ".join(f"tag{i}" for i in range(40))
        response = self.client.post(self.url, {"text": f"Thing, 5, 10k, DIP, {tags}"})
        self.assertContains(response, "tags too long")

    def test_the_line_number_is_given(self):
        response = self.client.post(self.url, {"text": f"Fine, 1\nBad, 5, {'x' * 60}"})
        self.assertContains(response, "Line 2")


class BackupTests(BaseCase):
    def test_the_dump_carries_parts_projects_and_history(self):
        from io import StringIO

        from django.core.management import call_command

        p = self.part(qty=10)
        proj = self.project()
        ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        p.receive(5)

        out = StringIO()
        call_command("backup", stdout=out, to_stdout=True)

        import json

        models = {row["model"] for row in json.loads(out.getvalue())}
        for expected in [
            "core.part",
            "core.project",
            "core.projectpart",
            "core.stockmovement",
            "auth.user",
        ]:
            with self.subTest(model=expected):
                self.assertIn(expected, models)


class DerivedDefaultTests(BaseCase):
    def test_a_line_with_only_an_allocation_wanted_that_much(self):
        p, proj = self.part(), self.project()
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        self.assertEqual(line.qty_wanted, 4)
        self.assertEqual(line.short, 0)

    def test_an_explicit_want_is_never_overwritten(self):
        p, proj = self.part(), self.project()
        line = ProjectPart.objects.create(
            project=proj, part=p, qty_wanted=9, qty_allocated=4
        )
        self.assertEqual(line.qty_wanted, 9)
        self.assertEqual(line.short, 5)

    def test_it_works_when_attributes_are_assigned_after_construction(self):
        p, proj = self.part(), self.project()
        line = ProjectPart(project=proj, part=p)
        line.qty_allocated = 6
        line.save()
        self.assertEqual(line.qty_wanted, 6)

    def test_validating_an_unsaved_line_sees_what_saving_would_write(self):
        p, proj = self.part(), self.project()
        ProjectPart(project=proj, part=p, qty_allocated=5).full_clean()

    def test_the_default_only_applies_on_create(self):
        p, proj = self.part(), self.project()
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        line.qty_allocated = 2
        line.save()
        line.refresh_from_db()
        self.assertEqual(line.qty_wanted, 4)


class ThrottleTests(ClearsThrottle, TestCase):
    def setUp(self):
        super().setUp()
        User.objects.create_user("owner", "o@example.com", "pw12345!")

    def wrong_password(self):
        return self.client.post(
            reverse("login"), {"username": "owner", "password": "nope"}
        )

    def test_repeated_wrong_passwords_are_eventually_refused(self):
        for _ in range(10):
            self.assertEqual(self.wrong_password().status_code, 200)
        response = self.wrong_password()
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many attempts", status_code=429)

    def test_logging_in_successfully_clears_the_count(self):
        for _ in range(9):
            self.wrong_password()

        self.client.post(
            reverse("login"), {"username": "owner", "password": "pw12345!"}
        )
        self.client.logout()

        for _ in range(10):
            self.assertEqual(self.wrong_password().status_code, 200)

    def test_a_correct_password_still_works_below_the_limit(self):
        self.wrong_password()
        response = self.client.post(
            reverse("login"), {"username": "owner", "password": "pw12345!"}
        )
        self.assertEqual(response.status_code, 302)

    def test_signups_are_limited_too(self):
        for i in range(5):
            response = self.client.post(
                reverse("signup"),
                {
                    "username": f"newcomer{i}",
                    "password1": "a-long-enough-passphrase",
                    "password2": "a-long-enough-passphrase",
                },
            )
            self.assertEqual(response.status_code, 302)
            self.client.logout()

        response = self.client.post(
            reverse("signup"),
            {
                "username": "onetoomany",
                "password1": "a-long-enough-passphrase",
                "password2": "a-long-enough-passphrase",
            },
        )
        self.assertEqual(response.status_code, 429)
        self.assertFalse(User.objects.filter(username="onetoomany").exists())

    def test_the_pages_themselves_are_never_blocked(self):
        for _ in range(15):
            self.wrong_password()
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)
        self.assertEqual(self.client.get(reverse("signup")).status_code, 200)


class TagTests(BaseCase):
    def test_they_are_normalised_on_save(self):
        p = self.part(tags="  sensor ,,  i2c ,sensor, ")
        p.refresh_from_db()
        self.assertEqual(p.tags, "sensor, i2c")

    def test_duplicates_collapse_regardless_of_case(self):
        p = self.part(tags="Sensor, sensor, SENSOR")
        p.refresh_from_db()
        self.assertEqual(p.tag_list(), ["Sensor"])

    def test_filtering_matches_a_whole_tag_not_a_substring(self):
        wanted = self.part("BMP280", tags="sensor, i2c")
        self.part("Resistor", tags="passive, i2c-pullup")

        response = self.client.get(reverse("part_list"), {"tag": "i2c"})
        self.assertEqual([p.pk for p in response.context["parts"]], [wanted.pk])

    def test_filtering_finds_a_tag_in_any_position(self):
        first = self.part("A", tags="target, other")
        middle = self.part("B", tags="other, target, more")
        last = self.part("C", tags="other, target")
        only = self.part("D", tags="target")
        self.part("E", tags="unrelated")

        response = self.client.get(reverse("part_list"), {"tag": "target"})
        self.assertEqual(
            sorted(p.pk for p in response.context["parts"]),
            sorted([first.pk, middle.pk, last.pk, only.pk]),
        )

    def test_the_filter_survives_sorting(self):
        self.part("A", qty=5, tags="target")
        self.part("B", qty=99, tags="target")
        self.part("C", qty=1, tags="other")
        response = self.client.get(
            reverse("part_list"), {"tag": "target", "sort": "-owned"}
        )
        self.assertEqual([p.name for p in response.context["parts"]], ["B", "A"])

    def test_the_index_counts_every_tag(self):
        self.part("A", tags="sensor, i2c")
        self.part("B", tags="sensor")
        response = self.client.get(reverse("tag_index"))
        self.assertEqual(dict(response.context["tags"]), {"sensor": 2, "i2c": 1})

    def test_renaming_fixes_a_typo_everywhere_at_once(self):
        a = self.part("A", tags="sensr, i2c")
        b = self.part("B", tags="sensr")
        self.client.post(reverse("tag_index"), {"old": "sensr", "new": "sensor"})

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.tag_list(), ["i2c", "sensor"])
        self.assertEqual(b.tag_list(), ["sensor"])

    def test_renaming_onto_an_existing_tag_merges_them(self):
        p = self.part("A", tags="sensr, sensor")
        self.client.post(reverse("tag_index"), {"old": "sensr", "new": "sensor"})
        p.refresh_from_db()
        self.assertEqual(p.tag_list(), ["sensor"])

    def test_an_empty_new_name_removes_the_tag(self):
        p = self.part("A", tags="keep, drop")
        self.client.post(reverse("tag_index"), {"old": "drop", "new": ""})
        p.refresh_from_db()
        self.assertEqual(p.tag_list(), ["keep"])

    def test_renaming_leaves_other_peoples_parts_alone(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        theirs = Part.objects.create(user=stranger, name="Theirs", tags="sensr")
        self.part("Mine", tags="sensr")
        self.client.post(reverse("tag_index"), {"old": "sensr", "new": "sensor"})
        theirs.refresh_from_db()
        self.assertEqual(theirs.tag_list(), ["sensr"])

    def test_the_form_offers_tags_you_already_use(self):
        self.part("A", tags="sensor, i2c")
        response = self.client.get(reverse("part_create"))
        self.assertContains(response, "known-tags")
        self.assertContains(response, 'value="sensor"')

    def test_a_part_page_suggests_others_sharing_a_tag(self):
        p = self.part("BMP280", tags="sensor, i2c")
        twin = self.part("BME280", qty=4, tags="sensor, i2c")
        self.part("Resistor", tags="passive")

        response = self.client.get(reverse("part_detail", args=[p.pk]))
        self.assertEqual([o.pk for o in response.context["similar"]], [twin.pk])
        self.assertContains(response, "Might do instead")

    def test_an_untagged_part_suggests_nothing(self):
        p = self.part("Lonely", tags="")
        self.part("Other", tags="sensor")
        response = self.client.get(reverse("part_detail", args=[p.pk]))
        self.assertEqual(list(response.context["similar"]), [])


class MatchKeyTests(TestCase):
    def test_case_spacing_and_ohms_all_fold_together(self):
        canonical = match_key("Resistor", "10k")
        for value in ["10 k", "10K", "10kΩ", "10 kohms", "10kohm"]:
            with self.subTest(value=value):
                self.assertEqual(match_key("resistor", value), canonical)

    def test_micro_signs_agree(self):
        canonical = match_key("Capacitor", "4.7uF")
        for value in ["4.7µF", "4.7μF"]:
            with self.subTest(value=value):
                self.assertEqual(match_key("Capacitor", value), canonical)

    def test_full_stops_are_kept_so_decimals_stay_distinct(self):
        self.assertNotEqual(match_key("R", "4.7k"), match_key("R", "47k"))

    def test_a_word_containing_ohm_is_not_mangled(self):
        self.assertEqual(match_key("Ohmite"), ("ohmite", ""))

    def test_hyphens_and_spacing_in_names_agree(self):
        self.assertEqual(match_key("Micro servo SG90"), match_key("micro-servo sg90"))


class MergeTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.keep = self.part("Resistor", qty=100, value="10k")
        self.dupe = self.part("resistor", qty=80, value="10 k")

    def test_quantities_and_wants_add_up(self):
        Part.objects.filter(pk=self.dupe.pk).update(qty_to_buy=25)
        self.dupe.refresh_from_db()
        self.dupe.merge_into(self.keep)

        self.keep.refresh_from_db()
        self.assertEqual(self.keep.qty_owned, 180)
        self.assertEqual(self.keep.qty_to_buy, 25)
        self.assertFalse(Part.objects.filter(pk=self.dupe.pk).exists())

    def test_lines_in_the_same_project_are_combined(self):
        proj = self.project("Shared")
        ProjectPart.objects.create(project=proj, part=self.keep, qty_allocated=10)
        ProjectPart.objects.create(project=proj, part=self.dupe, qty_allocated=6)

        self.dupe.merge_into(self.keep)

        self.assertEqual(proj.lines.count(), 1)
        line = proj.lines.get()
        self.assertEqual(line.part, self.keep)
        self.assertEqual(line.qty_allocated, 16)

    def test_lines_elsewhere_simply_move_across(self):
        proj = self.project("Only the duplicate")
        ProjectPart.objects.create(project=proj, part=self.dupe, qty_allocated=4)

        self.dupe.merge_into(self.keep)

        line = proj.lines.get()
        self.assertEqual(line.part, self.keep)
        self.assertEqual(line.qty_allocated, 4)

    def test_history_carries_over_and_still_reconciles(self):
        self.keep.receive(20)
        self.dupe.adjust_stock(-5, MovementReason.CORRECTION)
        self.dupe.merge_into(self.keep)
        self.keep.refresh_from_db()

        ledger = sum(self.keep.movements.values_list("delta", flat=True))
        self.assertEqual(ledger, self.keep.qty_owned)

        newest = self.keep.movements.order_by("created_at", "pk").last()
        self.assertEqual(newest.balance_after, self.keep.qty_owned)

    def test_running_balances_are_recomputed_not_left_stale(self):
        self.keep.receive(20)
        self.dupe.receive(10)
        self.dupe.merge_into(self.keep)
        self.keep.refresh_from_db()

        running = 0
        for movement in self.keep.movements.order_by("created_at", "pk"):
            running += movement.delta
            self.assertEqual(movement.balance_after, running)

    def test_the_merge_itself_is_recorded(self):
        self.dupe.merge_into(self.keep)
        marker = self.keep.movements.filter(reason=MovementReason.MERGE).get()
        self.assertEqual(marker.delta, 0)
        self.assertIn("80 unit(s)", marker.note)

    def test_a_part_cannot_be_merged_into_itself(self):
        with self.assertRaises(ValidationError):
            self.keep.merge_into(self.keep)

    def test_parts_from_different_accounts_cannot_be_merged(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        theirs = Part.objects.create(user=stranger, name="Theirs", qty_owned=5)
        with self.assertRaises(ValidationError):
            self.dupe.merge_into(theirs)

    def test_merging_through_the_page(self):
        response = self.client.post(
            reverse("part_merge", args=[self.dupe.pk]), {"target": self.keep.pk}
        )
        self.assertRedirects(response, reverse("part_detail", args=[self.keep.pk]))
        self.keep.refresh_from_db()
        self.assertEqual(self.keep.qty_owned, 180)

    def test_you_cannot_merge_someone_elses_part(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        theirs = Part.objects.create(user=stranger, name="Theirs", qty_owned=5)
        response = self.client.post(
            reverse("part_merge", args=[theirs.pk]), {"target": self.keep.pk}
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Part.objects.filter(pk=theirs.pk).exists())

    def test_you_cannot_merge_into_someone_elses_part(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        theirs = Part.objects.create(user=stranger, name="Theirs", qty_owned=5)
        response = self.client.post(
            reverse("part_merge", args=[self.dupe.pk]), {"target": theirs.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Part.objects.filter(pk=self.dupe.pk).exists())


class DuplicateFinderTests(BaseCase):
    url = reverse_lazy("part_duplicates")

    def test_groups_parts_that_look_the_same(self):
        self.part("Resistor", qty=10, value="10k")
        self.part("resistor", qty=5, value="10 K")
        self.part("DHT22", qty=2)

        groups = self.client.get(self.url).context["duplicates"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_says_nothing_when_there_is_nothing(self):
        self.part("Resistor", qty=10, value="4.7k")
        self.part("Resistor", qty=10, value="47k")
        response = self.client.get(self.url)
        self.assertEqual(response.context["duplicates"], [])
        self.assertContains(response, "Nothing looks duplicated")

    def test_the_part_page_warns_about_its_twin(self):
        keep = self.part("Resistor", qty=10, value="10k")
        self.part("resistor", qty=5, value="10 K")
        response = self.client.get(reverse("part_detail", args=[keep.pk]))
        self.assertEqual(len(response.context["twins"]), 1)
        self.assertContains(response, "looks like the same component")

    def test_only_your_own_parts_are_compared(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        Part.objects.create(user=stranger, name="Resistor", value="10k", qty_owned=1)
        self.part("Resistor", qty=10, value="10k")
        self.assertEqual(self.client.get(self.url).context["duplicates"], [])


class WantListTests(BaseCase):
    def want(self, part, qty):
        return self.client.post(reverse("part_want", args=[part.pk]), {"qty": qty})

    def test_a_part_can_be_wanted_without_inventing_a_project(self):
        p = self.part(qty=2)
        self.want(p, 20)
        p.refresh_from_db()
        self.assertEqual(p.qty_to_buy, 20)

        response = self.client.get(reverse("dashboard"))
        rows = list(response.context["shortfall"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["wanted"], 20)
        self.assertEqual(rows[0]["from_builds"], 0)
        self.assertEqual(rows[0]["total"], 20)
        self.assertEqual(Project.objects.count(), 0)

    def test_setting_it_to_zero_takes_it_off_the_list(self):
        p = self.part()
        self.want(p, 5)
        self.want(p, 0)
        p.refresh_from_db()
        self.assertEqual(p.qty_to_buy, 0)
        self.assertEqual(
            list(self.client.get(reverse("dashboard")).context["shortfall"]), []
        )

    def test_both_sources_add_up_on_one_row(self):
        p = self.part("DHT22", qty=1)
        ProjectPart.objects.create(
            project=self.project("A build"), part=p, qty_wanted=4, qty_allocated=1
        )
        self.want(p, 10)

        rows = list(self.client.get(reverse("dashboard")).context["shortfall"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["from_builds"], 3)
        self.assertEqual(rows[0]["wanted"], 10)
        self.assertEqual(rows[0]["total"], 13)

    def test_a_delivery_takes_it_back_off_the_list(self):
        p = self.part(qty=2)
        self.want(p, 20)
        self.client.post(reverse("part_add_stock", args=[p.pk]), {"qty": 20})
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 22)
        self.assertEqual(p.qty_to_buy, 0)

    def test_a_partial_delivery_leaves_the_rest_on_the_list(self):
        p = self.part(qty=0)
        self.want(p, 20)
        self.client.post(reverse("part_add_stock", args=[p.pk]), {"qty": 8})
        p.refresh_from_db()
        self.assertEqual(p.qty_to_buy, 12)

    def test_over_delivering_does_not_go_negative(self):
        p = self.part(qty=0)
        self.want(p, 5)
        p.receive(50)
        p.refresh_from_db()
        self.assertEqual(p.qty_to_buy, 0)

    def test_a_teardown_reversal_does_not_clear_the_want_list(self):
        p = self.part(qty=10)
        proj = self.project()
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        proj.tear_down([(line, 0, 3, 1)])
        self.want(p, 6)

        proj.reopen()
        p.refresh_from_db()
        self.assertEqual(p.qty_to_buy, 6)

    def test_you_cannot_want_someone_elses_part(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        theirs = Part.objects.create(user=stranger, name="Not yours", qty_owned=1)
        response = self.want(theirs, 9)
        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.qty_to_buy, 0)

    def test_it_requires_login(self):
        p = self.part()
        self.assertEqual(
            Client().post(reverse("part_want", args=[p.pk]), {"qty": 5}).status_code,
            302,
        )


class ReopenTests(BaseCase):
    def tear_down(self, project, rows):
        project.tear_down(rows)
        project.refresh_from_db()

    def test_a_teardown_can_be_taken_back_completely(self):
        p = self.part(qty=10)
        proj = self.project()
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)

        self.tear_down(proj, [(line, 1, 2, 1)])
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 7)

        proj.reopen()
        proj.refresh_from_db()
        p.refresh_from_db()
        line.refresh_from_db()

        self.assertEqual(proj.status, ProjectStatus.ACTIVE)
        self.assertIsNone(proj.archived_at)
        self.assertEqual(p.qty_owned, 10)
        self.assertEqual(p.compute_held(), 4)
        self.assertEqual(p.compute_available(), 6)
        self.assertEqual(line.qty_soldered, 0)
        self.assertEqual(line.qty_broken, 0)
        self.assertEqual(line.qty_returned, 0)
        self.assertEqual(line.remaining, 4)

    def test_parts_handed_back_mid_build_stay_handed_back(self):
        p = self.part(qty=10)
        proj = self.project()
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=6)

        line.qty_returned = 2
        line.save()

        self.tear_down(proj, [(line, 1, 2, 1)])
        proj.reopen()
        line.refresh_from_db()

        self.assertEqual(line.qty_returned, 2)
        self.assertEqual(line.remaining, 4)
        self.assertIsNone(line.teardown_returned)

    def test_the_undo_is_recorded_like_everything_else(self):
        p = self.part(qty=10)
        proj = self.project("Doomed")
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        self.tear_down(proj, [(line, 0, 3, 1)])
        proj.reopen()

        movement = p.movements.first()
        self.assertEqual(movement.reason, MovementReason.REOPEN)
        self.assertEqual(movement.delta, 4)
        self.assertEqual(movement.project, proj)
        p.refresh_from_db()
        self.assertEqual(movement.balance_after, p.qty_owned)

    def test_a_clean_teardown_reopens_without_moving_any_quantity(self):
        p = self.part(qty=10)
        proj = self.project()
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        self.tear_down(proj, [(line, 4, 0, 0)])
        proj.reopen()

        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 10)
        self.assertEqual(self.reopen_movements(p).count(), 0)

    def reopen_movements(self, part):
        return part.movements.filter(reason=MovementReason.REOPEN)

    def test_reopening_a_live_project_is_refused(self):
        proj = self.project()
        with self.assertRaises(ValidationError):
            proj.reopen()

    def test_tear_down_reopen_tear_down_lands_in_the_same_place(self):
        p = self.part(qty=10)
        proj = self.project()
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)

        self.tear_down(proj, [(line, 1, 2, 1)])
        p.refresh_from_db()
        first = p.qty_owned

        proj.reopen()
        line.refresh_from_db()
        self.tear_down(proj, [(line, 1, 2, 1)])
        p.refresh_from_db()

        self.assertEqual(p.qty_owned, first)

    def test_the_page_says_what_will_come_back(self):
        p = self.part(qty=10)
        proj = self.project()
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        self.tear_down(proj, [(line, 0, 3, 1)])

        response = self.client.get(reverse("project_reopen", args=[proj.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["coming_back"], 4)
        self.assertContains(response, "10k resistor")

    def test_posting_actually_reopens_it(self):
        p = self.part(qty=10)
        proj = self.project()
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        self.tear_down(proj, [(line, 0, 3, 1)])

        response = self.client.post(reverse("project_reopen", args=[proj.pk]))
        self.assertRedirects(response, reverse("project_detail", args=[proj.pk]))
        proj.refresh_from_db()
        p.refresh_from_db()
        self.assertEqual(proj.status, ProjectStatus.ACTIVE)
        self.assertEqual(p.qty_owned, 10)

    def test_a_live_project_just_redirects(self):
        proj = self.project()
        response = self.client.get(reverse("project_reopen", args=[proj.pk]))
        self.assertRedirects(response, reverse("project_detail", args=[proj.pk]))

    def test_you_cannot_reopen_someone_elses_project(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        theirs = Project.objects.create(
            user=stranger, name="Theirs", status=ProjectStatus.ARCHIVED
        )
        response = self.client.post(reverse("project_reopen", args=[theirs.pk]))
        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, ProjectStatus.ARCHIVED)


class StockLedgerTests(BaseCase):
    def reasons(self, part):
        return [
            m.reason for m in part.movements.all() if m.reason != MovementReason.OPENING
        ]

    def opening(self, part):
        return part.movements.filter(reason=MovementReason.OPENING)

    def test_a_new_part_opens_its_own_history(self):
        p = self.part(qty=10)
        self.assertEqual(self.opening(p).get().delta, 10)
        self.assertEqual(self.opening(p).get().balance_after, 10)

    def test_adjusting_records_the_change_and_the_running_balance(self):
        p = self.part(qty=10)
        p.adjust_stock(50, MovementReason.PURCHASE)
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 60)

        movement = p.movements.first()
        self.assertEqual(movement.delta, 50)
        self.assertEqual(movement.balance_after, 60)
        self.assertEqual(movement.reason, MovementReason.PURCHASE)

    def test_a_zero_change_is_not_recorded(self):
        p = self.part(qty=10)
        self.assertIsNone(p.adjust_stock(0, MovementReason.PURCHASE))
        self.assertEqual(self.reasons(p), [])

    def test_cannot_go_below_zero(self):
        p = self.part(qty=3)
        with self.assertRaises(ValidationError):
            p.adjust_stock(-5, MovementReason.CORRECTION)
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 3)
        self.assertEqual(self.reasons(p), [])

    def test_cannot_go_below_what_projects_are_holding(self):
        p = self.part(qty=10)
        ProjectPart.objects.create(project=self.project(), part=p, qty_allocated=6)
        with self.assertRaises(ValidationError):
            p.adjust_stock(-7, MovementReason.CORRECTION)
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 10)

    def test_set_stock_records_the_difference_not_the_total(self):
        p = self.part(qty=10)
        p.set_stock(7)
        self.assertEqual(p.movements.first().delta, -3)
        self.assertEqual(p.movements.first().balance_after, 7)

    def test_teardown_losses_name_the_project_that_ate_them(self):
        p = self.part(qty=10)
        proj = self.project("Doomed build")
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        proj.tear_down([(line, 1, 2, 1)])

        movement = p.movements.first()
        self.assertEqual(movement.delta, -3)
        self.assertEqual(movement.reason, MovementReason.TEARDOWN)
        self.assertEqual(movement.project, proj)
        self.assertIn("2 soldered in, 1 broken", movement.note)

    def test_a_rejected_teardown_leaves_no_trace(self):
        p = self.part(qty=10)
        proj = self.project()
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        with self.assertRaises(ValidationError):
            proj.tear_down([(line, 0, 1, 1)])
        self.assertEqual(self.reasons(p), [])

    def test_adding_a_part_by_hand_opens_its_history(self):
        self.client.post(
            reverse("part_create"),
            {
                "name": "DHT22",
                "qty_owned": 5,
                "value": "",
                "package": "",
                "pin_count": "",
                "voltage": "",
                "tags": "",
                "notes": "",
            },
        )
        part = Part.objects.get(name="DHT22")
        self.assertEqual(self.reasons(part), [])
        self.assertEqual(self.opening(part).get().delta, 5)

    def test_importing_opens_a_history_for_each_part(self):
        self.client.post(reverse("part_import"), {"text": "DHT22, 4\nBMP280, 2"})
        for name, qty in [("DHT22", 4), ("BMP280", 2)]:
            part = Part.objects.get(name=name)
            self.assertEqual(self.reasons(part), [])
            self.assertEqual(self.opening(part).get().delta, qty)

    def test_add_stock_is_recorded_as_a_delivery(self):
        p = self.part(qty=10)
        self.client.post(reverse("part_add_stock", args=[p.pk]), {"qty": 25})
        self.assertEqual(self.reasons(p), [MovementReason.PURCHASE])
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 35)

    def test_editing_the_quantity_is_recorded_as_a_recount(self):
        p = self.part(qty=10)
        self.client.post(
            reverse("part_update", args=[p.pk]),
            {
                "name": p.name,
                "qty_owned": 8,
                "value": "",
                "package": "",
                "pin_count": "",
                "voltage": "",
                "tags": "",
                "notes": "",
            },
        )
        p.refresh_from_db()
        self.assertEqual(p.qty_owned, 8)
        self.assertEqual(self.reasons(p), [MovementReason.CORRECTION])
        self.assertEqual(p.movements.first().delta, -2)

    def test_editing_other_fields_does_not_invent_a_movement(self):
        p = self.part(qty=10)
        self.client.post(
            reverse("part_update", args=[p.pk]),
            {
                "name": p.name,
                "qty_owned": 10,
                "value": "10k",
                "package": "",
                "pin_count": "",
                "voltage": "",
                "tags": "",
                "notes": "",
            },
        )
        p.refresh_from_db()
        self.assertEqual(p.value, "10k")
        self.assertEqual(self.reasons(p), [])

    def test_the_history_appears_on_the_part_page(self):
        p = self.part(qty=10)
        p.adjust_stock(5, MovementReason.PURCHASE)
        response = self.client.get(reverse("part_detail", args=[p.pk]))
        self.assertContains(response, "History")
        self.assertContains(response, "Bought or found")

    def test_the_ledger_reconciles_with_the_stored_quantity(self):
        p = self.part(qty=10)
        p.adjust_stock(5, MovementReason.PURCHASE)
        p.set_stock(12)
        proj = self.project()
        line = ProjectPart.objects.create(project=proj, part=p, qty_allocated=4)
        proj.tear_down([(line, 2, 1, 1)])

        p.refresh_from_db()
        ledger = sum(p.movements.values_list("delta", flat=True))
        self.assertEqual(ledger, p.qty_owned)
        self.assertEqual(p.movements.first().balance_after, p.qty_owned)


class SortingTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.cheap = self.part("Zener diode", qty=100)
        self.scarce = self.part("Arduino Nano", qty=3)
        self.middling = self.part("MPU-6050", qty=20)
        ProjectPart.objects.create(
            project=self.project(), part=self.scarce, qty_allocated=3
        )

    def names(self, response):
        return [p.name for p in response.context["parts"]]

    def test_defaults_to_name_ascending(self):
        response = self.client.get(reverse("part_list"))
        self.assertEqual(self.names(response), sorted(self.names(response)))

    def test_sorting_by_availability_finds_what_you_are_out_of(self):
        response = self.client.get(reverse("part_list"), {"sort": "available"})
        self.assertEqual(self.names(response)[0], "Arduino Nano")

    def test_direction_can_be_reversed(self):
        response = self.client.get(reverse("part_list"), {"sort": "-available"})
        self.assertEqual(self.names(response)[0], "Zener diode")

    def test_sorting_by_owned(self):
        response = self.client.get(reverse("part_list"), {"sort": "owned"})
        self.assertEqual([p.qty_owned for p in response.context["parts"]], [3, 20, 100])

    def test_an_unknown_sort_falls_back_instead_of_erroring(self):
        response = self.client.get(reverse("part_list"), {"sort": "; DROP TABLE"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.names(response), sorted(self.names(response)))

    def test_headers_toggle_the_direction_you_are_already_on(self):
        response = self.client.get(reverse("part_list"), {"sort": "available"})
        columns = {c["label"]: c for c in response.context["columns"]}
        self.assertTrue(columns["Available"]["active"])
        self.assertFalse(columns["Available"]["descending"])
        self.assertIn("sort=-available", columns["Available"]["url"])

    def test_sorting_keeps_the_search_you_typed(self):
        response = self.client.get(
            reverse("part_list"), {"q": "Nano", "sort": "available"}
        )
        self.assertEqual(self.names(response), ["Arduino Nano"])
        columns = {c["label"]: c for c in response.context["columns"]}
        self.assertIn("q=Nano", columns["Owned"]["url"])


class DashboardTests(BaseCase):
    url = reverse_lazy("dashboard")

    def test_requires_login(self):
        response = Client().get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_empty_account_points_at_the_importer(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("part_import"))

    def test_lists_whats_on_the_bench_with_held_counts(self):
        p = self.part(qty=10)
        live = self.project("On the bench")
        ProjectPart.objects.create(project=live, part=p, qty_allocated=4)
        self.project("Old one", status=ProjectStatus.ARCHIVED)

        response = self.client.get(self.url)
        self.assertEqual(
            [pr.name for pr in response.context["active"]], ["On the bench"]
        )
        self.assertEqual(response.context["active"][0].held_count, 4)
        self.assertEqual(response.context["archived_count"], 1)

    def test_shopping_list_totals_shortfall_across_builds(self):
        scarce = self.part("DHT22", qty=1)
        for name, wanted in [("Build A", 3), ("Build B", 4)]:
            ProjectPart.objects.create(
                project=self.project(name),
                part=scarce,
                qty_wanted=wanted,
                qty_allocated=0,
            )
        response = self.client.get(self.url)
        rows = list(response.context["shortfall"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "DHT22")
        self.assertEqual(rows[0]["from_builds"], 7)
        self.assertEqual(rows[0]["total"], 7)
        self.assertEqual(response.context["total_short"], 7)

    def test_archived_builds_do_not_appear_on_the_shopping_list(self):
        p = self.part(qty=0)
        ProjectPart.objects.create(
            project=self.project("Done", status=ProjectStatus.ARCHIVED),
            part=p,
            qty_wanted=5,
            qty_allocated=0,
        )
        response = self.client.get(self.url)
        self.assertEqual(list(response.context["shortfall"]), [])

    def test_running_low_puts_the_scarcest_first(self):
        self.part("Plentiful", qty=200)
        scarce = self.part("Scarce", qty=2)
        ProjectPart.objects.create(project=self.project(), part=scarce, qty_allocated=2)
        response = self.client.get(self.url)
        self.assertEqual(response.context["running_low"][0].name, "Scarce")
        self.assertEqual(response.context["running_low"][0].available, 0)

    def test_query_count_does_not_grow_with_the_number_of_builds(self):
        parts = [self.part(f"P{i}", qty=50) for i in range(3)]

        def queries_for(project_count):
            Project.objects.filter(user=self.user).delete()
            for j in range(project_count):
                project = self.project(f"Build {j}")
                for part in parts:
                    ProjectPart.objects.create(
                        project=project, part=part, qty_wanted=4, qty_allocated=2
                    )
            with CaptureQueriesContext(connection) as captured:
                self.client.get(self.url)
            return len(captured)

        self.assertEqual(queries_for(2), queries_for(8))

    def test_only_your_own_work_appears(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        Project.objects.create(user=stranger, name="Theirs")
        Part.objects.create(user=stranger, name="Their part", qty_owned=5)
        response = self.client.get(self.url)
        self.assertEqual(list(response.context["active"]), [])
        self.assertEqual(response.context["total_parts"], 0)


class GuideTests(ClearsThrottle, TestCase):
    def test_readable_without_an_account(self):
        response = Client().get(reverse("guide"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "They get held")

    def test_covers_the_whole_loop(self):
        response = Client().get(reverse("guide"))
        for topic in ["Import", "Tear down", "Returned", "Soldered in", "short"]:
            with self.subTest(topic=topic):
                self.assertContains(response, topic)

    def test_offers_signup_to_visitors_but_not_to_members(self):
        anonymous = Client().get(reverse("guide"))
        self.assertContains(anonymous, reverse("signup"))

        user = User.objects.create_user("owner", "o@e.com", "pw12345!")
        client = Client()
        client.force_login(user)
        self.assertNotContains(client.get(reverse("guide")), reverse("signup"))

    def test_login_page_points_newcomers_at_it(self):
        self.assertContains(Client().get(reverse("login")), reverse("guide"))


class NavigationTests(BaseCase):
    def test_the_brand_is_a_mark_not_a_link(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, '<span class="brand">')
        self.assertNotContains(
            response, f'<a href="{reverse("guide")}">Inventory System</a>'
        )

    def test_every_section_has_its_own_nav_entry(self):
        response = self.client.get(reverse("dashboard"))
        for name, label in [
            ("dashboard", "Bench"),
            ("part_list", "Parts"),
            ("project_list", "Projects"),
            ("guide", "Guide"),
        ]:
            with self.subTest(label=label):
                self.assertContains(response, f'<a href="{reverse(name)}">{label}</a>')

    def test_bench_is_still_the_landing_page_after_login(self):
        client = Client()
        client.login(username="owner", password="pw12345!")
        response = client.post(
            reverse("login"), {"username": "owner", "password": "pw12345!"}
        )
        self.assertRedirects(response, reverse("dashboard"))


class HealthCheckTests(TestCase):
    def test_healthz_is_open_and_reports_ok(self):
        response = Client().get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_healthz_does_not_require_login(self):
        response = Client().get(reverse("healthz"))
        self.assertNotIn("Location", response.headers)


class PasswordResetTests(ClearsThrottle, TestCase):
    def test_reset_page_admits_it_cannot_send_mail(self):
        response = self.client.get(reverse("password_reset"))
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "isn't set up", status_code=503)

    def test_login_page_does_not_offer_a_reset_link(self):
        response = self.client.get(reverse("login"))
        self.assertNotContains(response, "Forgot your password")

    def test_posting_to_a_disabled_reset_sends_nothing(self):
        User.objects.create_user("owner", "o@example.com", "pw12345!")
        response = self.client.post(
            reverse("password_reset"), {"email": "o@example.com"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(EMAIL_CONFIGURED=True)
class PasswordResetWithMailTests(ClearsThrottle, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("owner", "o@example.com", "pw12345!")

    def test_login_page_offers_the_link_once_mail_works(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, "Forgot your password")

    def test_reset_form_renders_instead_of_the_apology(self):
        response = self.client.get(reverse("password_reset"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reset your password")

    def test_end_to_end_reset_actually_changes_the_password(self):
        response = self.client.post(
            reverse("password_reset"), {"email": "o@example.com"}
        )
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)

        message = mail.outbox[0]
        self.assertEqual(message.subject, "Reset your Inventory password")
        self.assertIn("owner", message.body)
        self.assertIn("/accounts/reset/", message.body)

        link = next(
            line.strip()
            for line in message.body.splitlines()
            if "/accounts/reset/" in line
        )
        path = link.split("testserver", 1)[-1]

        response = self.client.get(path, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose a new password")

        response = self.client.post(
            response.redirect_chain[-1][0] if response.redirect_chain else path,
            {
                "new_password1": "a-brand-new-passphrase",
                "new_password2": "a-brand-new-passphrase",
            },
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("a-brand-new-passphrase"))

    def test_unknown_address_does_not_reveal_itself(self):
        response = self.client.post(
            reverse("password_reset"), {"email": "nobody@example.com"}
        )
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)
