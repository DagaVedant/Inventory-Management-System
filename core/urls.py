from django.urls import path

from . import views

urlpatterns = [
    path("healthz/", views.healthz, name="healthz"),
    path("", views.PartListView.as_view(), name="part_list"),
    path("parts/new/", views.PartCreateView.as_view(), name="part_create"),
    path("parts/<int:pk>/edit/", views.PartUpdateView.as_view(), name="part_update"),
    path("parts/<int:pk>/delete/", views.PartDeleteView.as_view(), name="part_delete"),
    path("projects/", views.ProjectListView.as_view(), name="project_list"),
    path("projects/new/", views.ProjectCreateView.as_view(), name="project_create"),
    path("projects/<int:pk>/", views.project_detail, name="project_detail"),
    path(
        "projects/<int:pk>/edit/",
        views.ProjectUpdateView.as_view(),
        name="project_update",
    ),
    path(
        "projects/<int:pk>/delete/",
        views.ProjectDeleteView.as_view(),
        name="project_delete",
    ),
    path(
        "projects/<int:pk>/teardown/",
        views.project_teardown,
        name="project_teardown",
    ),
    path(
        "projects/<int:pk>/lines/<int:line_pk>/remove/",
        views.line_remove,
        name="line_remove",
    ),
    path(
        "projects/<int:pk>/lines/<int:line_pk>/return/",
        views.line_return,
        name="line_return",
    ),
]
