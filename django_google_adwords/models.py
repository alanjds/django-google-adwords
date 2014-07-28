import os
import re
import time
import pytz
import logging
from django.db import models
from django.db.models.query import QuerySet as _QuerySet
from django_toolkit.db.models import QuerySetManager
from money.contrib.django.models.fields import MoneyField
from django.db.models.query import QuerySet
from datetime import date, timedelta, datetime
from .settings import GoogleAdwordsConf
from django.conf import settings
from .lock import acquire_googleadwords_lock
from django_google_adwords.lock import release_googleadwords_lock
from celery.contrib.methods import task
from django_google_adwords.helper import adwords_service, paged_request
from celery.canvas import group
from django_toolkit.models.decorators import refresh
from contextlib import contextmanager
from django_toolkit.file import tempfile
from django.core.files.base import File
import xmltodict
from django_google_adwords.errors import *
from django.db.models.signals import post_delete
from googleads.errors import GoogleAdsError
from decimal import Decimal
from django.utils import timezone
from django.db.models.fields.related import ForeignKey
from django.db.models.fields import FieldDoesNotExist, DecimalField
from celery.app import shared_task
from django_toolkit.celery.decorators import ensure_self
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models.aggregates import Sum
from django.db.models import Max

class AdwordsDataInconsistency(Exception): pass

logger = logging.getLogger(__name__)

first_cap_re = re.compile('(.)([A-Z][a-z]+)')
all_cap_re = re.compile('([a-z0-9])([A-Z])')

def attribute_to_field_name(attribute):
    s1 = first_cap_re.sub(r'\1_\2', attribute[1:])
    return all_cap_re.sub(r'\1_\2', s1).lower()

class PopulatingGoogleAdwordsQuerySet(_QuerySet):
    IGNORE_FIELDS = ['created', 'updated']

    def populate_model_from_dict(self, model, data, ignore_fields=[]):
        update_fields = []
        
        def to_field_name(key):
            return attribute_to_field_name(key)
        
        def clean(value, field):
            # If the adwords api returns "--" regardless of the field we want to return None
            if value == '--':
                return None
            
            # If money divide by 1,000,000 to get dollars/cents
            elif isinstance(field, MoneyField):
                if int(value) > 0:
                    return Decimal(value)/1000000
                return Decimal(value)
            
            # The adwords api returns "1.87%" or "< 10%" for percentage fields we need to remove the % < > signs
            elif isinstance(field, DecimalField):
                mapping = [('%', ''), ('<', ''), ('>', ''), (',', ''), (' ', '')]
                for k, v in mapping:
                    value = value.replace(k, v)
                return value
            
            # The api returns data in a way we can handle
            else:
                return value
        
        for key, _value in data.items():
            field_name = to_field_name(key)
            if field_name in self.IGNORE_FIELDS or field_name in ignore_fields:
                continue
            try:
                field = model._meta.get_field(field_name)
            except FieldDoesNotExist:
                # Skip fields that dont exist in the model
                continue
            
            value = clean(_value, field)
            try:
                value = field.to_python(value)
            except DjangoValidationError as e:
                raise ValidationError(field_name, e.messages)
            
            if value != getattr(model, field_name):
                update_fields.append(field_name)
                setattr(model, field_name, value)
        
        return update_fields

    def _populate(self, data, ignore_fields=[], **kwargs):
        """
        Low level get or create model which then populates the model with data.
        
        :param data: A dict of data as retrieved from the Google Adwords API.
        :param **kwargs: Keyword args that are supplied to retrieve the model instance
                         and also to generate an instance if one does not exist.
        :return: models.Model
        """
        model_cls = self.model
        try:
            model = model_cls.objects.get(**kwargs)
        except model_cls.DoesNotExist:
            model = model_cls(**kwargs)
        update_fields = self.populate_model_from_dict(model, data, ignore_fields)
        if model.pk is None:
            model.save()
        else:
            model.save(update_fields=update_fields)
        return model

