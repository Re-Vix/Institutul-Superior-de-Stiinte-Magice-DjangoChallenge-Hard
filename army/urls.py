from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('action/<str:action>/', views.run_action, name='run_action'),
    path('simulate', views.simulate, name='simulate'),
    path('simulate/', views.simulate),
    path('simulate/<int:fight_id>', views.fight_detail, name='fight_detail'),
]
