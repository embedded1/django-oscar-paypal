from oscar.core.loading import get_class
from decimal import ROUND_FLOOR, Decimal as D
from django.http import HttpResponseRedirect, HttpResponseNotAllowed
from django.db.models import get_model
from django.views import generic
from django.shortcuts import get_object_or_404
from django.conf import settings
from paypal.exceptions import PayPalError
from paypal.adaptive.exceptions import (
    EmptyBasketException, MissingShippingAddressException,
    MissingShippingMethodException, InvalidBasket,
    PayPalFailedValidationException, GeneralException)
from paypal.adaptive.facade import (
    get_pay_request_attrs, fetch_account_info,
    set_transaction_details)
from paypal.express.facade import fetch_address_details
from django.contrib import messages
from django.utils import six
from django.core.urlresolvers import reverse
from django.utils.translation import ugettext as _
from paypal.adaptive.mixins import PaymentSourceMixin
import logging

TWO_PLACES = D('0.01')

# Load views dynamically
PaymentDetailsView = get_class('checkout.views', 'PaymentDetailsView')
CheckoutSessionMixin = get_class('checkout.session', 'CheckoutSessionMixin')
Repository = get_class('shipping.repository', 'Repository')
Applicator = get_class('offer.utils', 'Applicator')
Source = get_model('payment', 'Source')
Order = get_model('order', 'Order')
SourceType = get_model('payment', 'SourceType')
Basket = get_model('basket', 'Basket')
logger = logging.getLogger('paypal.adaptive')