class Account(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_SYNC = 'sync'
    STATUS_INACTIVE = 'inactive'
    STATUS_CHOICES = (
        (STATUS_ACTIVE, 'Active'),
        (STATUS_SYNC, 'Sync'),
        (STATUS_INACTIVE, 'Inactive'),
    )
    STATUS_CONSIDERED_ACTIVE = (STATUS_ACTIVE, STATUS_SYNC,)

    account_id = models.BigIntegerField(unique=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    account = models.CharField(max_length=255, blank=True, null=True, help_text='Account descriptive name')
    currency = models.CharField(max_length=255, blank=True, null=True, help_text='Account currency code')
    account_last_synced = models.DateField(blank=True, null=True)
    campaign_last_synced = models.DateField(blank=True, null=True)
    ad_group_last_synced = models.DateField(blank=True, null=True)
    ad_last_synced = models.DateField(blank=True, null=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True, auto_now_add=True)

    objects = QuerySetManager()

    def __unicode__(self):
        return '%s' % (self.account_id)
    
    class QuerySet(PopulatingGoogleAdwordsQuerySet):
        
        def active(self):
            return Account.objects.filter(status=Account.STATUS_ACTIVE)
        
        def populate(self, data, account):
            """
            A locking get_or_create - note only the account_id is used in the 'get'.
            """
            # Get a lock based upon the campaign id
            while not acquire_googleadwords_lock(Account, account.account_id):
                logger.debug("Waiting for acquire_googleadwords_lock: %s:%s", Account.__name__, account.account_id)
                time.sleep(settings.GOOGLEADWORDS_LOCK_WAIT)
               
            try:
                logger.debug("Success acquire_googleadwords_lock: %s:%s", Account.__name__, account.account_id)
                return self._populate(data, 
                                      ignore_fields=['status', 'account_id', 'account_last_synced'], 
                                      account_id=account.account_id)
            
            finally:
                logger.debug("Releasing acquire_googleadwords_lock: %s:%s", Account.__name__, account.account_id)
                release_googleadwords_lock(Account, account.account_id)
    
    @task(name='Account.sync', ignore_result=True, queue=settings.GOOGLEADWORDS_CELERY_QUEUE)
    def sync(self, start=None, force=False):
        """
        Sync all data from Google Adwords API for this account.
        
        Retrieve and populate the following reports from the Google Adwords API.
        
        - Account Performance Report
        - Campaign Performance Report
        - Ad Group Performance Report
        - Ad Performance Report
        """
        
        self.start_sync()
        tasks = []
        
        """
        Account
        """
        if settings.GOOGLEADWORDS_SYNC_ACCOUNT:
            if not force and not self.account_last_synced:
                account_start = date.today() - timedelta(days=settings.GOOGLEADWORDS_NEW_ACCOUNT_ACCOUNT_SYNC_DAYS)
            elif not force and self.account_last_synced:
                account_start = self.account_last_synced - timedelta(days=settings.GOOGLEADWORDS_EXISTING_ACCOUNT_SYNC_DAYS)
            elif force and start:
                account_start = start
            tasks.append(self.create_report_file.si(Account.get_selector(start=account_start)) | self.sync_account.s(this=self) | self.finish_account_sync.s(this=self))
            
        """
        Campaign
        """
        if settings.GOOGLEADWORDS_SYNC_CAMPAIGN:
            if not force and not self.campaign_last_synced:
                campaign_start = date.today() - timedelta(days=settings.GOOGLEADWORDS_NEW_ACCOUNT_CAMPAIGN_SYNC_DAYS)
            elif not force and self.campaign_last_synced:
                campaign_start = self.campaign_last_synced - timedelta(days=settings.GOOGLEADWORDS_EXISTING_CAMPAIGN_SYNC_DAYS)
            elif force and start:
                campaign_start = start
            tasks.append(self.create_report_file.si(Campaign.get_selector(start=campaign_start)) | self.sync_campaign.s(this=self) | self.finish_campaign_sync.s(this=self))
            
        """
        Ad Group
        """
        if settings.GOOGLEADWORDS_SYNC_ADGROUP:
            if not force and not self.ad_group_last_synced:
                ad_group_start = date.today() - timedelta(days=settings.GOOGLEADWORDS_NEW_ACCOUNT_ADGROUP_SYNC_DAYS)
            elif not force and self.ad_group_last_synced:
                ad_group_start = self.ad_group_last_synced - timedelta(days=settings.GOOGLEADWORDS_EXISTING_ADGROUP_SYNC_DAYS)
                print ad_group_start
            elif force and start:
                ad_group_start = start
            tasks.append(self.create_report_file.si(AdGroup.get_selector(start=ad_group_start)) | self.sync_ad_group.s(this=self) | self.finish_ad_group_sync.s(this=self))
            
        """
        Ad
        """
        if settings.GOOGLEADWORDS_SYNC_AD:
            if not force and not self.ad_last_synced:
                ad_start = date.today() - timedelta(days=settings.GOOGLEADWORDS_NEW_ACCOUNT_AD_SYNC_DAYS)
            elif not force and self.ad_last_synced:
                ad_start = self.ad_last_synced - timedelta(days=settings.GOOGLEADWORDS_EXISTING_AD_SYNC_DAYS)
            elif force and start:
                ad_start = start
            tasks.append(self.create_report_file.si(Ad.get_selector(start=ad_start)) | self.sync_ad.s(this=self) | self.finish_ad_sync.s(this=self))

        canvas = group(*tasks) | self.finish_sync.s(this=self)
        canvas.apply_async()
    
    @task(name='Account.start_sync', ignore_result=True, queue=settings.GOOGLEADWORDS_CELERY_QUEUE)
    @refresh
    def start_sync(self):
        self.status = self.STATUS_SYNC
        self.save(update_fields=['status'])
    
    @task(name='Account.finish_sync', queue=settings.GOOGLEADWORDS_CELERY_QUEUE)
    @ensure_self
    @refresh
    def finish_sync(self, ignore_result=True):
        self.status = self.STATUS_ACTIVE
        self.save(update_fields=['updated', 'status'])
        
    @task(name='Account.finish_account_sync', queue=settings.GOOGLEADWORDS_CELERY_QUEUE)
    @ensure_self
    @refresh
    def finish_account_sync(self, ignore_result=True):
        self.account_last_synced = None
        account_last_synced = DailyAccountMetrics.objects.filter(account=self).aggregate(Max('day'))
        if account_last_synced.has_key('day__max'):
            self.account_last_synced = account_last_synced['day__max']
        self.save(update_fields=['updated', 'account_last_synced'])
        
    @task(name='Account.finish_campaign_sync', queue=settings.GOOGLEADWORDS_CELERY_QUEUE)
    @ensure_self
    @refresh
    def finish_campaign_sync(self, ignore_result=True):
        self.campaign_last_synced = None
        campaign_last_synced = DailyCampaignMetrics.objects.filter(campaign__account=self).aggregate(Max('day'))
        if campaign_last_synced.has_key('day__max'):
            self.campaign_last_synced = campaign_last_synced['day__max']
        self.save(update_fields=['updated', 'campaign_last_synced'])
        
    @task(name='Account.finish_ad_group_sync', queue=settings.GOOGLEADWORDS_CELERY_QUEUE)
    @ensure_self
    @refresh
    def finish_ad_group_sync(self, ignore_result=True):
        self.ad_group_last_synced = None
        ad_group_last_synced = DailyAdGroupMetrics.objects.filter(ad_group__campaign__account=self).aggregate(Max('day'))
        if ad_group_last_synced.has_key('day__max'):
            self.ad_group_last_synced = ad_group_last_synced['day__max']
        self.save(update_fields=['updated', 'ad_group_last_synced'])
        
    @task(name='Account.finish_ad_sync', queue=settings.GOOGLEADWORDS_CELERY_QUEUE)
    @ensure_self
    @refresh
    def finish_ad_sync(self, ignore_result=True):
        self.ad_last_synced = None
        ad_last_synced = DailyAdMetrics.objects.filter(ad__ad_group__campaign__account=self).aggregate(Max('day'))
        if ad_last_synced.has_key('day__max'):
            self.ad_last_synced = ad_last_synced['day__max']
        self.save(update_fields=['updated', 'ad_last_synced'])
    
    @task(name='Account.get_account_data', queue=settings.GOOGLEADWORDS_CELERY_QUEUE)
    def create_report_file(self, report_definition):
        """
        Create a ReportFile that contains the Google Adwords data as specified by report_definition.
        """
        try:
            return ReportFile.objects.request(report_definition=report_definition,
                                              client_customer_id=self.account_id)
        except RateExceededError, exc:
            logger.info("Caught RateExceededError for account '%s' - retrying in '%s' seconds.", self.pk, exc.retry_after_seconds)
            raise self.get_account_data.retry(exc, countdown=exc.retry_after_seconds)
        except GoogleAdsError, exc:
            raise InterceptedGoogleAdsError(exc, account_id=self.account_id)
    
    @task(name='Account.sync_account', queue=settings.GOOGLEADWORDS_CELERY_QUEUE)
    @ensure_self
    @refresh
    def sync_account(self, report_file):
        """
        Sync the account data report.
        
        :param report_file: ReportFile
        """
        dehydrated_report = report_file.dehydrate()
        try:
            for row in dehydrated_report['report']['table']['row']:
                account = Account.objects.populate(row, self)
                DailyAccountMetrics.objects.populate(row, account=account)
                
        except KeyError as e:
            logger.info("Caught KeyError syncing account '%s', report_file '%s' - Report doesn't have expected rows", self.pk, report_file.pk)
            raise
        
    @task(name='Account.sync_campaign', queue=settings.GOOGLEADWORDS_CELERY_QUEUE)
    @ensure_self
    @refresh
    def sync_campaign(self, report_file):
        """
        Sync the campaign data report.
        
        :param report_file: ReportFile
        """
        dehydrated_report = report_file.dehydrate()
        try:
            for row in dehydrated_report['report']['table']['row']:
                account = Account.objects.populate(row, self)
                campaign = Campaign.objects.populate(row, account=account)
                DailyCampaignMetrics.objects.populate(row, campaign=campaign)
                
        except KeyError as e:
            logger.info("Caught KeyError syncing campaign for account '%s', report_file '%s' - Report doesn't have expected rows", self.pk, report_file.pk)
            raise
    
    @task(name='Account.sync_ad_group', queue=settings.GOOGLEADWORDS_CELERY_QUEUE)
    @ensure_self
    @refresh
    def sync_ad_group(self, report_file):
        """
        Sync the ad group data report.
        
        :param report_file: ReportFile
        """
        dehydrated_report = report_file.dehydrate()
        try:
            for row in dehydrated_report['report']['table']['row']:
                account = Account.objects.populate(row, self)
                campaign = Campaign.objects.populate(row, account=account)
                ad_group = AdGroup.objects.populate(row, campaign=campaign)
                DailyAdGroupMetrics.objects.populate(row, ad_group=ad_group)
    
        except KeyError as e:
            logger.info("Caught KeyError syncing ad group for account '%s', report_file '%s' - Report doesn't have expected rows", self.pk, report_file.pk)
            raise
    
    @task(name='Account.sync_ad', queue=settings.GOOGLEADWORDS_CELERY_QUEUE)
    @ensure_self
    @refresh
    def sync_ad(self, report_file):
        """
        Sync the ad data report.
        
        :param report_file: ReportFile
        """
        dehydrated_report = report_file.dehydrate()
        try:
            for row in dehydrated_report['report']['table']['row']:
                account = Account.objects.populate(row, self)
                campaign = Campaign.objects.populate(row, account=account)
                ad_group = AdGroup.objects.populate(row, campaign=campaign)
                ad = Ad.objects.populate(row, ad_group=ad_group)
                DailyAdMetrics.objects.populate(row, ad=ad)
    
        except KeyError as e:
            logger.info("Caught KeyError syncing ad for account '%s', report_file '%s' - Report doesn't have expected rows", self.pk, report_file.pk)
            raise
        
    @staticmethod
    def get_selector(start=None, finish=None):
        """
        Returns the selector to pass to the api to get the data.
        """
        if not start:
            start = date.today() - timedelta(days=settings.GOOGLEADWORDS_EXISTING_ACCOUNT_SYNC_DAYS)
        if not finish:
            finish = date.today() - timedelta(days=1)
            
        report_definition = {
            'reportName': 'Account Performance Report',
            'dateRangeType': 'CUSTOM_DATE',
            'reportType': 'ACCOUNT_PERFORMANCE_REPORT',
            'downloadFormat': 'XML',
            'selector': {
                'fields': [
                           'AccountCurrencyCode',
                           'AccountDescriptiveName',
                           'AverageCpc',
                           'AverageCpm',
                           'AveragePosition',
                           'Clicks',
                           'ContentBudgetLostImpressionShare',
                           'ContentImpressionShare',
                           'ContentRankLostImpressionShare',
                           'ConversionRate',
                           'ConversionRateManyPerClick',
                           'ConversionValue',
                           'Conversions',
                           'ConversionsManyPerClick',
                           'Cost',
                           'CostPerConversion',
                           'CostPerConversionManyPerClick',
                           'CostPerEstimatedTotalConversion',
                           'Ctr',
                           'Device',
                           'EstimatedCrossDeviceConversions',
                           'EstimatedTotalConversionRate',
                           'EstimatedTotalConversionValue',
                           'EstimatedTotalConversionValuePerClick',
                           'EstimatedTotalConversionValuePerCost',
                           'EstimatedTotalConversions',
                           'Impressions',
                           'InvalidClickRate',
                           'InvalidClicks',
                           'SearchBudgetLostImpressionShare',
                           'SearchExactMatchImpressionShare',
                           'SearchImpressionShare',
                           'SearchRankLostImpressionShare',
                           'Date',
                           ],
                'dateRange': {
                              'min': start.strftime("%Y%m%d"),
                              'max': finish.strftime("%Y%m%d")
                              },
            },
            'includeZeroImpressions': 'true'
        }
        
        return report_definition
    
    def spend(self, start, finish):
        """
        @param start: the start date the the data is for.
        @param finish: the finish date you want the data for. 
        """
        if not self.last_synced or (self.last_synced - timedelta(days=1)) < finish:
            raise AdwordsDataInconsistency('Google Adwords Account %s does not have correct amount of data to calculate the spend between "%s" and "%s"' % (
                self, 
                start, 
                finish, 
            ))
        
        cost = self.account_metrics.filter(day__gte=start, day__lte=finish).aggregate(Sum('cost'))['cost__sum']
        
        if cost == None:
            return 0
        else:
            return cost

class Alert(models.Model):
    TYPE_ACCOUNT_ON_TARGET = 'ACCOUNT_ON_TARGET' 
    TYPE_DECLINED_PAYMENT = 'DECLINED_PAYMENT' 
    TYPE_CREDIT_CARD_EXPIRING = 'CREDIT_CARD_EXPIRING' 
    TYPE_ACCOUNT_BUDGET_ENDING = 'ACCOUNT_BUDGET_ENDING'
    TYPE_CAMPAIGN_ENDING = 'CAMPAIGN_ENDING'
    TYPE_PAYMENT_NOT_ENTERED = 'PAYMENT_NOT_ENTERED'
    TYPE_MISSING_BANK_REFERENCE_NUMBER = 'MISSING_BANK_REFERENCE_NUMBER'
    TYPE_CAMPAIGN_ENDED = 'CAMPAIGN_ENDED'
    TYPE_ACCOUNT_BUDGET_BURN_RATE = 'ACCOUNT_BUDGET_BURN_RATE'
    TYPE_USER_INVITE_PENDING = 'USER_INVITE_PENDING'
    TYPE_USER_INVITE_ACCEPTED = 'USER_INVITE_ACCEPTED'
    TYPE_MANAGER_LINK_PENDING = 'MANAGER_LINK_PENDING'
    TYPE_ZERO_DAILY_SPENDING_LIMIT = 'ZERO_DAILY_SPENDING_LIMIT'
    TYPE_TV_ACCOUNT_ON_TARGET = 'TV_ACCOUNT_ON_TARGET'
    TYPE_TV_ACCOUNT_BUDGET_ENDING = 'TV_ACCOUNT_BUDGET_ENDING'
    TYPE_TV_ZERO_DAILY_SPENDING_LIMIT = 'TV_ZERO_DAILY_SPENDING_LIMIT'
    TYPE_UNKNOWN = 'UNKNOWN'
    TYPE_CHOICES = (
        (TYPE_ACCOUNT_ON_TARGET, 'Account On Target'),
        (TYPE_DECLINED_PAYMENT, 'Declined Payment'),
        (TYPE_CREDIT_CARD_EXPIRING, 'Credit Card Expiring'),
        (TYPE_ACCOUNT_BUDGET_ENDING, 'Account Budget Ending'),
        (TYPE_CAMPAIGN_ENDING, 'Campaign Ending'),
        (TYPE_PAYMENT_NOT_ENTERED, 'Payment Not Entered'),
        (TYPE_MISSING_BANK_REFERENCE_NUMBER, 'Missing Bank Reference Number'),
        (TYPE_CAMPAIGN_ENDED, 'Campaign Ended'),
        (TYPE_ACCOUNT_BUDGET_BURN_RATE, 'Account Budget Burn Rate'),
        (TYPE_USER_INVITE_PENDING, 'User Invite Pending'),
        (TYPE_USER_INVITE_ACCEPTED, 'User Invite Accepted'),
        (TYPE_MANAGER_LINK_PENDING, 'Manager Link Pending'),
        (TYPE_ZERO_DAILY_SPENDING_LIMIT, 'Zero Daily Spending Limit'),
        (TYPE_TV_ACCOUNT_ON_TARGET, 'TV Account On Target'),
        (TYPE_TV_ACCOUNT_BUDGET_ENDING, 'TV Account Budget Ending'),
        (TYPE_TV_ZERO_DAILY_SPENDING_LIMIT, 'TV Zero Daily Spending Limit'),
        (TYPE_UNKNOWN, 'Unknown')
    )
    
    SEVERITY_GREEN = 'GREEN'
    SEVERITY_YELLOW = 'YELLOW'
    SEVERITY_RED = 'RED'
    SEVERITY_UNKNOWN = 'UNKNOWN'
    SEVERITY_CHOICES = (
        (SEVERITY_GREEN, 'Green'),
        (SEVERITY_YELLOW, 'Yellow'),
        (SEVERITY_RED, 'Red'),
        (SEVERITY_UNKNOWN, 'Unknown'),
    )
    
    account = models.ForeignKey('django_google_adwords.Account', related_name='alerts')
    type = models.CharField(max_length=100, choices=TYPE_CHOICES)
    severity = models.CharField(max_length=100, choices=SEVERITY_CHOICES)
    occurred = models.DateTimeField()
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True, auto_now_add=True)
    
    objects = QuerySetManager()
    
    class QuerySet(_QuerySet):
        pass
    
    @shared_task(name='Alert.sync_alerts')
    def sync_alerts():
        """
        Sync the alerts for the account.
        """
        for (data, selector) in paged_request(service='AlertService', selector=Alert.get_selector()):
            for row in data:
                try:
                    account = Account.objects.get(account_id=row.clientCustomerId)
                    
                    try:
                        parts = row.details[0].triggerTime.split(' ')
                        dt = datetime.strptime(parts[0] + parts[1], '%Y%m%d%H%M%S')
                        occurred = pytz.timezone(parts[2]).localize(dt)
                    except AttributeError, e:
                        logger.error("Could not create Alert as row didn't have a triggerTime.", exc_info=e)
                        continue
                    
                    alert, created = Alert.objects.get_or_create(account=account,
                                         type=row.alertType,
                                         severity=row.alertSeverity,
                                         occurred=occurred
                                         )
                except Account.DoesNotExist: pass
    
    @staticmethod
    def get_selector():
        return  {
            'query': {
                'clientSpec': 'ALL',
                'filterSpec': 'ALL',
                'types': ['ACCOUNT_BUDGET_BURN_RATE', 'ACCOUNT_BUDGET_ENDING',
                          'ACCOUNT_ON_TARGET', 'CAMPAIGN_ENDED', 'CAMPAIGN_ENDING',
                          'CREDIT_CARD_EXPIRING', 'DECLINED_PAYMENT',
                          'KEYWORD_BELOW_MIN_CPC', 'MANAGER_LINK_PENDING',
                          'MISSING_BANK_REFERENCE_NUMBER', 'PAYMENT_NOT_ENTERED',
                          'TV_ACCOUNT_BUDGET_ENDING', 'TV_ACCOUNT_ON_TARGET',
                          'TV_ZERO_DAILY_SPENDING_LIMIT', 'USER_INVITE_ACCEPTED',
                          'USER_INVITE_PENDING', 'ZERO_DAILY_SPENDING_LIMIT'],
                'severities': ['GREEN', 'YELLOW', 'RED'],
                'triggerTimeSpec': 'ALL_TIME'
            }
        }

class DailyAccountMetrics(models.Model):
    account = models.ForeignKey('django_google_adwords.Account', related_name='account_metrics')
    DEVICE_UNKNOWN = 'Other'
    DEVICE_DESKTOP = 'Computers'
    DEVICE_HIGH_END_MOBILE = 'Mobile devices with full browsers'
    DEVICE_TABLET = 'Tablets with full browsers'
    DEVICE_CHOICES = (
        (DEVICE_UNKNOWN, DEVICE_UNKNOWN),
        (DEVICE_DESKTOP, DEVICE_DESKTOP),
        (DEVICE_HIGH_END_MOBILE, DEVICE_HIGH_END_MOBILE),
        (DEVICE_TABLET, DEVICE_TABLET)
    )
    
    avg_cpc = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Avg. CPC', null=True, blank=True)
    avg_cpm = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Avg. CPM', null=True, blank=True)
    avg_position = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Avg. position')
    clicks = models.IntegerField(help_text='Clicks', null=True, blank=True)
    click_conversion_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Click conversion rate')
    conv_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Conv. rate')
    converted_clicks = models.BigIntegerField(help_text='Converted clicks', null=True, blank=True)
    total_conv_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Total conv. value')
    conversions = models.BigIntegerField(help_text='Conversions', null=True, blank=True)
    cost = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost', null=True, blank=True)
    cost_converted_click = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost / converted click', null=True, blank=True)
    cost_conv = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost / conv.', null=True, blank=True)
    ctr = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='CTR')
    device = models.CharField(max_length=255, choices=DEVICE_CHOICES, help_text='Device')
    impressions = models.BigIntegerField(help_text='Impressions', null=True, blank=True)
    day = models.DateField(help_text='When this metric occurred')
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True, auto_now_add=True)
    content_impr_share = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Content Impr. share')
    content_lost_is_rank = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Content Lost IS (rank)')
    cost_est_total_conv = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost / est. total conv.', null=True, blank=True)
    est_cross_device_conv = models.BigIntegerField(help_text='Est. cross-device conv.', null=True, blank=True)
    est_total_conv_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Est. total conv. rate')
    est_total_conv_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Est. total conv. value')
    est_total_conv_value_click = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Est. total conv. value / click')
    est_total_conv_value_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Est. total conv. value / cost')
    est_total_conv = models.BigIntegerField(help_text='Est. total conv.', null=True, blank=True)
    search_exact_match_is = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Search Exact match IS')
    search_impr_share = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Search Impr. share')
    search_lost_is_rank = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Search Lost IS (rank)')
    content_lost_is_budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Content Lost IS (budget)')
    invalid_click_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Invalid click rate')
    invalid_clicks = models.BigIntegerField(help_text='Invalid clicks', null=True, blank=True)
    search_lost_is_budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Search Lost IS (budget)')

    objects = QuerySetManager()
    
    class QuerySet(PopulatingGoogleAdwordsQuerySet):

        def populate(self, data, account):
            device = data.get('@device')
            day = data.get('@day')
            identifier = '%s-%s' % (device, day)
            
            while not acquire_googleadwords_lock(DailyAccountMetrics, identifier):
                logger.debug("Waiting for acquire_googleadwords_lock: %s:%s", DailyAccountMetrics.__name__, identifier)
                time.sleep(settings.GOOGLEADWORDS_LOCK_WAIT)
               
            try:
                logger.debug("Success acquire_googleadwords_lock: %s:%s", DailyAccountMetrics.__name__, identifier)
                return self._populate(data, ignore_fields=['account'], device=device, day=day, account=account)
            
            finally:
                logger.debug("Releasing acquire_googleadwords_lock: %s:%s", DailyAccountMetrics.__name__, identifier)
                release_googleadwords_lock(DailyAccountMetrics, identifier)

