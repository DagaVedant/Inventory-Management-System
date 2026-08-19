from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

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
        self.url = reverse("admin:core_project_teardown", args=[self.proj.pk])

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
        url = reverse("admin:core_project_teardown", args=[empty.pk])
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
        url = reverse("admin:core_project_teardown", args=[proj.pk])
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
