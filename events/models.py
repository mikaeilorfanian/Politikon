# -*- coding: utf-8 -*-
import json
import logging

from dateutil.relativedelta import relativedelta
from math import exp
from unidecode import unidecode

from django.conf import settings
from django.core.urlresolvers import reverse
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.template.defaultfilters import slugify
from django.utils import timezone
from django.utils.translation import ugettext as _

from .exceptions import UnknownOutcome, EventNotInProgress
from .managers import EventManager, BetManager, TransactionManager

from bladepolska.snapshots import SnapshotAddon
from bladepolska.site import current_domain
from django_elasticsearch.models import EsIndexable
from constance import config
from imagekit.models import ProcessedImageField
from imagekit.processors import ResizeToFill
from taggit_autosuggest.managers import TaggableManager

logger = logging.getLogger(__name__)


class EventCategory(models.Model):
    name = models.CharField(u'tytuł wydarzenia', max_length=255, unique=True)
    slug = models.SlugField(verbose_name=_('Slug url'), unique=True)

    class Meta:
        verbose_name = u'kategoria'
        verbose_name_plural = u'kategorie'

    def __unicode__(self):
        return self.name


class Event(EsIndexable, models.Model):
    """
    Event model represents exactly real question which you can answer YES or NO.
    """
    IN_PROGRESS, CANCELLED, FINISHED_YES, FINISHED_NO = range(1, 5)
    EVENT_OUTCOME_CHOICES = (
        (IN_PROGRESS, u'w trakcie'),
        (CANCELLED, u'anulowane'),
        (FINISHED_YES, u'rozstrzygnięte na TAK'),
        (FINISHED_NO, u'rozstrzygnięte na NIE'),
    )
    EVENT_FINISHED_TYPES = (CANCELLED, FINISHED_YES, FINISHED_NO)

    BOOLEAN_OUTCOME_DICT = {
        FINISHED_YES: True,
        FINISHED_NO: False
    }

    BEGIN_PRICE = 50
    FACTOR_B = 10
    PRIZE_FOR_WINNING = 100

    CHART_MARGIN = 3
    EVENT_SMALL_CHART_DAYS = 14
    EVENT_BIG_CHART_DAYS = 28

    SMALL_IMAGE_WIDTH = 340
    SMALL_IMAGE_HEIGHT = 250
    BIG_IMAGE_WIDTH = 1250
    BIG_IMAGE_HEIGHT = 510

    snapshots = SnapshotAddon(fields=[
        'current_buy_for_price',
        'current_buy_against_price',
        'current_sell_for_price',
        'current_sell_against_price',
        'Q_for',
        'Q_against',
        'B'
    ])

    title = models.CharField(u'tytuł wydarzenia', max_length=255)
    short_title = models.CharField(
        verbose_name=u'tytuł promocyjny wydarzenia', max_length=255, default='', blank=True
    )
    description = models.TextField(u'pełny opis wydarzenia', default='')
    categories = models.ManyToManyField('events.EventCategory', verbose_name=u'kategorie', blank=True)
    is_featured = models.BooleanField(u'wyróżniony', default=False)
    is_published = models.BooleanField(u'opublikowano', default=True)

    twitter_tag = models.CharField(
        verbose_name=u'tag twittera', max_length=32, null=True, blank=True, default='',
        validators=[
            RegexValidator(
                regex=r'^([^\s]+)$',
                message=u'Tag twittera nie może zawierać spacji',
                code='invalid_twitter_tag'
            ),
        ]
    )

    title_fb_yes = models.CharField(
        u'tytuł na TAK obiektu FB', max_length=255, default='', blank=True, null=True
    )
    title_fb_no = models.CharField(
        u'tytuł na NIE obiektu FB', max_length=255, default='', blank=True, null=True
    )

    small_image = ProcessedImageField(
        help_text=u'mały obrazek {0}x{1}'.format(SMALL_IMAGE_WIDTH, SMALL_IMAGE_HEIGHT),
        upload_to='events_small',
        processors=[ResizeToFill(SMALL_IMAGE_WIDTH, SMALL_IMAGE_HEIGHT)],
        null=True,
        blank=False,
    )
    big_image = ProcessedImageField(
        help_text=u'duży obrazek {0}x{1}'.format(BIG_IMAGE_WIDTH, BIG_IMAGE_HEIGHT),
        upload_to='events_big',
        processors=[ResizeToFill(BIG_IMAGE_WIDTH, BIG_IMAGE_HEIGHT)],
        null=True,
        blank=False,
    )

    # głosowanie do rozstrzygania wydarzeń
    vote_yes_count = models.PositiveIntegerField(u'głosów na tak', default=0)
    vote_no_count = models.PositiveIntegerField(u'głosów na nie', default=0)
    vote_cancel_count = models.PositiveIntegerField(u'głosów na anuluj', default=0)

    outcome = models.PositiveIntegerField(u'rozstrzygnięcie', choices=EVENT_OUTCOME_CHOICES, default=1)
    outcome_reason = models.TextField(u'uzasadnienie wyniku', default='', blank=True)

    created_date = models.DateTimeField(auto_now_add=True, verbose_name=u'data utworzenia')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=u'utworzone przez', null=True, related_name='created_by'
    )
    estimated_end_date = models.DateTimeField(u'przewidywana data rozstrzygnięcia', null=True, blank=False)
    end_date = models.DateTimeField(u'data rozstrzygnięcia', null=True, blank=True)

    current_buy_for_price = models.IntegerField(
        u'cena nabycia akcji zdarzenia', default=BEGIN_PRICE
    )
    current_buy_against_price = models.IntegerField(
        u'cena nabycia akcji zdarzenia przeciwnego', default=BEGIN_PRICE
    )
    current_sell_for_price = models.IntegerField(
        u'cena sprzedaży akcji zdarzenia', default=BEGIN_PRICE
    )
    current_sell_against_price = models.IntegerField(
        u'cena sprzedaży akcji zdarzenia przeciwnego', default=BEGIN_PRICE
    )

    last_transaction_date = models.DateTimeField(u'data ostatniej transakcji', null=True)

    Q_for = models.IntegerField(u'zakładów na TAK', default=0)
    Q_against = models.IntegerField(u'zakładów na NIE', default=0)
    turnover = models.IntegerField(u'obrót', default=0, db_index=True)

    absolute_price_change = models.IntegerField(
        u'zmiana ceny (wartość absolutna)', db_index=True, default=0
    )
    price_change = models.IntegerField(u'zmiana ceny', default=0)

    # constant for calculating event change
    # probably: how you need to increment quantity, to change price
    B = models.FloatField(u'stała B', default=FACTOR_B)

    objects = EventManager()
    tags = TaggableManager(blank=True)

    class Meta:
        verbose_name = 'wydarzenie'
        verbose_name_plural = 'wydarzenia'

    def __unicode__(self):
        return self.title

    def save(self, *args, **kwargs):
        """
        Recalculate prices for event
        :param kwargs:
        """
        if not self.pk:
            self.recalculate_prices()

        super(Event, self).save(*args, **kwargs)

    def get_absolute_url(self):
        return 'http://%(domain)s%(url)s' % {
            'domain': current_domain(),
            'url': reverse('events:event_detail', kwargs={'pk': self.pk})
        }

    def get_relative_url(self):
        return '/event/%(id)d-%(title)s' % {'id': self.id, 'title': slugify(unidecode(self.title))}

    def get_absolute_facebook_object_url(self):
        return 'http://%(domain)s%(url)s' % {
            'domain': current_domain(),
            'url': reverse('events:event_facebook_object_detail', kwargs={'event_id': self.id})
        }

    def get_small_embed_url(self):
        return 'http://%(domain)s%(url)s' % {
            'domain': current_domain(),
            'url': reverse('events:event_embed_detail', kwargs={'pk': self.id})
        }

    @staticmethod
    def autocomplete_search_fields():
        return ("id__iexact", "title__icontains", "short_title__icontains")

    @property
    def is_in_progress(self):
        return self.outcome == Event.IN_PROGRESS

    @property
    def publish_channel(self):
        return 'event_%d' % self.id

    @property
    def event_dict(self):
        return {
            'event_id': self.id,
            'buy_for_price': self.current_buy_for_price,
            'buy_against_price': self.current_buy_against_price,
            'sell_for_price': self.current_sell_for_price,
            'sell_against_price': self.current_sell_against_price,
        }

    @property
    def finish_date(self):
        """
        If event is not finished then estimated_end_date, else end_date
        :return: finish date
        :rtype: datetime
        """
        if self.is_in_progress:
            return self.estimated_end_date
        else:
            return self.end_date

    def price_for_outcome(self, outcome, direction=True):
        if (direction, outcome) not in Bet.BET_OUTCOMES_TO_PRICE_ATTR:
            raise UnknownOutcome()

        attr = Bet.BET_OUTCOMES_TO_PRICE_ATTR[(direction, outcome)]
        return getattr(self, attr)

    def get_event_small_chart(self):
        """
        Get last transactions price for every day from small event range
        :return: chart points of EVENT_SMALL_CHART_DAYS days
        :rtype: {int, [], []}
        """
        return self.__get_chart_points(self.EVENT_SMALL_CHART_DAYS)

    def get_event_big_chart(self):
        """
        Get last transactions price for every day from big event range
        :return: chart points of EVENT_BIG_CHART_DAYS days
        :rtype: {int, [], []}
        """
        return self.__get_chart_points(self.EVENT_BIG_CHART_DAYS)

    def get_JSON_small_chart(self):
        return json.dumps(self.get_event_small_chart())

    def get_JSON_big_chart(self):
        return json.dumps(self.get_event_big_chart())

    @transaction.atomic
    def __get_chart_points(self, days):
        """
        Get last transactions price for every day;
        :param days: number of days in past on chart
        :type days: int
        :return: chart points
        :rtype: {int, [], []}
        """
        last_date = self.end_date if self.end_date else timezone.now()
        first_date = max(last_date - relativedelta(days=days), self.created_date)
        labels = []
        points = []

        snapshots = self.snapshots.filter(
            snapshot_of_id=self.id,
            created_at__gte=first_date,
            created_at__lte=last_date,
            created_at__hour=0
        ).order_by('created_at')

        additional_points = min(days - len(snapshots), Event.CHART_MARGIN)
        step_date = first_date - relativedelta(days=additional_points)

        for point in range(additional_points):
            labels.append(u'{0} {1}'.format(step_date.day, _(step_date.strftime('%B'))))
            step_date += relativedelta(days=1)
            points.append(Event.BEGIN_PRICE)

        for snapshot in snapshots:
            labels.append(u'{0} {1}'.format(snapshot.created_at.day, _(snapshot.created_at.strftime('%B'))))
            last_price = snapshot.current_buy_for_price
            points.append(last_price)

        return {
            'id': self.id,
            'labels': labels,
            'points': points
        }

    def get_user_bet_object(self, user):
        """
        find not empty just only one bet object or None
        :param user: logged user
        :type user: User
        :return: normally it should returns one bet where bet.has > 0
        :rtype: Bet or None
        """
        bets = self.bets.filter(user=user, has__gt=0).order_by('-id')
        if bets.exists():
            return bets[0]

    def get_user_bet(self, user):
        """
        get bet summary for user; user maybe anonymous.
        :param user: logged user or anonymous
        :type user: User
        :return: data for one bet display
        :rtype: {}
        """
        # Using 'true' and 'false' because some keys are designed for json
        bet_line = {
            'is_user': False,
            'has': 0,
            'avgPrice': 0,
            'outcome': None,  # note: None is the same as False
            'buyNO': 'true',  # default option is buy bet
            'buyYES': 'true',  # default option is buy bet
            'priceYES': self.current_buy_for_price,
            'priceNO': self.current_buy_against_price,
        }
        if user.pk:
            bet_line['is_user'] = True
            bet = self.get_user_bet_object(user)
            if bet:
                bet_line['id'] = bet.pk     # it is only for debugging purpose
                bet_line['has'] = bet.has
                bet_line['avgPrice'] = bet.bought_avg_price
                bet_line['outcome'] = bet.outcome       # True - YES          False - NO
                if bet.outcome:
                    # you have bet for YES, you can sell them
                    bet_line['buyNO'] = 'false'     # that means you sell bet YES
                    bet_line['priceYES'] = self.current_buy_for_price
                    bet_line['priceNO'] = self.current_sell_for_price
                    bet_line['outcome_str'] = 'true'
                else:
                    # you have bet for NO, you can sell them
                    bet_line['buyYES'] = 'false'    # that means you sell bet NO
                    bet_line['priceYES'] = self.current_sell_against_price
                    bet_line['priceNO'] = self.current_buy_against_price
                    bet_line['outcome_str'] = 'false'

        return bet_line

    def get_bet_social(self):
        """
        Get users who bought this event
        :return: Dict with 4 keys: 2 QuerySet with YES users and NO users, 2
        integers with counts
        :rtype: dict{}
        """
        response = {}
        bet_social_yes = Bet.objects.filter(
            event=self,
            outcome=True,  # bought YES
            has__gt=0,
        )
        response['yes_count'] = bet_social_yes.count()
        response['yes_bets'] = bet_social_yes[:6]

        bet_social_no = Bet.objects.filter(
            event=self,
            outcome=False,  # bought NO
            has__gt=0,
        )
        response['no_count'] = bet_social_no.count()
        response['no_bets'] = bet_social_no[:6]
        return response

    def increment_quantity(self, outcome, by_amount):
        """
        Used when operation buy or sell occurs
        :param outcome: event outcome - YES or NO; True for YES
        :type outcome: bool
        :param by_amount: operations count, usually 1
        :type by_amount: int
        :return:
        """
        if outcome not in Bet.BET_OUTCOMES_TO_QUANTITY_ATTR:
            raise UnknownOutcome()

        attr = Bet.BET_OUTCOMES_TO_QUANTITY_ATTR[outcome]
        setattr(self, attr, getattr(self, attr) + by_amount)

        self.recalculate_prices()

    def increment_turnover(self, by_amount):
        """
        Turnover increases +1 when operation buy or sell occurs
        :param by_amount: operations count, usually 1
        :type by_amount: int
        """
        self.turnover += by_amount

    def recalculate_prices(self):
        """
        Calculate 4 prices for event
        """
        factor = 100.

        B = self.B

        Q_for = self.Q_for
        Q_against = self.Q_against
        Q_for_sell = max(0, Q_for - 1)
        Q_against_sell = max(0, Q_against - 1)

        e_for_buy = exp(Q_for / B)
        e_against_buy = exp(Q_against / B)
        e_for_sell = exp(Q_for_sell / B)
        e_against_sell = exp(Q_against_sell / B)

        buy_for_price = e_for_buy / float(e_for_buy + e_against_buy)
        buy_against_price = e_against_buy / float(e_for_buy + e_against_buy)
        sell_for_price = e_for_sell / float(e_for_sell + e_against_buy)
        sell_against_price = e_against_sell / float(e_for_buy + e_against_sell)

        self.current_buy_for_price = round(factor * buy_for_price, 0)
        self.current_buy_against_price = round(factor * buy_against_price, 0)
        self.current_sell_for_price = round(factor * sell_for_price, 0)
        self.current_sell_against_price = round(factor * sell_against_price, 0)

    def vote_yes(self):
        self.vote_yes_count += 1
        if self.vote_yes_count >= config.VOICES_TO_RESOLVE:
            self.finish_yes()
        self.save()
        return self.vote_yes_count

    def vote_no(self):
        self.vote_no_count += 1
        if self.vote_no_count >= config.VOICES_TO_RESOLVE:
            self.finish_no()
        self.save()
        return self.vote_no_count

    def vote_cancel(self):
        self.vote_cancel_count += 1
        if self.vote_cancel_count >= config.VOICES_TO_RESOLVE:
            self.cancel()
        self.save()
        return self.vote_cancel_count

    @transaction.atomic
    def __finish(self, outcome):
        """
        Set Event finish status
        :param outcome: outcome status; EVENT_OUTCOME_CHOICES
        :type outcome: Choices
        """
        if self.outcome != self.IN_PROGRESS:
            raise EventNotInProgress("Wydarzenie zostało już rozwiązane.")
        self.outcome = outcome
        self.end_date = timezone.now()
        self.save()

    @transaction.atomic
    def __finish_with_outcome(self, outcome):
        """
        main finish status
        :param outcome: outcome status; EVENT_OUTCOME_CHOICES
        :type outcome: Choices
        """
        self.__finish(outcome)
        for bet in Bet.objects.filter(event=self):
            if bet.outcome == self.BOOLEAN_OUTCOME_DICT[outcome]:
                bet.rewarded_total = self.PRIZE_FOR_WINNING * bet.has
                bet.user.total_cash += bet.rewarded_total
                Transaction.objects.create(
                    user=bet.user,
                    event=self,
                    type=Transaction.EVENT_WON_PRIZE,
                    quantity=bet.has,
                    price=self.PRIZE_FOR_WINNING
                )
            # TODO: tutaj wallet change
            # bet.user.portfolio_value -= bet.has
            bet.user.save()
            # This cause display event in "latest outcome"
            bet.is_new_resolved = True
            bet.save()

    @transaction.atomic
    def finish_yes(self):
        """
        if event is finished on YES then prizes calculate
        """
        self.__finish_with_outcome(self.FINISHED_YES)

    @transaction.atomic
    def finish_no(self):
        """
        if event is finished on NO then prizes calculate
        """
        self.__finish_with_outcome(self.FINISHED_NO)

    @transaction.atomic
    def cancel(self):
        """
        refund for users on cancel event.
        """
        self.__finish(self.CANCELLED)
        users = {}
        for t in Transaction.objects.filter(event=self).order_by('user'):
            if t.user not in users:
                users.update({
                    t.user: 0
                })
            if t.type in Transaction.BUY_SELL_TYPES:
                # for transaction type BUY the price is below 0 that means refund should be
                # other side. For BUY (buy is always -) refund should be (+) (EVENT_CANCELLED_REFUND)
                # but for BUY and SELL with profit refund should be (-) (EVENT_CANCELLED_DEBIT)
                users[t.user] -= t.quantity * t.price

        for user, refund in users.iteritems():
            if refund == 0:
                continue
            user.total_cash += refund
            user.save()
            if refund > 0:
                transaction_type = Transaction.EVENT_CANCELLED_REFUND
            else:
                transaction_type = Transaction.EVENT_CANCELLED_DEBIT
            Transaction.objects.create(
                user=user,
                event=self,
                type=transaction_type,
                price=refund
            )