class Campaign(models.Model):
    STATE_ACTIVE = 'active'
    STATE_PAUSED = 'paused'
    STATE_DELETED = 'deleted'
    STATE_CHOICES = (
        (STATE_ACTIVE, 'Active'),
        (STATE_PAUSED, 'Paused'),
        (STATE_DELETED, 'Deleted')
    )
    
    account = models.ForeignKey('django_google_adwords.Account', related_name='campaigns')
    campaign_id = models.BigIntegerField(unique=True)
    campaign = models.CharField(max_length=255, help_text='Campaign name')
    campaign_state = models.CharField(max_length=20, choices=STATE_CHOICES)
    budget = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Budget', null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True, auto_now_add=True)

    objects = QuerySetManager()
    
    class QuerySet(PopulatingGoogleAdwordsQuerySet):

        def populate(self, data, account):
            """
            A locking get_or_create - note only the campaign_id is used in the 'get'.
            """
            campaign_id = int(data.get('@campaignID'))

            # Get a lock based upon the campaign id
            while not acquire_googleadwords_lock(Campaign, campaign_id):
                logger.debug("Waiting for acquire_googleadwords_lock: %s:%s", Campaign.__name__, campaign_id)
                time.sleep(settings.GOOGLEADWORDS_LOCK_WAIT)
               
            try:
                logger.debug("Success acquire_googleadwords_lock: %s:%s", Campaign.__name__, campaign_id)
                return self._populate(data, ignore_fields=['account'], campaign_id=campaign_id, account=account)
            
            finally:
                logger.debug("Releasing acquire_googleadwords_lock: %s:%s", Campaign.__name__, campaign_id)
                release_googleadwords_lock(Campaign, campaign_id)
    
    @staticmethod
    def get_selector(start=None, finish=None):
        """
        Returns the selector to pass to the api to get the data.
        """
        if not start:
            start = date.today() - timedelta(days=6)
        if not finish:
            finish = date.today() - timedelta(days=1)
            
        report_definition = {
            'reportName': 'Campaign Performance Report',
            'dateRangeType': 'CUSTOM_DATE',
            'reportType': 'CAMPAIGN_PERFORMANCE_REPORT',
            'downloadFormat': 'XML',
            'selector': {
                'fields': [
                            'AccountCurrencyCode',
                            'AccountDescriptiveName',
                            'Amount',
                            'AverageCpc',
                            'AverageCpm',
                            'AveragePosition',
                            'BiddingStrategyId',
                            'BiddingStrategyName',
                            'BiddingStrategyType',
                            'CampaignId',
                            'CampaignName',
                            'CampaignStatus',
                            'Clicks',
                            'ContentBudgetLostImpressionShare',
                            'ContentImpressionShare',
                            'ContentImpressionShare',
                            'ContentRankLostImpressionShare',
                            'ContentRankLostImpressionShare',
                            'ConversionRate',
                            'ConversionRateManyPerClick',
                            'ConversionValue',
                            'Conversions',
                            'ConversionsManyPerClick',
                            'Cost',
                            'CostPerConversion',
                            'CostPerConversionManyPerClick',
                            'CostPerEstimatedTotalConversion',
                            'CostPerEstimatedTotalConversion',
                            'Ctr',
                            'Device',
                            'EstimatedCrossDeviceConversions',
                            'EstimatedCrossDeviceConversions',
                            'EstimatedTotalConversionRate',
                            'EstimatedTotalConversionRate',
                            'EstimatedTotalConversions',
                            'EstimatedTotalConversions',
                            'EstimatedTotalConversionValue',
                            'EstimatedTotalConversionValue',
                            'EstimatedTotalConversionValuePerClick',
                            'EstimatedTotalConversionValuePerClick',
                            'EstimatedTotalConversionValuePerCost',
                            'EstimatedTotalConversionValuePerCost',
                            'Impressions',
                            'InvalidClickRate',
                            'InvalidClicks',
                            'SearchBudgetLostImpressionShare',
                            'SearchExactMatchImpressionShare',
                            'SearchExactMatchImpressionShare',
                            'SearchImpressionShare',
                            'SearchImpressionShare',
                            'SearchRankLostImpressionShare',
                            'SearchRankLostImpressionShare',
                            'Date',
                           ],
                'dateRange': {
                              'min': start.strftime("%Y%m%d"),
                              'max': finish.strftime("%Y%m%d")
                              },
            },
            'includeZeroImpressions': 'true'
        }
        
        return report_definition