class RedirectView(PaymentSourceMixin, generic.RedirectView):
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
            url = self._get_redirect_url(**kwargs)
        except PayPalError:
            messages.error(
                self.request, _("An error occurred communicating with PayPal"))
            if self.as_payment_method:
                url = reverse('checkout:payment-details')
            else:
                url = reverse('customer:pending-packages')
            return url
        except InvalidBasket as e:
            messages.warning(self.request, six.text_type(e))
            return reverse('customer:pending-packages')
        except EmptyBasketException:
            messages.error(self.request, _("Your basket is empty"))
            return reverse('customer:pending-packages')
        except MissingShippingAddressException:
            messages.error(
                self.request, _("A shipping address must be specified"))
            return reverse('checkout:shipping-address')
        except MissingShippingMethodException:
            messages.error(
                self.request, _("A shipping method must be specified"))
            return reverse('checkout:shipping-method')
        except PayPalFailedValidationException:
            return reverse('customer:pending-packages')
        except GeneralException:
            messages.error(
                self.request, _("Something went terribly wrong, please try again later"))
            return reverse('customer:pending-packages')
        else:
            # Transaction successfully registered with PayPal.  Now freeze the
            # basket so it can't be edited while the customer is on the PayPal
            # site.
            self.basket.freeze()

            logger.info("Basket #%s - redirecting to %s", self.basket.id, url)

            return url

    def store_pay_key(self, pay_key):
        """
        We save Pay request pay_key to identify the Pay transaction
        """
        self.checkout_session.store_pay_key(pay_key)


    def get_receivers(self):
        """
        This function returns the payment receivers, we support 2 options:
        1 - Order payment is split between USendHome and the partner, in that case
            we calculate partner's share
        2 - Order payment isn't split and transferred as a whole to USendHome
        To determine in what option are we, we check if PartnerOrderPaymentSettings object
        is available for package's partner, if it exists, we follow it and divide the payment
        between UsendHome and the partner, otherwise, we take it all.
        """
        partner_share = D('0.0')
        receivers = [
            {
                'email': settings.PAYPAL_PRIMARY_RECEIVER_EMAIL,
                'is_primary': True,
                'amount': self.basket.total_incl_tax
            }
        ]

        #Find out if we need to transfer funds to partner
        partner = self.package.stockrecords.all().prefetch_related(
            'partner', 'partner__payments_settings')[0].partner
        partner_order_payment_settings = partner.active_payment_settings

        if partner_order_payment_settings:
            partner_share = self.calc_partner_share(partner_order_payment_settings)
            #store partner's share in sessions as we
            #would like to store it in DB for audit
            self.store_partner_share(partner_share)
            receivers.append({
                'email': partner_order_payment_settings.billing_email,
                'is_primary': False,
                'amount': partner_share
            })

        #referral program discount can turn the table on us and we must adapt for
        #cases where the secondary receiver share is bigger than the total order
        #in such cases our share will be 0
        if partner_share > self.basket.total_incl_tax:
            try:
                secondary_receiver = receivers[1]
            except IndexError:
                pass
            else:
                secondary_receiver['amount'] = self.basket.total_incl_tax
                #update partner share
                self.store_partner_share(self.basket.total_incl_tax)

        return receivers

    def align_receivers(self, params):
        #The default Pay actionType is PAY_PRIMARY for chained payments
        #we need to set it to Pay in case no secondary receiver exists
        if len(params['receivers']) == 1:
            params['action'] = 'PAY'
            #Only 1 receiver, need to change is_primary to False
            params['receivers'][0]['is_primary'] = False
        else:
            params['action'] = 'PAY_PRIMARY'

    def add_shipping_address_to_tran(self, pay_key, shipping_address):
        set_transaction_details(
            pay_key=pay_key,
            shipping_address=shipping_address)

    def _get_redirect_url(self, **kwargs):
        if self.basket.is_empty:
            raise EmptyBasketException()

        #user = self.request.user
        customer_shipping_address = self.get_shipping_address(self.basket)
        is_return_to_merchant = self.checkout_session.is_return_to_store_enabled()

        #check that shipping address exists
        if not customer_shipping_address and not is_return_to_merchant:
            # we could not get shipping address - redirect to basket page with warning message
            logger.warning("customer's shipping address not found while verifying PayPal account")
            self.unfreeze_basket(kwargs['basket_id'])
            raise MissingShippingMethodException()

        #Run some validations on the user
        #if not self.validate_txn(sender_email=user.email,
        #                         sender_first_name=user.first_name,
        #                         sender_last_name=user.last_name,
        #                         sender_shipping_address=customer_shipping_address,
        #                         return_to_merchant=is_return_to_merchant):
        #    raise PayPalFailedValidationException()

        params = {
            'basket': self.basket,
            'sender_email': self.request.user.email
        }

        if settings.DEBUG:
            # Determine the localserver's hostname to use when
            # in testing mode
            params['host'] = self.request.META['HTTP_HOST']

        params['paypal_params'] = self._get_paypal_params()

        params['receivers'] = self.get_receivers()
        self.align_receivers(params)

        redirect_url, pay_key = get_pay_request_attrs(**params)
        self.store_pay_key(pay_key)
        self.store_pay_payment_method('PayPal Account')
        #add shipping address to the transaction before we redirect to PayPal
        self.add_shipping_address_to_tran(
            pay_key=pay_key,
            shipping_address=customer_shipping_address)
        return redirect_url


    def _get_paypal_params(self):
        """
        Return any additional PayPal parameters
        """
        return {}

    def validate_txn(self, sender_email, sender_first_name, sender_last_name,
                     return_to_merchant, sender_shipping_address):
        #make sure user has verified Paypal account and account data is valid
        if not self.validate_paypal_account(
                sender_email, sender_first_name,
                sender_last_name):
            return False

        #check shipping address only for non merchant addresses
        #this check is not needed for US addresses where the package is returned to store
        #if not return_to_merchant:
        #    if not self.validate_shipping_address(sender_email, sender_shipping_address):
        #        return False

        #all went fine continue with payment
        return True

    def get_account_status(self, first_name, last_name, email):
        return fetch_account_info(first_name, last_name, email)

    def validate_paypal_account(self, sender_email, sender_first_name, sender_last_name):
        #Get account info from PP
        try:
            (pp_account_status,
             pp_account_email,
             pp_account_first_name,
             pp_account_last_name) = self.get_account_status(first_name=sender_first_name,
                                                             last_name=sender_last_name,
                                                             email=sender_email)
        except PayPalError:
            #we couldn't get account status, this is probably because the credentials
            #don't match the data on file at PayPal
            #logger.error("Cannot determine PayPal account status: %s %s" % (sender_first_name, sender_last_name))
            # unverified payer - redirect to pending packages page with error message
            messages.error(self.request, _("A problem occurred communicating with PayPal.<br/>"
                                           "Please make sure your USendHome account name and email address"
                                           " are completely identical to the data on your PayPal account."),
                           extra_tags='safe')
            return False

        if any([pp_account_status is None,
                pp_account_email is None,
                pp_account_first_name is None,
                pp_account_last_name is None]):
            logger.error("We didn't receive all data from PayPal through the AdaptiveAccounts API")
            # unverified payer - redirect to pending packages page with error message
            messages.error(self.request, _("A problem occurred communicating with PayPal "
                                           "- please try again later"))
            return False


        if pp_account_status.lower() != 'verified':
            #logger.error("unverified payer found: %s %s" % (sender_first_name, sender_last_name))
            # unverified payer - redirect to pending packages page with error message
            messages.error(self.request, _("Your PayPal account isn't verified.<br/>"
                                           "We only accept payments from verified PayPal accounts.<br/>"
                                           "Please complete the payment with your credit or debit card."),
                           extra_tags='safe')
            return False


        #make sure the paypal email address is identical to the email address the customer
        #uses on site
        if sender_email.strip().lower() != pp_account_email.strip().lower():
            #logger.error("paypal email address %s does not match on site email address: %s"
            #             % (pp_account_email, sender_email))
            messages.error(self.request, _("Your PayPal email address doesn't match USendHome email address.<br/>"
                                           " Please edit your settings and try again."),
                           extra_tags='safe')
            return False

        #check that the paypal account name is same as the one on site
        if pp_account_first_name.strip().lower() != sender_first_name.strip().lower() or \
           pp_account_last_name.strip().lower() != sender_last_name.strip().lower():
            #logger.error("PayPal account name does not match USendHome account name: %s %s, paypal name: %s %s"
            #             % (sender_first_name, sender_last_name, pp_account_first_name, pp_account_last_name))
            messages.error(self.request, _("Your PayPal account name doesn't match USendHome account name<br/>"
                                           "Please contact customer support."),
                           extra_tags='safe block')
            return False

        return True

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


        #can't select different country than their home country
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

