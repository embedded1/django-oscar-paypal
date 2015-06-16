from django.conf.urls import *
from paypal.adaptive import views


urlpatterns = patterns('',
    # Views for normal flow that starts on the basket page
    url(r'^redirect/', views.RedirectView.as_view(), name='paypal-redirect'),
    url(r'^preview/(?P<basket_id>\d+)/$',
        views.SuccessResponseView.as_view(preview=True),
        name='paypal-success-response'),
    url(r'^cancel/(?P<basket_id>\d+)/$', views.CancelResponseView.as_view(),
        name='paypal-cancel-response'),
)

