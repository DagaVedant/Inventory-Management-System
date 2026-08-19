from datetime import datetime, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse, reverse_lazy

from .models import Part, Project, ProjectPart, ProjectStatus

User = get_user_model()


class BaseCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("owner", "o@example.com", "pw12345!")
        self.client = Client()
        self.client.force_login(self.user)

    def part(self, name="10k resistor", qty=10, **kw):
        return Part.objects.create(user=self.user, name=name, qty_owned=qty, **kw)

    def project(self, name="Weather Station", **kw):
        return Project.objects.create(user=self.user, name=name, **kw)


class AvailabilityTests(BaseCase):
    """qty_owned is stored; held and available are derived."""

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
    """Guards that live in the database, not just in Python."""

    def test_cannot_account_for_more_than_allocated(self):
        p, proj = self.part(), self.project()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectPart.objects.create(
                project=proj, part=p, qty_allocated=1, qty_returned=5
            )

    def test_allocation_must_be_positive(self):
        p, proj = self.part(), self.project()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectPart.objects.create(project=proj, part=p, qty_allocated=0)

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
    """You cannot allocate parts you do not have."""

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
        line.full_clean()  # unchanged re-save must not look like over-allocation
        line.qty_allocated = 8
        line.full_clean()  # reducing is always fine
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
        # First line is fine, second does not add up. The whole thing must roll back.
        self.post([(self.line, 4, 0, 0), (line2, 1, 0, 0)])
        self.line.refresh_from_db()
        self.p.refresh_from_db()
        self.proj.refresh_from_db()
        self.assertEqual(self.line.qty_returned, 0)
        self.assertEqual(self.p.qty_owned, 10)
        self.assertEqual(self.proj.status, ProjectStatus.ACTIVE)


class ScopingTests(TestCase):
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
    """The custom UI, as opposed to the admin."""

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
                "name": "DHT22", "qty_owned": 3, "value": "", "package": "",
                "pin_count": "", "voltage": "", "tags": "", "notes": "",
                "_addanother": "1",
            },
        )
        self.assertRedirects(response, reverse("part_create"))
        self.assertTrue(Part.objects.filter(name="DHT22").exists())

    def test_created_part_belongs_to_you(self):
        self.client.post(
            reverse("part_create"),
            {
                "name": "DHT22", "qty_owned": 3, "value": "", "package": "",
                "pin_count": "", "voltage": "", "tags": "", "notes": "",
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

    def test_allocating_reduces_available_but_not_owned(self):
        self.client.post(self.url, {"part": self.p.pk, "qty_allocated": 4, "note": ""})
        self.p.refresh_from_db()
        self.assertEqual(self.p.qty_owned, 10)
        self.assertEqual(self.p.compute_available(), 6)

    def test_cannot_allocate_more_than_available(self):
        response = self.client.post(
            self.url, {"part": self.p.pk, "qty_allocated": 11, "note": ""}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "available")
        self.assertEqual(self.proj.lines.count(), 0)

    def test_allocating_the_same_part_twice_tops_up_the_line(self):
        self.client.post(self.url, {"part": self.p.pk, "qty_allocated": 2, "note": ""})
        self.client.post(self.url, {"part": self.p.pk, "qty_allocated": 3, "note": ""})
        self.assertEqual(self.proj.lines.count(), 1)
        self.assertEqual(self.proj.lines.first().qty_allocated, 5)

    def test_topping_up_still_respects_availability(self):
        self.client.post(self.url, {"part": self.p.pk, "qty_allocated": 8, "note": ""})
        response = self.client.post(
            self.url, {"part": self.p.pk, "qty_allocated": 5, "note": ""}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.proj.lines.first().qty_allocated, 8)

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
        self.client.post(self.url, {"part": self.p.pk, "qty_allocated": 1, "note": ""})
        self.assertEqual(self.proj.lines.count(), 0)

    def test_part_picker_only_offers_your_own_parts(self):
        stranger = User.objects.create_user("stranger", "s@e.com", "pw12345!")
        theirs = Part.objects.create(user=stranger, name="Not yours", qty_owned=5)
        response = self.client.post(
            self.url, {"part": theirs.pk, "qty_allocated": 1, "note": ""}
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


class SignupTests(TestCase):
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
        self.assertRedirects(response, reverse("part_list"))
        self.assertTrue(User.objects.filter(username="newcomer").exists())
        self.assertEqual(int(self.client.session["_auth_user_id"]),
                         User.objects.get(username="newcomer").pk)

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
        self.assertRedirects(self.client.get(self.url), reverse("part_list"))

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
        self.assertRedirects(response, reverse("part_list"))
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
        # Those three parts were written off at teardown. Deleting the record
        # must not resurrect them.
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
        """A project created late evening Eastern must not show tomorrow."""
        proj = self.project()
        Project.objects.filter(pk=proj.pk).update(
            created_at=datetime(2026, 8, 20, 1, 30, tzinfo=dt_timezone.utc)
        )
        response = self.client.get(reverse("project_list"))
        # 01:30 UTC on the 20th is 21:30 on the 19th in New York.
        self.assertContains(response, "19 Aug 2026")
        self.assertNotContains(response, "20 Aug 2026")

    def test_pages_declare_a_favicon(self):
        response = self.client.get(reverse("part_list"))
        self.assertContains(response, "favicon.svg")


class PasswordResetTests(TestCase):
    """With no mail server configured, say so instead of pretending."""

    def test_reset_page_admits_it_cannot_send_mail(self):
        response = self.client.get(reverse("password_reset"))
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "isn't set up", status_code=503)

    def test_login_page_does_not_offer_a_reset_link(self):
        response = self.client.get(reverse("login"))
        self.assertNotContains(response, "Forgot your password")