class DailyCampaignMetrics(models.Model):
    BID_STRATEGY_TYPE_BUDGET_OPTIMIZER = 'auto'
    BID_STRATEGY_TYPE_CONVERSION_OPTIMIZER = 'max/target cpa'
    BID_STRATEGY_TYPE_MANUAL_CPC = 'cpc'
    BID_STRATEGY_TYPE_MANUAL_CPM = 'cpm'
    BID_STRATEGY_TYPE_PAGE_ONE_PROMOTED = 'Target search page location'
    BID_STRATEGY_TYPE_PERCENT_CPA = 'max cpa percent'
    BID_STRATEGY_TYPE_TARGET_SPEND = 'Maximize clicks'
    BID_STRATEGY_TYPE_ENHANCED_CPC = 'Enhanced CPC'
    BID_STRATEGY_TYPE_TARGET_CPA = 'Target CPA'
    BID_STRATEGY_TYPE_TARGET_ROAS = 'Target ROAS'
    BID_STRATEGY_TYPE_NONE = 'None'
    BID_STRATEGY_TYPE_UNKNOWN = 'unknown'
    BID_STRATEGY_TYPE_CHOICES = (
        (BID_STRATEGY_TYPE_BUDGET_OPTIMIZER, 'Budget Optimizer'),
        (BID_STRATEGY_TYPE_CONVERSION_OPTIMIZER, 'Conversion Optimizer'),
        (BID_STRATEGY_TYPE_MANUAL_CPC, 'Manual CPC'),
        (BID_STRATEGY_TYPE_MANUAL_CPM, 'Manual CPM'),
        (BID_STRATEGY_TYPE_PAGE_ONE_PROMOTED, 'Page One Promoted'),
        (BID_STRATEGY_TYPE_PERCENT_CPA, 'Percent CPA'),
        (BID_STRATEGY_TYPE_TARGET_SPEND, 'Target Spend'),
        (BID_STRATEGY_TYPE_ENHANCED_CPC, 'Enhanced CPC'),
        (BID_STRATEGY_TYPE_TARGET_CPA, 'Target CPA'),
        (BID_STRATEGY_TYPE_TARGET_ROAS, 'Target ROAS'),
        (BID_STRATEGY_TYPE_NONE, 'None'),
        (BID_STRATEGY_TYPE_UNKNOWN, 'Unknown')
    )
    
    DEVICE_UNKNOWN = 'Other'
    DEVICE_DESKTOP = 'Computers'
    DEVICE_HIGH_END_MOBILE = 'Mobile devices with full browsers'
    DEVICE_TABLET = 'Tablets with full browsers'
    DEVICE_CHOICES = (
        (DEVICE_UNKNOWN, DEVICE_UNKNOWN),
        (DEVICE_DESKTOP, DEVICE_DESKTOP),
        (DEVICE_HIGH_END_MOBILE, DEVICE_HIGH_END_MOBILE),
        (DEVICE_TABLET, DEVICE_TABLET)
    )
    
    campaign = models.ForeignKey('django_google_adwords.Campaign', related_name='metrics')
    avg_cpc = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Avg. CPC', null=True, blank=True)
    avg_cpm = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Avg. CPM', null=True, blank=True)
    avg_position = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Avg. position')
    clicks = models.IntegerField(help_text='Clicks', null=True, blank=True)
    click_conversion_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Click conversion rate')
    conv_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Conv. rate')
    converted_clicks = models.BigIntegerField(help_text='Converted clicks', null=True, blank=True)
    total_conv_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Total conv. value')
    conversions = models.BigIntegerField(help_text='Conversions', null=True, blank=True)
    cost = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost', null=True, blank=True)
    cost_converted_click = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost / converted click', null=True, blank=True)
    cost_conv = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost / conv.', null=True, blank=True)
    ctr = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='CTR')
    device = models.CharField(max_length=255, choices=DEVICE_CHOICES, help_text='Device')
    impressions = models.BigIntegerField(help_text='Impressions', null=True, blank=True)
    day = models.DateField(help_text='When this metric occurred')
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True, auto_now_add=True)
    content_impr_share = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Content Impr. share')
    content_lost_is_rank = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Content Lost IS (rank)')
    cost_est_total_conv = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost / est. total conv.', null=True, blank=True)
    est_cross_device_conv = models.BigIntegerField(help_text='Est. cross-device conv.', null=True, blank=True)
    est_total_conv_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Est. total conv. rate')
    est_total_conv_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Est. total conv. value')
    est_total_conv_value_click = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Est. total conv. value / click')
    est_total_conv_value_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Est. total conv. value / cost')
    est_total_conv = models.BigIntegerField(help_text='Est. total conv.', null=True, blank=True)
    search_exact_match_is = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Search Exact match IS')
    search_impr_share = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Search Impr. share')
    search_lost_is_rank = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Search Lost IS (rank)')
    bid_strategy_id = models.BigIntegerField(help_text='Bid Strategy ID', null=True, blank=True)
    bid_strategy_name = models.CharField(max_length=255, null=True, blank=True)
    bid_strategy_type = models.CharField(max_length=40, choices=BID_STRATEGY_TYPE_CHOICES, help_text='Bid Strategy Type', null=True, blank=True)
    content_lost_is_budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Content Lost IS (budget)')
    invalid_click_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Invalid click rate')
    invalid_clicks = models.BigIntegerField(help_text='Invalid clicks', null=True, blank=True)
    search_lost_is_budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Search Lost IS (budget)')
    value_converted_click = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Value / converted click')
    value_conv = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Value / conv.')
    view_through_conv = models.BigIntegerField(help_text='View-through conv.', null=True, blank=True)

    objects = QuerySetManager()
    
    class QuerySet(PopulatingGoogleAdwordsQuerySet):

        def populate(self, data, campaign):
            device = data.get('@device')
            day = data.get('@day')
            identifier = '%s-%s' % (device, day)
            
            while not acquire_googleadwords_lock(DailyCampaignMetrics, identifier):
                logger.debug("Waiting for acquire_googleadwords_lock: %s:%s", DailyCampaignMetrics.__name__, identifier)
                time.sleep(settings.GOOGLEADWORDS_LOCK_WAIT)
               
            try:
                logger.debug("Success acquire_googleadwords_lock: %s:%s", DailyCampaignMetrics.__name__, identifier)
                return self._populate(data,
                                  ignore_fields=['campaign'],
                                  device=device,
                                  day=day,
                                  campaign=campaign)
            
            finally:
                logger.debug("Releasing acquire_googleadwords_lock: %s:%s", DailyCampaignMetrics.__name__, identifier)
                release_googleadwords_lock(DailyCampaignMetrics, identifier)
            
