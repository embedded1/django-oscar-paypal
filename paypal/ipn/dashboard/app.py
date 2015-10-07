from django.conf.urls.defaults import patterns, url
from django.contrib.admin.views.decorators import staff_member_required

from oscar.core.application import Application

from paypal.ipn.dashboard import views


class IPNDashboardApplication(Application):
    name = None
    list_view = views.IPNResponseListView
    detail_view = views.IPNResponseDetailView

    def get_urls(self):
        urlpatterns = patterns('',
            url(r'^ipns/$', self.list_view.as_view(),
                name='paypal-ipn-list'),
            url(r'^ipns/(?P<pk>\d+)/$', self.detail_view.as_view(),
                name='paypal-ipn-detail'),
        )
        return self.post_process_urls(urlpatterns)

    def get_url_decorator(self, url_name):
        return staff_member_required


application = IPNDashboardApplication()
