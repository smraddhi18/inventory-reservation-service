from django.urls import path

from inventory import views

urlpatterns = [
    path("health/live", views.live),
    path("health/ready", views.ready),
    path("v1/items", views.items),
    path("v1/items/<str:sku>", views.item_detail),
    path("v1/items/<str:sku>/movements", views.movements),
    path("v1/items/<str:sku>/adjustments", views.adjustments),
    path("v1/reservations", views.reservations),
    path("v1/reservations/<uuid:reservation_id>", views.reservation_detail),
    path("v1/reservations/<uuid:reservation_id>/commit", views.commit_reservation),
    path("v1/reservations/<uuid:reservation_id>/cancel", views.cancel_reservation),
]