class AdGroup(models.Model):
    STATE_ENABLED = 'enabled'
    STATE_PAUSED = 'paused'
    STATE_DELETED = 'deleted'
    STATE_CHOICES = (
        (STATE_ENABLED, 'Enabled'),
        (STATE_PAUSED, 'Paused'),
        (STATE_DELETED, 'Deleted')
    )
    
    campaign = models.ForeignKey('django_google_adwords.Campaign', related_name='ad_groups')
    
    ad_group_id = models.BigIntegerField(unique=True)
    ad_group = models.CharField(max_length=255, help_text='Ad group name', null=True, blank=True)
    ad_group_state = models.CharField(max_length=20, choices=STATE_CHOICES, null=True, blank=True)
    
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True, auto_now_add=True)
    
    objects = QuerySetManager()
    
    class QuerySet(PopulatingGoogleAdwordsQuerySet):

        def populate(self, data, campaign):
            """
            A locking get_or_create - note only the ad_group_id is used in the 'get'.
            """
            ad_group_id = int(data.get('@adGroupID'))

            # Get a lock based upon the ad_group_id
            while not acquire_googleadwords_lock(AdGroup, ad_group_id):
                logger.debug("Waiting for acquire_googleadwords_lock: %s:%s", AdGroup.__name__, ad_group_id)
                time.sleep(settings.GOOGLEADWORDS_LOCK_WAIT)
               
            try:
                logger.debug("Success acquire_googleadwords_lock: %s:%s", AdGroup.__name__, ad_group_id)
                return self._populate(data, ignore_fields=['campaign'], ad_group_id=ad_group_id, campaign=campaign)
            
            finally:
                logger.debug("Releasing acquire_googleadwords_lock: %s:%s", AdGroup.__name__, ad_group_id)
                release_googleadwords_lock(AdGroup, ad_group_id)
        
    @staticmethod
    def get_selector(start=None, finish=None):
        """
        Returns the selector to pass to the api to get the data.
        """
        if not start:
            start = date.today() - timedelta(days=6)
        if not finish:
            finish = date.today() - timedelta(days=1)
            
        report_definition = {
            'reportName': 'Ad Group Performance Report',
            'dateRangeType': 'CUSTOM_DATE',
            'reportType': 'ADGROUP_PERFORMANCE_REPORT',
            'downloadFormat': 'XML',
            'selector': {
                'fields': [
                            'AccountCurrencyCode',
                            'AccountDescriptiveName',
                            'AdGroupId',
                            'AdGroupName',
                            'AdGroupStatus',
                            'CampaignId',
                            'CampaignName',
                            'CampaignStatus',
                            'TargetCpa',
                            'ValuePerEstimatedTotalConversion',
                            'BiddingStrategyId',
                            'BiddingStrategyName',
                            'BiddingStrategyType',
                            'ContentImpressionShare',
                            'ContentRankLostImpressionShare',
                            'CostPerEstimatedTotalConversion',
                            'EstimatedCrossDeviceConversions',
                            'EstimatedTotalConversionRate',
                            'EstimatedTotalConversionValue',
                            'EstimatedTotalConversionValuePerClick',
                            'EstimatedTotalConversionValuePerCost',
                            'EstimatedTotalConversions',
                            'SearchExactMatchImpressionShare',
                            'SearchImpressionShare',
                            'SearchRankLostImpressionShare',
                            'ValuePerConversion',
                            'ValuePerConversionManyPerClick',
                            'ViewThroughConversions',
                            'AverageCpc',
                            'AverageCpm',
                            'AveragePosition',
                            'Clicks',
                            'ConversionRate',
                            'ConversionRateManyPerClick',
                            'ConversionValue',
                            'Conversions',
                            'ConversionsManyPerClick',
                            'Cost',
                            'CostPerConversion',
                            'CostPerConversionManyPerClick',
                            'Ctr',
                            'Device',
                            'Impressions',
                            'Date',
                           ],
                'dateRange': {
                              'min': start.strftime("%Y%m%d"),
                              'max': finish.strftime("%Y%m%d")
                              },
            },
            'includeZeroImpressions': 'true'
        }
        
        return report_definition