class SolutionVote(models.Model):
    """
    Vote for event resolve
    """
    class Meta:
        unique_together = ('user', 'event')

    YES, NO, CANCEL = range(1, 4)
    VOTE_OUTCOME_CHOICES = (
        (YES, u'rozwiązanie na TAK'),
        (NO, u'rozwiązanie na NIE'),
        (CANCEL, u'anulowanie wydarzenia')
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    event = models.ForeignKey(Event)
    outcome = models.IntegerField(u'rozwiązanie wydarzenia', choices=VOTE_OUTCOME_CHOICES, null=True)


class Bet(models.Model):
    """
    Created when user choose YES or NO for event.
    """
    class Meta:
        verbose_name = 'zakład'
        verbose_name_plural = 'zakłady'

    YES = True
    NO = False
    BET_OUTCOME_CHOICES = (
        (YES, u'udziały na TAK'),
        (NO, u'udziały na NIE'),
    )

    BUY = True
    SELL = False
    BET_OUTCOMES_TO_PRICE_ATTR = {
        (BUY, YES): 'current_buy_for_price',
        (BUY, NO): 'current_buy_against_price',
        (SELL, YES): 'current_sell_for_price',
        (SELL, NO): 'current_sell_against_price'
    }

    BET_OUTCOMES_TO_QUANTITY_ATTR = {
        True: 'Q_for',
        False: 'Q_against'
    }

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=False, related_name='bets', related_query_name='bet'
    )
    event = models.ForeignKey(Event, null=False, related_name='bets', related_query_name='bet')
    outcome = models.BooleanField(u'zakład na TAK', choices=BET_OUTCOME_CHOICES)
    # most important param: how many bets user has.
    has = models.PositiveIntegerField(u'posiadane zakłady', default=0, null=False)
    bought = models.PositiveIntegerField(u'kupione zakłady', default=0, null=False)
    sold = models.PositiveIntegerField(u'sprzedane zakłady', default=0, null=False)
    bought_avg_price = models.FloatField(u'kupione po średniej cenie', default=0, null=False)
    sold_avg_price = models.FloatField(u'sprzedane po średniej cenie', default=0, null=False)
    # this field is probably for the biggest rewards
    rewarded_total = models.IntegerField(u'nagroda za wynik', default=0, null=False)
    # this is used to show event in my wallet.
    is_new_resolved = models.BooleanField(u'ostatnio rozstrzygnięte', default=False, null=False)

    objects = BetManager()

    @property
    def bet_dict(self):
        """
        Dictionary with bet values
        :return: bet vaules
        :rtype: {}
        """
        return {
            'bet_id': self.id,
            'event_id': self.event.id,
            'user_id': self.user.id,
            'outcome': self.outcome,
            'has': self.has,
            'bought': self.bought,
            'sold': self.sold,
            'bought_avg_price': self.bought_avg_price,
            'sold_avg_price': self.sold_avg_price,
            'rewarded_total': self.rewarded_total,
        }

    def __unicode__(self):
        return u'zakłady %s na %s' % (self.user, self.event)

    def current_event_price(self):
        """
        Get current price for event. Price depend on bet.outcome
        :return: current price
        :rtype: int
        """
        if self.outcome:
            return self.event.current_buy_for_price
        else:
            return self.event.current_buy_against_price

    def is_won(self):
        """
        winning bet when bet has outcome True  and event.outcome is 3
        (FINISHED_YES) or
        when bet has outcome False and event.outcome is 4 (FINISHED_NO)
        :return: True if won
        :rtype: bool
        """
        if self.outcome and self.event.outcome == Event.FINISHED_YES:
            return True
        elif not self.outcome and self.event.outcome == Event.FINISHED_NO:
            return True
        return False

    def get_wallet_change(self):
        """
        Get amount won or lose after event finished. For events in progress
        get amount possible to win.
        :return: more or less than zero
        :rtype: int
        """
        # TODO: NAPRAWDE NIE WIEM
        if self.is_won() or self.event.outcome == Event.IN_PROGRESS:
            return self.get_won() - self.get_invested()
        else:
            return -self.get_invested()

    def get_invested(self):
        """
        How many invested in this bet
        :return: price above zero
        :rtype: float
        """
        # TODO: NO NIE WIEM
        if self.event.outcome == Event.CANCELLED:
            return 0
        return round(self.has * self.bought_avg_price, 0)

    def get_won(self):
        """
        Get amount won or possibility to win.
        :return: price
        :rtype: int
        """
        if self.is_won() or self.event.outcome == Event.IN_PROGRESS:
            return self.has * Event.PRIZE_FOR_WINNING
        else:
            return 0

    def is_finished_yes(self):
        """
        Result for bet
        :return: True if event resolved for YES
        :rtype: bool
        """
        return self.event.outcome == Event.FINISHED_YES

    def is_finished_no(self):
        """
        Result for bet
        :return: True if event resolved for NO
        :rtype: bool
        """
        return self.event.outcome == Event.FINISHED_NO

    def is_cancelled(self):
        """
        Result for bet
        :return: True if canceled bet
        :rtype: bool
        """
        return self.event.outcome == Event.CANCELLED


