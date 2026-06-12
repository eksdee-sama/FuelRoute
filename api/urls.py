from django.urls import path

from .views import PlaceSearchView, RouteMapView, RouteView

urlpatterns = [
    path("route/", RouteView.as_view(), name="route"),
    path("route/map/", RouteMapView.as_view(), name="route-map"),
    path("search/", PlaceSearchView.as_view(), name="place-search"),
]