class DailyAdGroupMetrics(models.Model):
    BID_STRATEGY_TYPE_BUDGET_OPTIMIZER = 'auto'
    BID_STRATEGY_TYPE_CONVERSION_OPTIMIZER = 'max/target cpa'
    BID_STRATEGY_TYPE_MANUAL_CPC = 'cpc'
    BID_STRATEGY_TYPE_MANUAL_CPM = 'cpm'
    BID_STRATEGY_TYPE_PAGE_ONE_PROMOTED = 'Target search page location'
    BID_STRATEGY_TYPE_PERCENT_CPA = 'max cpa percent'
    BID_STRATEGY_TYPE_TARGET_SPEND = 'Maximize clicks'
    BID_STRATEGY_TYPE_ENHANCED_CPC = 'Enhanced CPC'
    BID_STRATEGY_TYPE_TARGET_CPA = 'Target CPA'
    BID_STRATEGY_TYPE_TARGET_ROAS = 'Target ROAS'
    BID_STRATEGY_TYPE_NONE = 'None'
    BID_STRATEGY_TYPE_UNKNOWN = 'unknown'
    BID_STRATEGY_TYPE_CHOICES = (
        (BID_STRATEGY_TYPE_BUDGET_OPTIMIZER, 'Budget Optimizer'),
        (BID_STRATEGY_TYPE_CONVERSION_OPTIMIZER, 'Conversion Optimizer'),
        (BID_STRATEGY_TYPE_MANUAL_CPC, 'Manual CPC'),
        (BID_STRATEGY_TYPE_MANUAL_CPM, 'Manual CPM'),
        (BID_STRATEGY_TYPE_PAGE_ONE_PROMOTED, 'Page One Promoted'),
        (BID_STRATEGY_TYPE_PERCENT_CPA, 'Percent CPA'),
        (BID_STRATEGY_TYPE_TARGET_SPEND, 'Target Spend'),
        (BID_STRATEGY_TYPE_ENHANCED_CPC, 'Enhanced CPC'),
        (BID_STRATEGY_TYPE_TARGET_CPA, 'Target CPA'),
        (BID_STRATEGY_TYPE_TARGET_ROAS, 'Target ROAS'),
        (BID_STRATEGY_TYPE_NONE, 'None'),
        (BID_STRATEGY_TYPE_UNKNOWN, 'Unknown')
    )
    
    DEVICE_UNKNOWN = 'Other'
    DEVICE_DESKTOP = 'Computers'
    DEVICE_HIGH_END_MOBILE = 'Mobile devices with full browsers'
    DEVICE_TABLET = 'Tablets with full browsers'
    DEVICE_CHOICES = (
        (DEVICE_UNKNOWN, DEVICE_UNKNOWN),
        (DEVICE_DESKTOP, DEVICE_DESKTOP),
        (DEVICE_HIGH_END_MOBILE, DEVICE_HIGH_END_MOBILE),
        (DEVICE_TABLET, DEVICE_TABLET)
    )
    
    ad_group = models.ForeignKey('django_google_adwords.AdGroup', related_name='metrics')
    avg_cpc = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Avg. CPC', null=True, blank=True)
    avg_cpm = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Avg. CPM', null=True, blank=True)
    avg_position = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Avg. position')
    clicks = models.IntegerField(help_text='Clicks', null=True, blank=True)
    click_conversion_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Click conversion rate')
    conv_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Conv. rate')
    converted_clicks = models.BigIntegerField(help_text='Converted clicks', null=True, blank=True)
    total_conv_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Total conv. value')
    conversions = models.BigIntegerField(help_text='Conversions', null=True, blank=True)
    cost = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost', null=True, blank=True)
    cost_converted_click = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost / converted click', null=True, blank=True)
    cost_conv = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost / conv.', null=True, blank=True)
    ctr = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='CTR')
    device = models.CharField(max_length=255, choices=DEVICE_CHOICES, help_text='Device')
    impressions = models.BigIntegerField(help_text='Impressions', null=True, blank=True)
    day = models.DateField(help_text='When this metric occurred')
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True, auto_now_add=True)
    content_impr_share = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Content Impr. share')
    content_lost_is_rank = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Content Lost IS (rank)')
    cost_est_total_conv = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost / est. total conv.', null=True, blank=True)
    est_cross_device_conv = models.BigIntegerField(help_text='Est. cross-device conv.', null=True, blank=True)
    est_total_conv_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Est. total conv. rate')
    est_total_conv_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Est. total conv. value')
    est_total_conv_value_click = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Est. total conv. value / click')
    est_total_conv_value_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Est. total conv. value / cost')
    est_total_conv = models.BigIntegerField(help_text='Est. total conv.', null=True, blank=True)
    search_exact_match_is = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Search Exact match IS')
    search_impr_share = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Search Impr. share')
    search_lost_is_rank = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Search Lost IS (rank)')
    bid_strategy_id = models.BigIntegerField(help_text='Bid Strategy ID', null=True, blank=True)
    bid_strategy_name = models.CharField(max_length=255, null=True, blank=True)
    bid_strategy_type = models.CharField(max_length=40, choices=BID_STRATEGY_TYPE_CHOICES, help_text='Bid Strategy Type', null=True, blank=True)
    max_cpa_converted_clicks = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Max. CPA (converted clicks)', null=True, blank=True)
    value_est_total_conv = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Value / est. total conv.')
    value_converted_click = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Value / converted click')
    value_conv = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Value / conv.')
    view_through_conv = models.BigIntegerField(help_text='View-through conv.', null=True, blank=True)
    
    objects = QuerySetManager()
    
    class QuerySet(PopulatingGoogleAdwordsQuerySet):

        def populate(self, data, ad_group):
            device = data.get('@device')
            day = data.get('@day')
            identifier = '%s-%s' % (device, day)
            
            while not acquire_googleadwords_lock(DailyAdGroupMetrics, identifier):
                logger.debug("Waiting for acquire_googleadwords_lock: %s:%s", DailyAdGroupMetrics.__name__, identifier)
                time.sleep(settings.GOOGLEADWORDS_LOCK_WAIT)
               
            try:
                logger.debug("Success acquire_googleadwords_lock: %s:%s", DailyAdGroupMetrics.__name__, identifier)
                return self._populate(data, ignore_fields=['ad_group'], device=device, day=day, ad_group=ad_group)
            
            finally:
                logger.debug("Releasing acquire_googleadwords_lock: %s:%s", DailyAdGroupMetrics.__name__, identifier)
                release_googleadwords_lock(DailyAdGroupMetrics, identifier)
    
