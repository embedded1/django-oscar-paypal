from django.conf import settings
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from paypal.adaptive.gateway import (
    pay, payment_details, set_payment_option,
    execute_payment, get_account_info
)


def get_pay_request_attrs(receivers, basket, action, host=None,
                          scheme=None, sender_email=None, paypal_params=None):
    """
    Return the URL for a PayPal Adaptive Payment transaction.

    This involves registering the txn with PayPal to get a one-time
    URL.  If a shipping method and shipping address are passed, then these are
    given to PayPal directly - this is used within when using PayPal as a
    payment method.
    """
    currency = getattr(settings, 'PAYPAL_CURRENCY', 'GBP')
    if host is None:
        host = Site.objects.get_current().domain
    if scheme is None:
        use_https = getattr(settings, 'PAYPAL_CALLBACK_HTTPS', True)
        scheme = 'https' if use_https else 'http'
    return_url = '%s://%s%s' % (
        scheme, host, reverse('paypal-success-response', kwargs={
            'basket_id': basket.id}))
    cancel_url = '%s://%s%s' % (
        scheme, host, reverse('paypal-cancel-response', kwargs={
            'basket_id': basket.id}))
    #if getattr(settings, 'PAYPAL_SANDBOX_MODE', False):
    #    ipn_url = settings.PAYPAL_SANDBOX_IPN_URL % basket.id
    #else:
    #    ipn_url = '%s://%s%s' % (
    #            scheme, host, reverse('webhooks:paypal-ipn', kwargs={
    #                'basket_id': basket.id}))

    #first create the Pay transaction
    txn = pay(receivers=receivers,
              action=action,
              currency=currency,
              return_url=return_url,
              cancel_url=cancel_url,
              sender_email=sender_email)

    #Return some Pay request important attributes
    return (
        txn.redirect_url,
        txn.correlation_id,
        txn.pay_key
    )


def fetch_transaction_details(txn_id):
    """
    Fetch the completed details about the PayPal transaction.
    """
    return payment_details(txn_id)

def set_transaction_details(pay_key, shipping_address, basket=None):
    return set_payment_option(
        basket=basket,
        pay_key=pay_key,
        shipping_address=shipping_address)

def fetch_account_info(first_name, last_name, email):
    return get_account_info(first_name, last_name, email)

def pay_secondary_receivers(pay_key):
    return execute_payment(pay_key)