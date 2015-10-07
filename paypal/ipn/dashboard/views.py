from django.views import generic
from paypal.ipn import models


class IPNResponseListView(generic.ListView):
    model = models.IPNResponse
    template_name = 'paypal/ipn/dashboard/ipn_list.html'
    context_object_name = 'ipn_responses'


class IPNResponseDetailView(generic.DetailView):
    model = models.IPNResponse
    template_name = 'paypal/ipn/dashboard/ipn_detail.html'
    context_object_name = 'ipn_res'