class Ad(models.Model):
    STATE_ENABLED = 'enabled'
    STATE_PAUSED = 'paused'
    STATE_DELETED = 'deleted'
    STATE_CHOICES = (
        (STATE_ENABLED, 'Enabled'),
        (STATE_PAUSED, 'Paused'),
        (STATE_DELETED, 'Deleted')
    )
    
    TYPE_DEPRECATED_AD = 'Other'
    TYPE_IMAGE_AD = 'Image ad'
    TYPE_MOBILE_AD = 'Mobile ad'
    TYPE_PRODUCT_AD = 'Product listing ad'
    TYPE_TEMPLATE_AD = 'Display ad'
    TYPE_TEXT_AD = 'Text ad'
    TYPE_THIRD_PARTY_REDIRECT_AD = 'Third party ad'
    TYPE_DYNAMIC_SEARCH_AD = 'Dynamic search ad'
    TYPE_CHOICES = (
        (TYPE_DEPRECATED_AD, 'Other'),
        (TYPE_IMAGE_AD, 'Image Ad'),
        (TYPE_MOBILE_AD, 'Mobile Ad'),
        (TYPE_PRODUCT_AD, 'Product Listing Ad'),
        (TYPE_TEMPLATE_AD, 'Display Ad'),
        (TYPE_TEXT_AD, 'Text Ad'),
        (TYPE_THIRD_PARTY_REDIRECT_AD, 'Third Party Ad'),
        (TYPE_DYNAMIC_SEARCH_AD, 'Dynamic Search Ad')
    )
    
    ad_group = models.ForeignKey('django_google_adwords.AdGroup', related_name='ads')
    ad_id = models.BigIntegerField(help_text='Googles Ad ID')
    ad_state = models.CharField(max_length=20, choices=STATE_CHOICES, null=True, blank=True)
    ad_type = models.CharField(max_length=20, choices=TYPE_CHOICES, null=True, blank=True)
    destination_url = models.TextField(help_text='Destination URL', null=True, blank=True)
    display_url = models.TextField(help_text='Display URL', null=True, blank=True)
    ad = models.TextField(help_text='Ad/Headline', null=True, blank=True)
    description_line1 = models.TextField(help_text='Description line 1', null=True, blank=True)
    description_line2 = models.TextField(help_text='Description line 2', null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True, auto_now_add=True)
    
    objects = QuerySetManager()
    
    class QuerySet(PopulatingGoogleAdwordsQuerySet):

        def populate(self, data, ad_group):
            """
            A locking get_or_create - note only the ad_id is used in the 'get'.
            """
            ad_id = int(data.get('@adID'))

            # Get a lock based upon the campaign id
            while not acquire_googleadwords_lock(Ad, ad_id):
                logger.debug("Waiting for acquire_googleadwords_lock: %s:%s", Ad.__name__, ad_id)
                time.sleep(settings.GOOGLEADWORDS_LOCK_WAIT)
               
            try:
                logger.debug("Success acquire_googleadwords_lock: %s:%s", Ad.__name__, ad_id)
                return self._populate(data, ignore_fields=['ad_group'], ad_id=ad_id, ad_group=ad_group)
            
            finally:
                logger.debug("Releasing acquire_googleadwords_lock: %s:%s", Ad.__name__, ad_id)
                release_googleadwords_lock(Ad, ad_id)
    
    @staticmethod
    def get_selector(start=None, finish=None):
        """
        Returns the selector to pass to the api to get the data.
        """
        if not start:
            start = date.today() - timedelta(days=6)
        if not finish:
            finish = date.today() - timedelta(days=1)
            
        report_definition = {
            'reportName': 'Ad Performance Report',
            'dateRangeType': 'CUSTOM_DATE',
            'reportType': 'AD_PERFORMANCE_REPORT',
            'downloadFormat': 'XML',
            'selector': {
                'fields': [
                            'AccountCurrencyCode',
                            'AccountDescriptiveName',
                            'AdGroupId',
                            'AdGroupName',
                            'AdGroupStatus',
                            'AdType',
                            'AverageCpc',
                            'AverageCpm',
                            'AveragePosition',
                            'CampaignId',
                            'CampaignName',
                            'CampaignStatus',
                            'Clicks',
                            'ConversionRate',
                            'ConversionRateManyPerClick',
                            'ConversionValue',
                            'Conversions',
                            'ConversionsManyPerClick',
                            'Cost',
                            'CostPerConversion',
                            'CostPerConversionManyPerClick',
                            'CreativeDestinationUrl',
                            'Ctr',
                            'Description1',
                            'Description2',
                            'Device',
                            'DisplayUrl',
                            'Headline',
                            'Id',
                            'Impressions',
                            'Status',
                            'ValuePerConversion',
                            'ValuePerConversionManyPerClick',
                            'ViewThroughConversions',
                            'Date',
                           ],
                'dateRange': {
                              'min': start.strftime("%Y%m%d"),
                              'max': finish.strftime("%Y%m%d")
                              },
            },
            'includeZeroImpressions': 'true'
        }
    
        return report_definition
    