class GuestRedirectView(RedirectView):
    def _get_redirect_url(self, **kwargs):
        """
        Guest payment allows customer with no PayPal account to
        transfer the payment using a credit card instead of using their
        PayPal account balance. We don't run the standard PayPal validations
        as there's no such account.
        """
        if self.basket.is_empty:
            raise EmptyBasketException()

        customer_shipping_address = self.get_shipping_address(self.basket)
        is_return_to_merchant = self.checkout_session.is_return_to_store_enabled()

        #check that shipping address exists
        if not customer_shipping_address and not is_return_to_merchant:
            # we could not get shipping address - redirect to basket page with warning message
            logger.warning("customer's shipping address not found while verifying PayPal account")
            self.unfreeze_basket(kwargs['basket_id'])
            raise MissingShippingMethodException()

        params = {
            'basket': self.basket
        }

        if settings.DEBUG:
            # Determine the localserver's hostname to use when
            # in testing mode
            params['host'] = self.request.META['HTTP_HOST']

        params['paypal_params'] = self._get_paypal_params()

        params['receivers'] = self.get_receivers()
        self.align_receivers(params)

        redirect_url, pay_key = get_pay_request_attrs(**params)
        self.store_pay_key(pay_key)
        self.store_pay_payment_method('Credit Card')
        #add shipping address to the transaction before we redirect to PayPal
        self.add_shipping_address_to_tran(
            pay_key=pay_key,
            shipping_address=customer_shipping_address)
        return redirect_url