class Transaction(models.Model):
    """
    Operation buy or sell or other for user and event
    """
    class Meta:
        ordering = ['-date']
        verbose_name = 'transakcja'
        verbose_name_plural = 'transakcje'

    BUY_YES, SELL_YES, BUY_NO, SELL_NO, \
    EVENT_CANCELLED_REFUND, EVENT_CANCELLED_DEBIT, \
    EVENT_WON_PRIZE, TOPPED_UP, BONUS = range(1, 10)
    TRANSACTION_TYPE_CHOICES = (
        (BUY_YES, u'zakup udziałów na TAK'),
        (SELL_YES, u'sprzedaż udziałów na TAK'),
        (BUY_NO, u'zakup udziałów na NIE'),
        (SELL_NO, u'sprzedaż udziałów na NIE'),
        (EVENT_CANCELLED_REFUND, u'zwrot po anulowaniu wydarzenia'),
        (EVENT_CANCELLED_DEBIT, u'obciążenie konta po anulowaniu wydarzenia'),
        (EVENT_WON_PRIZE, u'wygrana po rozstrzygnięciu wydarzenia'),
        (TOPPED_UP, u'doładowanie konta przez aplikację'),
        (BONUS, u'bonus')
    )

    # Transactions changing event price: BUY_YES, SELL_YES, BUY_NO, SELL_NO
    BUY_SELL_TYPES = (BUY_YES, SELL_YES, BUY_NO, SELL_NO)
    EVENT_SOLVED_TYPES = (EVENT_CANCELLED_REFUND, EVENT_CANCELLED_DEBIT, EVENT_WON_PRIZE)
    BONUS_TYPES = (TOPPED_UP, BONUS)

    YES_OUTCOME = (BUY_YES, SELL_YES)
    NO_OUTCOME = (BUY_NO, SELL_NO)

    BUY_TYPES = (BUY_YES, BUY_NO)
    SELL_TYPES = (SELL_YES, SELL_NO)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=False, related_name='transactions',
        related_query_name='transaction'
    )
    event = models.ForeignKey(
        Event, null=True, related_name='transactions', related_query_name='transaction'
    )
    type = models.PositiveIntegerField(
        "rodzaj transakcji", choices=TRANSACTION_TYPE_CHOICES, default=1
    )
    date = models.DateTimeField('data', auto_now_add=True)
    quantity = models.PositiveIntegerField(u'ilość', default=1)
    price = models.IntegerField(u'cena jednostkowa', default=0, null=False)

    objects = TransactionManager()

    def __unicode__(self):
        return u'%s przez %s' % (self.get_type_display(), self.user)

    @property
    def total_cash(self):
        """
        Get total price for all quantity in transaction: total won, total bought, total sold
        :return: total amount
        :rtype: int
        """
        return self.quantity * self.price

    @property
    def total_wallet(self):
        """
        Get total price for all quantity in transaction: total won, total bought, total sold
        :return: total amount
        :rtype: int
        """
        return -1 * self.quantity * self.price
