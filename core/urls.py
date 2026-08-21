from django.urls import path

from . import views

urlpatterns = [
    path("healthz/", views.healthz, name="healthz"),
    path("", views.dashboard, name="dashboard"),
    path("guide/", views.GuideView.as_view(), name="guide"),
    path("parts/", views.PartListView.as_view(), name="part_list"),
    path("parts/new/", views.PartCreateView.as_view(), name="part_create"),
    path("parts/import/", views.part_import, name="part_import"),
    path("parts/<int:pk>/", views.PartDetailView.as_view(), name="part_detail"),
    path("parts/<int:pk>/add-stock/", views.part_add_stock, name="part_add_stock"),
    path("parts/<int:pk>/want/", views.part_want, name="part_want"),
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
        "projects/<int:pk>/reopen/",
        views.project_reopen,
        name="project_reopen",
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
