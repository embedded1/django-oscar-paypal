from oscar.core.loading import get_class
from decimal import Decimal as D
from django.http import HttpResponseRedirect
from django.db.models import get_model
from django.views import generic
from django.shortcuts import get_object_or_404
from django.conf import settings
from paypal.exceptions import PayPalError
from paypal.adaptive.exceptions import (
    EmptyBasketException, MissingShippingAddressException,
    MissingShippingMethodException, InvalidBasket)
from paypal.adaptive.facade import (
    get_paypal_url, fetch_transaction_details,
    fetch_account_status, confirm_transaction)
from paypal.express.facade import fetch_address_details
from django.contrib import messages
from django.utils import six
from django.core.urlresolvers import reverse
from django.utils.translation import ugettext_lazy as _
from oscar.apps.payment.exceptions import UnableToTakePayment
import logging

# Load views dynamically
PaymentDetailsView = get_class('checkout.views', 'PaymentDetailsView')
CheckoutSessionMixin = get_class('checkout.session', 'CheckoutSessionMixin')
Repository = get_class('shipping.repository', 'Repository')
Applicator = get_class('offer.utils', 'Applicator')
Selector = get_class('partner.strategy', 'Selector')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Basket = get_model('basket', 'Basket')
logger = logging.getLogger('paypal.adaptive')


class RedirectView(CheckoutSessionMixin, generic.RedirectView):
    """
    Initiate the transaction with Paypal and redirect the user
    to PayPal's adaptive payments to perform the transaction.
    """
    permanent = False

    # Setting to distinguish if the site has already collected a shipping
    # address.  This is False when redirecting to PayPal straight from the
    # basket page but True when redirecting from checkout.
    as_payment_method = False

    def get_redirect_url(self, **kwargs):
        try:
            basket = self.request.basket
            url = self._get_redirect_url(basket, **kwargs)
        except PayPalError:
            messages.error(
                self.request, _("An error occurred communicating with PayPal"))
            if self.as_payment_method:
                url = reverse('checkout:payment-details')
            else:
                url = reverse('basket:summary')
            return url
        except InvalidBasket as e:
            messages.warning(self.request, six.text_type(e))
            return reverse('basket:summary')
        except EmptyBasketException:
            messages.error(self.request, _("Your basket is empty"))
            return reverse('basket:summary')
        except MissingShippingAddressException:
            messages.error(
                self.request, _("A shipping address must be specified"))
            return reverse('checkout:shipping-address')
        except MissingShippingMethodException:
            messages.error(
                self.request, _("A shipping method must be specified"))
            return reverse('checkout:shipping-method')
        else:
            # Transaction successfully registered with PayPal.  Now freeze the
            # basket so it can't be edited while the customer is on the PayPal
            # site.
            basket.freeze()

            logger.info("Basket #%s - redirecting to %s", basket.id, url)

            return url

    def _get_redirect_url(self, basket, **kwargs):
        if basket.is_empty:
            raise EmptyBasketException()

        params = {
            'basket': basket,
            'shipping_methods': []          # setup a default empty list
        }                                   # to support no_shipping

        user = self.request.user
        if self.as_payment_method:
            if basket.is_shipping_required():
                # Only check for shipping details if required.
                shipping_addr = self.get_shipping_address(basket)
                if not shipping_addr:
                    raise MissingShippingAddressException()

                shipping_method = self.get_shipping_method(
                    basket, shipping_addr)
                if not shipping_method:
                    raise MissingShippingMethodException()

                params['shipping_address'] = shipping_addr

        if settings.DEBUG:
            # Determine the localserver's hostname to use when
            # in testing mode
            params['host'] = self.request.META['HTTP_HOST']

        if user.is_authenticated():
            params['user'] = user

        params['paypal_params'] = self._get_paypal_params()

        return get_paypal_url(**params)

    def _get_paypal_params(self):
        """
        Return any additional PayPal parameters
        """
        return {}