class DailyAdMetrics(models.Model):
    DEVICE_UNKNOWN = 'Other'
    DEVICE_DESKTOP = 'Computers'
    DEVICE_HIGH_END_MOBILE = 'Mobile devices with full browsers'
    DEVICE_TABLET = 'Tablets with full browsers'
    DEVICE_CHOICES = (
        (DEVICE_UNKNOWN, DEVICE_UNKNOWN),
        (DEVICE_DESKTOP, DEVICE_DESKTOP),
        (DEVICE_HIGH_END_MOBILE, DEVICE_HIGH_END_MOBILE),
        (DEVICE_TABLET, DEVICE_TABLET)
    )
    
    ad = models.ForeignKey('django_google_adwords.Ad', related_name='metrics')
    avg_cpc = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Avg. CPC', null=True, blank=True)
    avg_cpm = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Avg. CPM', null=True, blank=True)
    avg_position = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Avg. position')
    clicks = models.IntegerField(help_text='Clicks', null=True, blank=True)
    click_conversion_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Click conversion rate')
    conv_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Conv. rate')
    converted_clicks = models.BigIntegerField(help_text='Converted clicks', null=True, blank=True)
    total_conv_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Total conv. value')
    conversions = models.BigIntegerField(help_text='Conversions', null=True, blank=True)
    cost = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost', null=True, blank=True)
    cost_converted_click = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost / converted click', null=True, blank=True)
    cost_conv = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency='AUD', help_text='Cost / conv.', null=True, blank=True)
    ctr = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='CTR')
    device = models.CharField(max_length=255, choices=DEVICE_CHOICES, help_text='Device')
    impressions = models.BigIntegerField(help_text='Impressions', null=True, blank=True)
    day = models.DateField(help_text='When this metric occurred')
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True, auto_now_add=True)
    value_converted_click = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Value / converted click')
    value_conv = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='Value / conv.')
    view_through_conv = models.BigIntegerField(help_text='View-through conv.', null=True, blank=True)
    
    objects = QuerySetManager()
    
    class QuerySet(PopulatingGoogleAdwordsQuerySet):

        def populate(self, data, ad):
            device = data.get('@device')
            day = data.get('@day')
            identifier = '%s-%s' % (device, day)
            
            while not acquire_googleadwords_lock(DailyAdMetrics, identifier):
                logger.debug("Waiting for acquire_googleadwords_lock: %s:%s", DailyAdMetrics.__name__, identifier)
                time.sleep(settings.GOOGLEADWORDS_LOCK_WAIT)
               
            try:
                logger.debug("Success acquire_googleadwords_lock: %s:%s", DailyAdMetrics.__name__, identifier)
                return self._populate(data, ignore_fields=['ad'], device=device, day=day, ad=ad)
            
            finally:
                logger.debug("Releasing acquire_googleadwords_lock: %s:%s", DailyAdMetrics.__name__, identifier)
                release_googleadwords_lock(DailyAdMetrics, identifier)

def reportfile_file_upload_to(instance, filename):
    filename = "%s%s" % (instance.pk, os.path.splitext(filename)[1])
    return os.path.join(settings.GOOGLEADWORDS_REPORT_FILE_ROOT,
                        instance.created.strftime("%Y"),
                        instance.created.strftime("%m"),
                        instance.created.strftime("%d"),
                        filename)

class ReportFile(models.Model):
    file = models.FileField(max_length=255, upload_to=reportfile_file_upload_to, null=True, blank=True)
    processed = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)
    
    objects = QuerySetManager()
    
    class QuerySet(_QuerySet):
        def request(self, report_definition, client_customer_id):
            """
            Fields and report types can be found here https://developers.google.com/adwords/api/docs/appendix/reports
             
            client_customer_id='591-877-6172'
            
            report_definition = {
                'reportName': 'Account Performance Report',
                'dateRangeType': 'CUSTOM_DATE',
                'reportType': 'ACCOUNT_PERFORMANCE_REPORT',
                'downloadFormat': 'XML',
                'selector': {
                    'fields': [
                               'AverageCpc',
                               'Clicks',
                               'Impressions',
                               'Cost',
                               'Conversions',
                               'Date'
                               ],
                    'dateRange': {
                        'min': '20140501',
                        'max': '20140601'
                    },
                },
                # Enable to get rows with zero impressions.
                'includeZeroImpressions': 'false'
            }
            
            Example usage of return data
            
            for metric, value in r['report']['table']['row'].iteritems():
                print metric, value
            
            @param report_definition: A dict of values used to specify a report to get from the API.
            @param client_customer_id: A string containing the Adwords Customer Client ID.
            @return OrderedDict containing report
            """
            client = adwords_service(client_customer_id)
            report_downloader = client.GetReportDownloader(version=settings.GOOGLEADWORDS_CLIENT_VERSION)
            
            try:
                report_file = ReportFile.objects.create()
                with report_file.file_manager('%s.xml' % report_file.pk) as f:
                    report_downloader.DownloadReport(report_definition, output=f)
                return report_file
            except GoogleAdsError as e:
                report_file.delete() # cleanup
                if not hasattr(e, 'fault') or not hasattr(e.fault, 'detail') or not hasattr(e.fault.detail, 'ApiExceptionFault') or not hasattr(e.fault.detail.ApiExceptionFault, 'errors'):
                    # If they aren't telling us to retryAfterSeconds - raise
                    raise
                retryAfterSeconds = sum([int(fault.retryAfterSeconds) for fault in e.fault.detail.ApiExceptionFault.errors if getattr(fault, 'ApiError.Type') == 'RateExceededError'])
                if retryAfterSeconds > 0:
                    # We've hit a RateExceededError - raise
                    raise RateExceededError(retryAfterSeconds)
                else:
                    # We haven't hit an error we care about, raise it.
                    raise

    @contextmanager
    def file_manager(self, filename):
        """
        Yields a temporary file like object which is then saved. 
        
        This can be used to safely write to the file attribute and ensure that
        upon an error the file is removed (ie.. there is cleanup).
        """
        with tempfile(delete=True) as f:
            yield f
            self.file.save(filename, File(f))

    def save_path(self, path):
        """
        Save a path to the file attribute.
        """
        self.file.save(os.path.basename(path), File(open(path)))

    def save_file(self, f):
        """
        Save a file like object to the file attribute.
        """
        self.file.save(os.path.basename(f.name), File(f))

    def dehydrate(self):
        """
        Convert the underlying report file into python (most likely, an OrderedDict).
        """
        return xmltodict.parse(self.file.read())

def receiver_delete_reportfile(sender, instance, **kwargs):
    if instance.file:
        instance.file.delete(save=False)
post_delete.connect(receiver_delete_reportfile, ReportFile)