class SuccessResponseView(PaymentSourceMixin, PaymentDetailsView):
    has_error = False
    preview = True
    err_msg = _("A problem occurred communicating with PayPal "
                "- please try again later")

    def get_pay_key(self):
        self.pay_key = self.checkout_session.get_pay_key()

    def post(self, request, *args, **kwargs):
        """
        We only support GET request
        """
        return HttpResponseNotAllowed(permitted_methods='GET')

    def get(self, request, *args, **kwargs):
        """
        Place an order.

        We fetch the txn details again and then proceed with oscar's standard
        payment details view for placing the order.
        """
        error_msg = _(
            "A problem occurred communicating with PayPal "
            "- please try again later"
        )

        self.get_pay_key()
        if self.pay_key is None:
            #Some PayPal transactions redirect more than once to this page
            #in the second time the pay_key will not be in session so we can continue
            #with the flow
            return HttpResponseRedirect(reverse('checkout:thank-you'))

        # Reload frozen basket which is specified in the URL
        basket = self.load_frozen_basket(kwargs['basket_id'])
        if not basket:
            logger.error("SuccessResponseView: no basket found")
            messages.error(self.request, error_msg)
            return HttpResponseRedirect(reverse('customer:pending-packages'))

        submission = self.build_submission(basket=basket)
        try:
            self.submit(**submission)
        except ValueError:
            #Order already exists, continue with the regular flow
            pass
        #we don't display the error message here but we redirect to
        #pending packages page
        if self.has_error:
            logger.critical("SuccessResponseView, error in submitting the order, the paypal transaction"
                            " has already been carried")
            return HttpResponseRedirect(reverse('customer:pending-packages'))
        #Order placement process has successfully finished,
        #redirect to thank you page
        return HttpResponseRedirect(reverse('checkout:thank-you'))


    def get_payment_method(self):
        return self.checkout_session.get_pay_payment_method()

    def handle_payment(self, order_number, total, **kwargs):
        """
        Save order related data into DB
        We keep the pay key in the payment source reference attribute so we
        could finish the payment for the secondary receiver.
        Payment event contains the Pay request transaction id for audit
        The payment source stores the following:
        1 - amount_allocated = Order total value
        2 - partner_share = Partner's share
        3 - self_share = USendHome's share
        """
        # Record payment source and event
        partner_share = self.get_partner_share()
        payment_method = self.get_payment_method()
        partner_payment_settings = self.get_partner_payment_settings()
        source_type, is_created = SourceType.objects.get_or_create(name='PayPal')
        source = Source(source_type=source_type,
                        currency=getattr(settings, 'PAYPAL_CURRENCY', 'USD'),
                        amount_allocated=total.incl_tax,
                        partner_share=partner_share,
                        self_share=total.incl_tax - partner_share,
                        partner_paid_shipping_costs=partner_payment_settings['paid_shipping_costs'],
                        partner_paid_shipping_insurance=partner_payment_settings['paid_shipping_insurance'],
                        reference=self.pay_key,
                        label=payment_method)
        self.add_payment_source(source)
        self.add_payment_event('Settled', total.incl_tax)
        #delete partner's share from session
        self.checkout_session.delete_partner_share()
        #delete the paypal_ap attributes from session
        self.checkout_session.delete_paypal_ap()


class CancelResponseView(generic.RedirectView):
    permanent = False

    def get(self, request, *args, **kwargs):
        basket = get_object_or_404(Basket, id=kwargs['basket_id'],
                                   status=Basket.FROZEN)
        basket.thaw()
        logger.info("Payment cancelled - basket #%s thawed", basket.id)
        return super(CancelResponseView, self).get(request, *args, **kwargs)

    def get_redirect_url(self, **kwargs):
        messages.error(self.request, _("PayPal transaction cancelled"))
        return reverse('checkout:payment-method')
