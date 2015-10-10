from django.views import generic
from paypal.ipn import models


class PaymentsListView(generic.ListView):
    model = models.PaymentMessage
    template_name = 'paypal/ipn/dashboard/payment/messages_list.html'
    context_object_name = 'payment_messages'


class PaymentDetailView(generic.DetailView):
    model = models.PaymentMessage
    template_name = 'paypal/ipn/dashboard/payment/message_detail.html'
    context_object_name = 'payment_message'