class SuccessResponseView(PaymentDetailsView):
    template_name_preview = 'paypal/adaptive/preview.html'
    preview = True

    def get(self, request, *args, **kwargs):
        """
        Fetch details about the successful transaction from PayPal.  We use
        these details to show a preview of the order with a 'submit' button to
        place it.
        """
        try:
            self.pay_key = request.GET['payKey']
        except KeyError:
            # Manipulation - redirect to basket page with warning message
            logger.warning("Missing GET params on success response page")
            messages.error(
                self.request,
                _("Unable to determine PayPal transaction details"))
            return HttpResponseRedirect(reverse('customer:pending-packages'))

        try:
            self.txn = fetch_transaction_details(self.pay_key)
        except PayPalError as e:
            logger.warning(
                "Unable to fetch transaction details for pay key %s: %s",
                self.pay_key, e)
            messages.error(
                self.request,
                _("A problem occurred communicating with PayPal - please try again later"))
            return HttpResponseRedirect(reverse('customer:pending-packages'))

        # Reload frozen basket which is specified in the URL
        kwargs['basket'] = self.load_frozen_basket(kwargs['basket_id'])
        if not kwargs['basket']:
            logger.warning(
                "Unable to load frozen basket with ID %s", kwargs['basket_id'])
            messages.error(
                self.request,
                _("No basket was found that corresponds to your "
                  "PayPal transaction"))
            return HttpResponseRedirect(reverse('customer:pending-packages'))

        basket = request.basket
        user = self.request.user
        customer_shipping_address = self.get_shipping_address(basket)
        is_return_to_merchant = self.checkout_session.is_return_to_store_enabled()

        #check that shipping address exists
        if not customer_shipping_address and not is_return_to_merchant:
            # we could not get shipping address - redirect to basket page with warning message
            logger.warning("customer's shipping address not found while verifying PayPal account")
            messages.error(self.request, _("No shipping address found, please try again."))
            self.unfreeze_basket(kwargs['basket_id'])
            return HttpResponseRedirect(reverse('checkout:shipping-address'))

        if not self.validate_txn(sender_email=user.email,
                                 sender_first_name=user.first_name,
                                 sender_last_name=user.last_name,
                                 is_return_to_merchant=is_return_to_merchant,
                                 sender_shipping_address=customer_shipping_address):
            return HttpResponseRedirect(reverse('customer:pending-packages'))

        logger.info(
            "Basket #%s - showing preview with pay key %s",
            kwargs['basket'].id, self.pay_key)

        return super(SuccessResponseView, self).get(request, *args, **kwargs)

    def load_frozen_basket(self, basket_id):
        # Lookup the frozen basket that this txn corresponds to
        try:
            basket = Basket.objects.get(id=basket_id, status=Basket.FROZEN)
        except Basket.DoesNotExist:
            return None

        # Assign strategy to basket instance
        if Selector:
            basket.strategy = Selector().strategy(self.request)

        # Re-apply any offers
        Applicator().apply(self.request, basket)

        return basket

    def get_context_data(self, **kwargs):
        ctx = super(SuccessResponseView, self).get_context_data(**kwargs)

        if not hasattr(self, 'pay_key'):
            return ctx

        # This context generation only runs when in preview mode
        ctx.update({
            'pay_key': self.pay_key
        })

        return ctx

    def post(self, request, *args, **kwargs):
        """
        Place an order.

        We fetch the txn details again and then proceed with oscar's standard
        payment details view for placing the order.
        """
        error_msg = _(
            "A problem occurred communicating with PayPal "
            "- please try again later"
        )
        try:
            self.pay_key = request.POST['pay_key']
        except KeyError:
            # Probably suspicious manipulation if we get here
            messages.error(self.request, error_msg)
            return HttpResponseRedirect(reverse('customer:pending-packages'))

        try:
            self.txn = fetch_transaction_details(self.pay_key)
        except PayPalError:
            # Unable to fetch txn details from PayPal - we have to bail out
            messages.error(self.request, error_msg)
            return HttpResponseRedirect(reverse('customer:pending-packages'))

        # Reload frozen basket which is specified in the URL
        basket = self.load_frozen_basket(kwargs['basket_id'])
        if not basket:
            messages.error(self.request, error_msg)
            return HttpResponseRedirect(reverse('customer:pending-packages'))

        submission = self.build_submission(basket=basket)
        return self.submit(**submission)

    def build_submission(self, **kwargs):
        submission = super(
            SuccessResponseView, self).build_submission(**kwargs)
        # Pass PP params
        submission['payment_kwargs']['pay_key'] = self.pay_key
        submission['payment_kwargs']['txn'] = self.txn
        return submission

    # Warning: This method can be removed when we drop support for Oscar 0.6
    def get_error_response(self):
        # We bypass the normal session checks for shipping address and shipping
        # method as they don't apply here.
        pass

    def handle_payment(self, order_number, total, **kwargs):
        """
        Complete payment with PayPal - this calls the 'ExecutePayment'
        method to capture the money from the initial transaction.
        """
        try:
            confirm_txn = confirm_transaction(kwargs['pay_key'])
        except PayPalError:
            raise UnableToTakePayment()
        if not confirm_txn.is_successful:
            raise UnableToTakePayment()

        payment_details_txn = kwargs['txn']
        #we stored the amount in the memo attribute for fast retrieval
        amount = D(payment_details_txn.value('memo'))
        # Record payment source and event
        source_type, is_created = SourceType.objects.get_or_create(
            name='PayPal')
        source = Source(source_type=source_type,
                        currency=payment_details_txn.currency,
                        amount_allocated=amount,
                        amount_debited=amount)
        self.add_payment_source(source)
        self.add_payment_event('Settled', amount,
                               reference=confirm_txn.correlation_id)


    def validate_shipping_address(self, email, shipping_address):
        try:
            conf_code, street_match, zip_match, country_code = fetch_address_details(email, shipping_address)
        except PayPalError:
            logger.critical("PayPal address_verify api call failed")
            messages.error(self.request, _("Either the postal code or the street address is invalid.<br/>"
                                           "Make sure they both match the format you have on file at PayPal"),
            extra_tags='safe block')
            return False

        #invalid paypal email address
        if street_match == 'none':
            logger.error("PayPal: invalid email address")
            # we should not get here - redirect to basket page with warning message
            messages.error(self.request, _("Email address was not found on file at PayPal."))
            return False

        #check street match
        if street_match.lower() != 'matched':
            logger.error("PayPal: Unmatched street found")
            # unmatched street - redirect to basket page with warning message
            messages.error(self.request, _("The street address doesn't match any street address on file at PayPal.<br/>"
                                           "Make sure you deliver your package to an address listed on your PayPal account."),
                           extra_tags='safe block')
            return False

        #check postal code match
        if zip_match.lower() != 'matched':
            logger.error("PayPal: Unmatched postal code found")
            # unmatched zip code - redirect to basket page with warning message
            messages.error(self.request, _("The postal code doesn't match any postal code on file at PayPal.<br/>"
                                           "Make sure you deliver your package to an address listed on your PayPal account."),
                           extra_tags='safe block')
            return False

        usendhome_country_code = shipping_address.country.iso_3166_1_a2
        #check country match
        if country_code != usendhome_country_code:
            logger.error("PayPal: Unmatched shipping country, paypal country code:%s, "
                         "USendHome country code: %s" % (country_code, usendhome_country_code))
            # unmatched country - redirect to basket page with warning message
            messages.error(self.request, _("The destination country doesn't match any destination country on"
                                           " file at PayPal.<br/>"
                                           "Make sure you deliver your package to an address listed on your PayPal account."),
                            extra_tags='safe block')
            return False

        return True


    def unfreeze_basket(self, basket_id):
        basket = self.load_frozen_basket(basket_id)
        basket.thaw()

    def get_account_status(self, first_name, last_name, email):
        return fetch_account_status(first_name, last_name, email)


    def validate_txn(self, sender_email, sender_first_name,
                     sender_last_name, is_return_to_merchant, sender_shipping_address):
        #check that the buyer is verified
        try:
            account_status = self.get_account_status(first_name=sender_first_name,
                                                     last_name=sender_last_name,
                                                     email=sender_email)
        except PayPalError:
            #we couldn't get account status, this is probably becasue the credentials
            #don't match the data on file at PayPal
            logger.error("We couldn't fetch Paypal account status for user: %s %s" % (sender_first_name, sender_last_name))
            messages.error(self.request, _("Email address, first and last names must match the information on"
                                           " file at PayPal. Please edit your settings and try again."))
            return False

        if account_status.lower() != 'verified':
            logger.error("unverified payer found: %s %s" % (sender_first_name, sender_last_name))
            # unverified payer - redirect to pending packages page with error message
            messages.error(self.request, _("Your PayPal account isn't verified, please verify your account before"
                                           " proceeding to checkout."))
            return False


        #check shipping address only for non merchant addresses
        #this check is not needed for US addresses where the package is returned to store
        if not is_return_to_merchant:
            if not self.validate_shipping_address(sender_email, sender_shipping_address):
                return False

        #all went fine continue with payment
        return True


    def get_shipping_method(self, basket, shipping_address=None, **kwargs):
        """
        Return the shipping method used in session
        """
        shipping_method = super(SuccessResponseView, self).get_shipping_method(basket)
        return shipping_method

    def get_shipping_address(self, basket):
        """
        Return the shipping address as entered on our site
        """
        shipping_addr = super(SuccessResponseView, self).get_shipping_address(basket)
        return shipping_addr

    def submit(self, user, basket, shipping_address, shipping_method,
               order_total, payment_kwargs=None, order_kwargs=None):
        """
        Since we fallback to no shipping required, we must enforce that the only case its allowed
        is when customer returns items back to merchant and he provided us with a return label
        in all other cases, we must redirect to pending packages page, display a message to the customer
        and log this incident
        """
        if shipping_method.code == 'no-shipping-required' \
            and not self.checkout_session.is_return_to_store_prepaid_enabled():
            logger.critical("Placing an order, no shipping method was found, fallback to no shipping required,"
                            " user #%s" % user.id)
            messages.error(self.request, _("It seems that you've been idle for too long, please re-place your order."))
            return HttpResponseRedirect(reverse('customer:pending-packages'))

        return super(SuccessResponseView, self).submit(user, basket, shipping_address, shipping_method,
                                                      order_total, payment_kwargs, order_kwargs)


class CancelResponseView(RedirectView):
    permanent = False

    def get(self, request, *args, **kwargs):
        basket = get_object_or_404(Basket, id=kwargs['basket_id'],
                                   status=Basket.FROZEN)
        basket.thaw()
        logger.info("Payment cancelled (token %s) - basket #%s thawed",
                    request.GET.get('token', '<no token>'), basket.id)
        return super(CancelResponseView, self).get(request, *args, **kwargs)

    def get_redirect_url(self, **kwargs):
        messages.error(self.request, _("PayPal transaction cancelled"))
        return reverse('basket:summary')
