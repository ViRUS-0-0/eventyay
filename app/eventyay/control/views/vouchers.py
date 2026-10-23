import datetime
import decimal
import io
from urllib.parse import quote_plus

from defusedcsv import csv
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Case, Count, Min, Q, Subquery, Sum, When
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.urls import resolve, reverse
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.views.generic import (
    CreateView,
    DeleteView,
    ListView,
    UpdateView,
    View,
)

from eventyay.api.webhooks import notify_webhooks
from eventyay.base.models import CartPosition, LogEntry, Voucher
from eventyay.base.models.vouchers import _generate_random_code
from eventyay.base.services.locking import NoLockManager
from eventyay.base.services.notifications import notify
from eventyay.base.services.vouchers import vouchers_send
from eventyay.base.views.tasks import AsyncFormView
from eventyay.control.forms.filter import VoucherFilterForm, advanced_filter_count, advanced_filters_open_from_get
from eventyay.control.forms.vouchers import VoucherBulkEditForm, VoucherBulkForm, VoucherForm
from eventyay.control.permissions import EventPermissionRequiredMixin
from eventyay.control.signals import voucher_form_class
from eventyay.control.views import PaginationMixin
from eventyay.helpers.models import modelcopy
from eventyay.multidomain.urlreverse import build_absolute_uri


class VoucherList(PaginationMixin, EventPermissionRequiredMixin, ListView):
    model = Voucher
    context_object_name = 'vouchers'
    template_name = 'pretixcontrol/vouchers/index.html'
    permission = 'can_view_vouchers'

    def get_filtered_queryset(self):
        qs = self.request.event.vouchers.filter(waitinglistentries__isnull=True).select_related(
            'product', 'variation', 'seat', 'quota', 'subevent'
        ).prefetch_related('limit_products', 'limit_variations', 'limit_variations__product')
        if self.filter_form and self.filter_form.is_valid():
            qs = self.filter_form.filter_qs(qs)

        return qs.distinct()

    def get_queryset(self):
        qs = self.get_filtered_queryset()
        group_representatives = (
            qs.exclude(tag='')
            .order_by()
            .values('tag')
            .annotate(first_id=Min('pk'))
            .values('first_id')
        )
        qs = qs.filter(Q(tag='') | Q(pk__in=Subquery(group_representatives)))
        return Voucher.annotate_budget_used_orders(qs)

    @cached_property
    def filter_form(self):
        return VoucherFilterForm(data=self.request.GET, event=self.request.event)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['filter_form'] = self.filter_form
        ctx['advanced_filters_open'] = advanced_filters_open_from_get(self.filter_form)
        ctx['advanced_filter_count'] = advanced_filter_count(self.filter_form)
        vouchers = list(ctx['vouchers'])
        group_tags = {voucher.tag for voucher in vouchers if voucher.tag}
        if group_tags:
            tags_with_limits = set(
                self.request.event.vouchers.filter(
                    waitinglistentries__isnull=True,
                    tag__in=group_tags,
                )
                .filter(Q(limit_products__isnull=False) | Q(limit_variations__isnull=False))
                .values_list('tag', flat=True)
            )
            group_totals = {
                total['tag']: total
                for total in self.request.event.vouchers.filter(waitinglistentries__isnull=True, tag__in=group_tags)
                .order_by()
                .values('tag')
                .annotate(
                    group_redeemed=Sum('redeemed'),
                    group_max_usages=Sum('max_usages'),
                    group_count=Count('pk'),
                    distinct_dates=Count('valid_until', distinct=True),
                    null_dates=Count(Case(When(valid_until__isnull=True, then=1))),
                    distinct_products=Count('product', distinct=True),
                    null_products=Count(Case(When(product__isnull=True, then=1))),
                    distinct_variations=Count('variation', distinct=True),
                    null_variations=Count(Case(When(variation__isnull=True, then=1))),
                    distinct_quotas=Count('quota', distinct=True),
                    null_quotas=Count(Case(When(quota__isnull=True, then=1))),
                    distinct_subevents=Count('subevent', distinct=True),
                    null_subevents=Count(Case(When(subevent__isnull=True, then=1))),
                )
            }
            for voucher in vouchers:
                if voucher.tag:
                    total = group_totals.get(voucher.tag)
                    voucher.group_redeemed = total['group_redeemed'] if total else 0
                    voucher.group_max_usages = total['group_max_usages'] if total else 0
                    voucher.group_count = total['group_count'] if total else 0
                    null_date_flag = 1 if (total and total['null_dates'] > 0) else 0
                    voucher.uniform_date = (total['distinct_dates'] + null_date_flag <= 1) if total else True
                    null_prod_flag = 1 if (total and total['null_products'] > 0) else 0
                    null_var_flag = 1 if (total and total['null_variations'] > 0) else 0
                    null_quota_flag = 1 if (total and total['null_quotas'] > 0) else 0
                    prod_uniform = (total['distinct_products'] + null_prod_flag <= 1) if total else True
                    var_uniform = (total['distinct_variations'] + null_var_flag <= 1) if total else True
                    quota_uniform = (total['distinct_quotas'] + null_quota_flag <= 1) if total else True
                    no_limits = voucher.tag not in tags_with_limits
                    voucher.uniform_product = bool(
                        total and no_limits and prod_uniform and var_uniform and quota_uniform
                    ) if total else True
                    null_sub_flag = 1 if (total and total['null_subevents'] > 0) else 0
                    voucher.uniform_subevent = (total['distinct_subevents'] + null_sub_flag <= 1) if total else True

        return ctx

    def get(self, request, *args, **kwargs):
        if request.GET.get('tab') == 'tags':
            query = request.GET.copy()
            query.pop('tab')
            return HttpResponseRedirect(f'{request.path}?{query.urlencode()}' if query else request.path)
        if request.GET.get('download', '') == 'yes':
            return self._download_csv()
        return super().get(request, *args, **kwargs)

    def _download_csv(self):
        output = io.StringIO()
        writer = csv.writer(output, quoting=csv.QUOTE_NONNUMERIC, delimiter=',')

        headers = [
            _('Voucher code'),
            _('Voucher link'),
            _('Valid until'),
            _('Product'),
            _('Reserve quota'),
            _('Bypass quota'),
            _('Bypass approval'),
            _('Price effect'),
            _('Value'),
            _('Tag'),
            _('Redeemed'),
            _('Maximum usages'),
            _('Seat'),
            _('Comment'),
        ]
        writer.writerow(headers)

        base_url = build_absolute_uri(self.request.event, 'presale:event.redeem')

        for v in Voucher.annotate_budget_used_orders(self.get_filtered_queryset()):
            url = f'{base_url}?voucher={quote_plus(v.code)}'
            if v.subevent_id and v.subevent and v.subevent.event_id == self.request.event.pk and v.subevent.active:
                url += f'&subevent={v.subevent_id}'
            if v.limit_products.exists() or v.limit_variations.exists():
                parts = [str(p) for p in v.limit_products.all()]
                parts += ['%s – %s' % (str(var.product), str(var)) for var in v.limit_variations.all()]
                prod = ', '.join(parts)
            elif v.product:
                if v.variation:
                    prod = '%s – %s' % (str(v.product), str(v.variation))
                else:
                    prod = '%s' % str(v.product)
            elif v.quota:
                prod = _('Any product in quota "{quota}"').format(quota=str(v.quota.name))
            else:
                prod = _('Any product')
            row = [
                v.code,
                url,
                v.valid_until.isoformat() if v.valid_until else '',
                prod,
                _('Yes') if v.block_quota else _('No'),
                _('Yes') if v.allow_ignore_quota else _('No'),
                _('Yes') if v.allow_ignore_approval else _('No'),
                v.get_price_mode_display(),
                str(v.value) if v.value is not None else '',
                v.tag,
                str(v.redeemed),
                str(v.max_usages),
                str(v.seat) if v.seat else '',
                str(v.comment) if v.comment else '',
            ]
            writer.writerow(row)

        r = HttpResponse(output.getvalue().encode('utf-8'), content_type='text/csv')
        r['Content-Disposition'] = 'attachment; filename="vouchers.csv"'
        return r


class VoucherGroupMembers(EventPermissionRequiredMixin, View):
    permission = 'can_view_vouchers'
    MEMBER_PAGE_SIZE = 50

    def get(self, request, *args, **kwargs):
        try:
            group_representative = request.event.vouchers.get(pk=kwargs['voucher'])
        except Voucher.DoesNotExist:
            raise Http404(_('The requested voucher does not exist.'))

        if not group_representative.tag:
            raise Http404(_('The requested voucher does not belong to a group.'))

        try:
            after = int(request.GET.get('after') or 0)
        except ValueError:
            return HttpResponseBadRequest()
        if after < 0:
            return HttpResponseBadRequest()

        members = Voucher.annotate_budget_used_orders(
            request.event.vouchers.filter(
                waitinglistentries__isnull=True,
                tag=group_representative.tag,
                pk__gt=after,
            )
            .select_related('product', 'variation', 'seat', 'quota', 'subevent')
            .prefetch_related('limit_products', 'limit_variations', 'limit_variations__product')
            .order_by('pk')
        )
        members = list(members[: self.MEMBER_PAGE_SIZE + 1])
        next_members_url = None
        if len(members) > self.MEMBER_PAGE_SIZE:
            next_members_url = '{}?after={}'.format(
                reverse(
                    'control:event.voucher.members',
                    kwargs={
                        'organizer': request.event.organizer.slug,
                        'event': request.event.slug,
                        'voucher': group_representative.pk,
                    },
                ),
                members[self.MEMBER_PAGE_SIZE - 1].pk,
            )
            members = members[: self.MEMBER_PAGE_SIZE]

        return render(
            request,
            'pretixcontrol/vouchers/_voucher_group_members.html',
            {
                'group_id': group_representative.pk,
                'members': members,
                'next_members_url': next_members_url,
            },
        )


class VoucherDelete(EventPermissionRequiredMixin, DeleteView):
    model = Voucher
    template_name = 'pretixcontrol/vouchers/delete.html'
    permission = 'can_change_vouchers'
    context_object_name = 'voucher'

    def get_object(self, queryset=None) -> Voucher:
        try:
            return self.request.event.vouchers.get(id=self.kwargs['voucher'])
        except Voucher.DoesNotExist:
            raise Http404(_('The requested voucher does not exist.'))

    def get(self, request, *args, **kwargs):
        if not self.get_object().allow_delete():
            messages.error(
                request,
                _('A voucher can not be deleted if it already has been redeemed.'),
            )
            return HttpResponseRedirect(self.get_success_url())
        return super().get(request, *args, **kwargs)

    @transaction.atomic
    def form_valid(self, form):
        self.object = self.get_object()
        success_url = self.get_success_url()

        if not self.object.allow_delete():
            messages.error(
                self.request,
                _('A voucher can not be deleted if it already has been redeemed.'),
            )
        else:
            self.object.log_action('eventyay.voucher.deleted', user=self.request.user)
            CartPosition.objects.filter(addon_to__voucher=self.object).delete()
            self.object.cartposition_set.all().delete()
            self.object.delete()
            messages.success(self.request, _('The selected voucher has been deleted.'))
        return HttpResponseRedirect(success_url)

    def get_success_url(self) -> str:
        return reverse(
            'control:event.vouchers',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        )


class VoucherUpdate(EventPermissionRequiredMixin, UpdateView):
    model = Voucher
    template_name = 'pretixcontrol/vouchers/detail.html'
    permission = 'can_change_vouchers'
    context_object_name = 'voucher'

    def get_form_class(self):
        form_class = VoucherForm
        for receiver, response in voucher_form_class.send(self.request.event, cls=form_class):
            if response:
                form_class = response
        return form_class

    def get_object(self, queryset=None) -> VoucherForm:
        url = resolve(self.request.path_info)
        try:
            return self.request.event.vouchers.get(id=url.kwargs['voucher'])
        except Voucher.DoesNotExist:
            raise Http404(_('The requested voucher does not exist.'))

    @transaction.atomic
    def form_valid(self, form):
        messages.success(self.request, _('Your changes have been saved.'))
        if form.has_changed():
            self.object.log_action(
                'eventyay.voucher.changed',
                user=self.request.user,
                data={k: form.cleaned_data.get(k) for k in form.changed_data},
            )
        return super().form_valid(form)

    def get_success_url(self) -> str:
        return reverse(
            'control:event.vouchers',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        )


class VoucherCreate(EventPermissionRequiredMixin, CreateView):
    model = Voucher
    template_name = 'pretixcontrol/vouchers/detail.html'
    permission = 'can_change_vouchers'
    context_object_name = 'voucher'

    def get_form_class(self):
        form_class = VoucherForm
        for receiver, response in voucher_form_class.send(self.request.event, cls=form_class):
            if response:
                form_class = response
        return form_class

    def get_success_url(self) -> str:
        return reverse(
            'control:event.vouchers',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['instance'] = Voucher(event=self.request.event)
        return kwargs

    @transaction.atomic
    def form_valid(self, form):
        form.instance.event = self.request.event
        ret = super().form_valid(form)
        url = reverse(
            'control:event.voucher',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
                'voucher': self.object.pk,
            },
        )
        messages.success(
            self.request,
            mark_safe(
                _('The new voucher has been created: {code}').format(
                    code=format_html('<a href="{url}">{code}</a>', url=url, code=self.object.code)
                )
            ),
        )
        form.instance.log_action('eventyay.voucher.added', data=dict(form.cleaned_data), user=self.request.user)
        return ret

    def post(self, request, *args, **kwargs):
        # TODO: Transform this into an asynchronous call?
        with request.event.lock():
            return super().post(request, *args, **kwargs)


class VoucherGo(EventPermissionRequiredMixin, View):
    permission = 'can_view_vouchers'

    def get_voucher(self, code):
        return Voucher.objects.get(code__iexact=code, event=self.request.event)

    def get(self, request, *args, **kwargs):
        code = request.GET.get('code', '').strip()
        try:
            voucher = self.get_voucher(code)
            return redirect(
                'control:event.voucher',
                event=request.event.slug,
                organizer=request.event.organizer.slug,
                voucher=voucher.id,
            )
        except Voucher.DoesNotExist:
            messages.error(request, _('There is no voucher with the given voucher code.'))
            return redirect(
                'control:event.vouchers',
                event=request.event.slug,
                organizer=request.event.organizer.slug,
            )


class VoucherBulkCreate(EventPermissionRequiredMixin, AsyncFormView):
    model = Voucher
    template_name = 'pretixcontrol/vouchers/bulk.html'
    permission = 'can_change_vouchers'
    context_object_name = 'voucher'

    def get_success_url(self, value) -> str:
        return reverse(
            'control:event.vouchers',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        )

    def get_error_url(self):
        return reverse(
            'control:event.vouchers',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        )

    @cached_property
    def copy_from(self):
        if self.request.GET.get('copy_from') and not getattr(self, 'object', None):
            try:
                return self.request.event.vouchers.get(pk=self.request.GET.get('copy_from'))
            except Voucher.DoesNotExist:
                pass

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()

        if self.copy_from:
            i = modelcopy(self.copy_from)
            i.pk = None
            i.redeemed = 0
            kwargs['instance'] = i
            if self.copy_from.limit_products.exists() or self.copy_from.limit_variations.exists():
                kwargs.setdefault('initial', {})
                kwargs['initial']['productvar'] = [str(p.pk) for p in self.copy_from.limit_products.all()] + [
                    '%d-%d' % (v.product_id, v.pk) for v in self.copy_from.limit_variations.all()
                ]
        else:
            kwargs['instance'] = Voucher(event=self.request.event, code=None)
        return kwargs

    def get_async_form_kwargs(self, form_kwargs, organizer=None, event=None):
        if not form_kwargs.get('instance'):
            form_kwargs['instance'] = Voucher(event=self.request.event, code=None)
        return form_kwargs

    def async_form_valid(self, task, form):
        lockfn = NoLockManager
        if form.data.get('block_quota'):
            lockfn = self.request.event.lock
        batch_size = 500
        total_num = 1  # will be set later

        def set_progress(percent):
            if not task.request.called_directly:
                task.update_state(state='PROGRESS', meta={'value': percent})

        def process_batch(batch_vouchers, voucherids):
            Voucher.objects.bulk_create(batch_vouchers)
            if not connection.features.can_return_rows_from_bulk_insert:
                batch_vouchers = list(self.request.event.vouchers.filter(code__in=[v.code for v in batch_vouchers]))

            log_entries = []
            for v in batch_vouchers:
                voucherids.append(v.pk)
                data = dict(form.cleaned_data)
                data['code'] = code
                data['bulk'] = True
                del data['codes']
                log_entries.append(
                    v.log_action(
                        'eventyay.voucher.added',
                        data=data,
                        user=self.request.user,
                        save=False,
                    )
                )
            LogEntry.objects.bulk_create(log_entries)
            form.post_bulk_save(batch_vouchers)
            batch_vouchers.clear()
            set_progress(len(voucherids) / total_num * (50.0 if form.cleaned_data['send'] else 100.0))

        voucherids = []
        with lockfn(), transaction.atomic():
            if not form.is_valid():
                raise ValidationError(form.errors)
            total_num = len(form.cleaned_data['codes'])

            batch_vouchers = []
            for code in form.cleaned_data['codes']:
                if len(batch_vouchers) > batch_size:
                    process_batch(batch_vouchers, voucherids)

                obj = modelcopy(form.instance, code=None)
                obj.event = self.request.event
                obj.code = code
                try:
                    obj.seat = form.cleaned_data['seats'].pop()
                    obj.product = obj.seat.product
                except IndexError:
                    pass
                batch_vouchers.append(obj)

            process_batch(batch_vouchers, voucherids)

        if form.cleaned_data['send']:
            vouchers_send(
                event=self.request.event,
                vouchers=voucherids,
                subject=form.cleaned_data['send_subject'],
                message=form.cleaned_data['send_message'],
                recipients=[r._asdict() for r in form.cleaned_data['send_recipients']],
                user=self.request.user.pk,
                progress=lambda p: set_progress(50.0 + p * 50.0),
            )

    def get_success_message(self, value):
        return _('The new vouchers have been created.')

    def get_form_class(self):
        form_class = VoucherBulkForm
        for receiver, response in voucher_form_class.send(self.request.event, cls=form_class):
            if response:
                form_class = response
        return form_class

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['code_length'] = settings.ENTROPY['voucher_code']
        ctx['max_codes'] = VoucherRNG.MAX_CODES
        ctx['max_prefix_length'] = VoucherRNG.get_max_prefix_length()
        return ctx


class VoucherRNG(EventPermissionRequiredMixin, View):
    permission = 'can_change_vouchers'
    MAX_CODES = 1_000

    @staticmethod
    def get_max_prefix_length() -> int:
        return Voucher._meta.get_field('code').max_length - settings.ENTROPY['voucher_code']

    def get(self, request, *args, **kwargs):
        codes = set()
        try:
            num = int(request.GET.get('num', '5'))
        except ValueError:  # NOQA
            return HttpResponseBadRequest()

        if num < 0 or num > self.MAX_CODES:
            # The codes are generated inside the request, so time and memory
            # grow with ``num``.
            return HttpResponseBadRequest()

        prefix = request.GET.get('prefix', '')
        if len(prefix) > self.get_max_prefix_length():
            return HttpResponseBadRequest()
        while len(codes) < num:
            new_codes = set()
            for i in range(min(num - len(codes), 500)):  # Work around SQLite's SQLITE_MAX_VARIABLE_NUMBER
                new_codes.add(_generate_random_code(prefix=prefix))
            new_codes -= set([v['code'] for v in Voucher.objects.filter(code__in=new_codes).values('code')])
            codes |= new_codes

        return JsonResponse({'codes': list(codes)})

    def get_success_url(self) -> str:
        return reverse(
            'control:event.vouchers',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        )


class VoucherBulkAction(EventPermissionRequiredMixin, View):
    permission = 'can_change_vouchers'

    @cached_property
    def objects(self):
        selected_vouchers = Q(id__in=self.request.POST.getlist('voucher'))
        selected_groups = self.request.POST.getlist('voucher_group')
        if selected_groups:
            selected_group_tags = (
                self.request.event.vouchers.filter(id__in=selected_groups).exclude(tag='').order_by().values('tag')
            )
            selected_vouchers |= Q(tag__in=Subquery(selected_group_tags), waitinglistentries__isnull=True)

        return self.request.event.vouchers.filter(selected_vouchers).distinct()

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        if not self.objects.exists():
            return redirect(self.get_success_url())

        if request.POST.get('action') == 'edit':
            return VoucherBulkEdit.as_view()(request, *args, **kwargs)

        if request.POST.get('action') == 'delete':
            selected_groups = self.request.POST.getlist('voucher_group')
            selected_vouchers = self.request.POST.getlist('voucher')
            if selected_groups and not selected_vouchers and len(selected_groups) == 1:
                return redirect(
                    reverse(
                        'control:event.voucher.delete_group',
                        kwargs={
                            'organizer': request.event.organizer.slug,
                            'event': request.event.slug,
                            'voucher': selected_groups[0],
                        },
                    )
                )
            return render(
                request,
                'pretixcontrol/vouchers/delete_bulk.html',
                {
                    'allowed': self.objects.filter(redeemed=0, orderposition__isnull=True),
                    'forbidden': self.objects.exclude(redeemed=0, orderposition__isnull=True),
                },
            )
        elif request.POST.get('action') == 'delete_confirm':
            allowed = self.objects.filter(redeemed=0, orderposition__isnull=True)
            forbidden = self.objects.exclude(redeemed=0, orderposition__isnull=True)
            has_allowed = allowed.exists()
            has_forbidden = forbidden.exists()

            allowed_ids = list(allowed.values_list('pk', flat=True))
            CartPosition.objects.filter(addon_to__voucher_id__in=allowed_ids).delete()
            CartPosition.objects.filter(voucher_id__in=allowed_ids).delete()

            for obj in allowed.iterator():
                obj.log_action('eventyay.voucher.deleted', user=self.request.user)
                obj.delete()

            if has_forbidden:
                messages.error(
                    request,
                    _('Deletion failed for some vouchers because they have already been redeemed or used in an order.'),
                )
                if has_allowed:
                    messages.success(request, _('The other selected vouchers have been deleted.'))
            else:
                messages.success(request, _('The selected vouchers have been deleted.'))
            transaction.on_commit(request.event.cache.clear)
        return redirect(self.get_success_url())

    def get_success_url(self) -> str:
        return reverse(
            'control:event.vouchers',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        )


class VoucherGroupDelete(EventPermissionRequiredMixin, View):
    permission = 'can_change_vouchers'

    def get_success_url(self) -> str:
        return reverse(
            'control:event.vouchers',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        )

    def get_group_vouchers(self):
        try:
            rep = self.request.event.vouchers.get(pk=self.kwargs['voucher'])
        except Voucher.DoesNotExist:
            raise Http404(_('The requested voucher does not exist.'))
        if not rep.tag:
            raise Http404(_('The requested voucher does not belong to a group.'))
        return rep, self.request.event.vouchers.filter(tag=rep.tag, waitinglistentries__isnull=True)

    def get(self, request, *args, **kwargs):
        rep, vouchers = self.get_group_vouchers()
        unused = vouchers.filter(redeemed=0, orderposition__isnull=True)
        used = vouchers.exclude(redeemed=0, orderposition__isnull=True)
        return render(
            request,
            'pretixcontrol/vouchers/delete_bulk.html',
            {
                'group_voucher': rep,
                'tag': rep.tag,
                'unused_count': unused.count(),
                'used_count': used.count(),
            },
        )

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        rep, vouchers = self.get_group_vouchers()
        expected_tag = request.POST.get('original_tag')
        if expected_tag and rep.tag != expected_tag:
            messages.error(request, _('The voucher group was modified. Please try again.'))
            return redirect(self.get_success_url())

        allowed = list(
            vouchers.filter(redeemed=0, orderposition__isnull=True).select_for_update(of=('self',))
        )
        allowed_ids = [v.pk for v in allowed]

        if allowed_ids:
            CartPosition.objects.filter(addon_to__voucher_id__in=allowed_ids).delete()
            CartPosition.objects.filter(voucher_id__in=allowed_ids).delete()
            log_entries = [
                v.log_action('eventyay.voucher.deleted', user=request.user, save=False)
                for v in allowed
            ]
            created_entries = LogEntry.objects.bulk_create(log_entries, batch_size=200)
            transaction.on_commit(lambda: [
                (notify.apply_async(args=(e.pk,)) if e.notification_type else None,
                 notify_webhooks.apply_async(args=(e.pk,)) if e.webhook_type else None)
                for e in created_entries
            ])
            self.request.event.vouchers.filter(pk__in=allowed_ids).delete()
            transaction.on_commit(request.event.cache.clear)
            messages.success(request, _('The unused vouchers in this group have been deleted.'))
        else:
            messages.info(request, _('No unused vouchers were found to delete.'))

        return redirect(self.get_success_url())


class VoucherBulkEdit(EventPermissionRequiredMixin, View):
    permission = 'can_change_vouchers'

    @cached_property
    def target_vouchers(self):
        group_pks = self.request.GET.getlist('voucher_group') or self.request.POST.getlist('voucher_group')
        voucher_pks = self.request.GET.getlist('voucher') or self.request.POST.getlist('voucher')
        target_tag = (
            self.request.GET.get('target_tag')
            or self.request.POST.get('target_tag')
            or self.request.GET.get('tag')
        )

        qs_filter = Q()
        if voucher_pks:
            qs_filter |= Q(pk__in=voucher_pks)
        if group_pks:
            tags = (
                self.request.event.vouchers.filter(pk__in=group_pks)
                .exclude(tag='')
                .order_by()
                .values('tag')
            )
            qs_filter |= Q(tag__in=Subquery(tags))
        elif target_tag:
            qs_filter |= Q(tag=target_tag)

        if not qs_filter:
            return self.request.event.vouchers.none()

        return (
            self.request.event.vouchers.filter(qs_filter, waitinglistentries__isnull=True)
            .distinct()
            .prefetch_related('limit_products', 'limit_variations', 'limit_variations__product')
        )

    def get_success_url(self) -> str:
        return reverse(
            'control:event.vouchers',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        )

    def render_form(self, request, form):
        vouchers = list(self.target_vouchers)
        total = len(vouchers)
        unused = sum(1 for v in vouchers if v.redeemed == 0)
        partly_used = sum(1 for v in vouchers if 0 < v.redeemed < v.max_usages)
        fully_used = sum(1 for v in vouchers if v.redeemed >= v.max_usages)

        sample = vouchers[0] if vouchers else Voucher(event=request.event)
        group_pks = request.GET.getlist('voucher_group') or request.POST.getlist('voucher_group')
        voucher_pks = request.GET.getlist('voucher') or request.POST.getlist('voucher')
        distinct_group_pks = set(group_pks)
        target_tag = (
            request.GET.get('target_tag')
            or request.POST.get('target_tag')
            or request.GET.get('tag')
        )
        if not target_tag and len(distinct_group_pks) == 1 and sample.tag:
            target_tag = sample.tag

        bulk_selected = request.POST.getlist('_bulk') if request.method == 'POST' else []

        return render(
            request,
            'pretixcontrol/vouchers/bulk_edit.html',
            {
                'form': form,
                'sample': sample,
                'vouchers_total': total,
                'vouchers_unused': unused,
                'vouchers_partly_used': partly_used,
                'vouchers_fully_used': fully_used,
                'voucher_pks': voucher_pks,
                'voucher_group_pks': group_pks,
                'target_tag': target_tag,
                'bulk_selected': bulk_selected,
            },
        )

    def get(self, request, *args, **kwargs):
        if not self.target_vouchers.exists():
            messages.warning(request, _('No vouchers selected.'))
            return redirect(self.get_success_url())

        sample = self.target_vouchers.first()
        form = VoucherBulkEditForm(instance=sample, target_vouchers=self.target_vouchers)
        return self.render_form(request, form)

    def post(self, request, *args, **kwargs):
        if not self.target_vouchers.exists():
            messages.warning(request, _('No vouchers selected.'))
            return redirect(self.get_success_url())

        sample = self.target_vouchers.first()
        action = request.POST.get('action')

        if action in ('edit', None):
            form = (
                VoucherBulkEditForm(
                    data=request.POST, instance=sample, target_vouchers=self.target_vouchers
                )
                if '_bulk' in request.POST
                else VoucherBulkEditForm(instance=sample, target_vouchers=self.target_vouchers)
            )
            return self.render_form(request, form)

        form = VoucherBulkEditForm(
            data=request.POST, instance=sample, target_vouchers=self.target_vouchers
        )
        bulk_fields = [f for f in request.POST.getlist('_bulk') if f in form.fields or f == 'productvar']

        if not bulk_fields:
            messages.error(request, _('You need to select at least one field to change.'))
            return self.render_form(request, form)

        if not form.is_valid():
            return self.render_form(request, form)

        # Classify vouchers
        vouchers = list(self.target_vouchers)
        new_max_usages = form.cleaned_data.get('max_usages') if 'max_usages' in bulk_fields else None
        to_update = []
        skipped_fully_used = []
        skipped_max_usages = []

        for v in vouchers:
            if v.redeemed >= v.max_usages:
                skipped_fully_used.append(v)
            elif new_max_usages is not None and v.redeemed > new_max_usages:
                skipped_max_usages.append(v)
            else:
                to_update.append(v)

        group_pks = request.POST.getlist('voucher_group')
        voucher_pks = request.POST.getlist('voucher')
        distinct_group_pks = set(group_pks)
        expected_tag = request.POST.get('original_tag')
        if action == 'save_confirm' and len(distinct_group_pks) == 1 and expected_tag:
            current_tags = set(
                self.request.event.vouchers.filter(pk__in=distinct_group_pks).values_list('tag', flat=True)
            )
            if current_tags != {expected_tag}:
                messages.error(request, _('The voucher group was modified. Please try again.'))
                return redirect(self.get_success_url())

        if action == 'save':
            # Step 2: Show confirmation preview before saving
            changed_fields_summary = []
            for f in bulk_fields:
                label = form.fields[f].label if f in form.fields else f
                val = form.cleaned_data.get(f)
                if f == 'price_mode':
                    display_val = dict(Voucher.PRICE_MODES).get(val, val)
                    if val != 'none' and form.cleaned_data.get('value') is not None:
                        display_val = f'{display_val} ({form.cleaned_data["value"]})'
                    changed_fields_summary.append((_('Price effect'), display_val))
                elif f == 'value':
                    if 'price_mode' not in bulk_fields:
                        changed_fields_summary.append((label, val))
                elif f == 'productvar':
                    if form._limit_products or form._limit_variations:
                        parts = [str(p) for p in form._limit_products] + [
                            f'{v.product} – {v}' for v in form._limit_variations
                        ]
                        display_val = ', '.join(parts)
                    elif form.instance.product:
                        display_val = (
                            f'{form.instance.product} – {form.instance.variation}'
                            if form.instance.variation
                            else str(form.instance.product)
                        )
                    elif form.instance.quota:
                        display_val = _('Any product in quota "{quota}"').format(quota=form.instance.quota.name)
                    else:
                        display_val = _('All products')
                    changed_fields_summary.append((_('Product'), display_val))
                else:
                    changed_fields_summary.append((label, val))

            target_tag = (
                request.POST.get('target_tag')
                or request.GET.get('target_tag')
                or request.GET.get('tag')
            )
            if not target_tag and len(distinct_group_pks) == 1 and sample.tag:
                target_tag = sample.tag

            return render(
                request,
                'pretixcontrol/vouchers/bulk_edit_confirm.html',
                {
                    'to_update_count': len(to_update),
                    'skipped_fully_used_count': len(skipped_fully_used),
                    'skipped_max_usages_count': len(skipped_max_usages),
                    'new_max_usages': new_max_usages,
                    'changed_fields_summary': changed_fields_summary,
                    'bulk_fields': bulk_fields,
                    'voucher_pks': voucher_pks,
                    'voucher_group_pks': group_pks,
                    'target_tag': target_tag,
                    'post_data': request.POST,
                },
            )

        elif action == 'save_confirm':
            # Step 3: Execute updates atomically under event lock
            with request.event.lock(), transaction.atomic():
                # Re-fetch under row lock to reclassify live redemption counts
                target_ids = list(self.target_vouchers.values_list('pk', flat=True))
                fresh_vouchers = list(
                    self.request.event.vouchers.filter(pk__in=target_ids)
                    .select_for_update()
                    .prefetch_related('limit_products', 'limit_variations', 'limit_variations__product')
                )
                to_update = []
                skipped_fully_used = []
                skipped_max_usages = []
                for v in fresh_vouchers:
                    if v.redeemed >= v.max_usages:
                        skipped_fully_used.append(v)
                    elif new_max_usages is not None and v.redeemed > new_max_usages:
                        skipped_max_usages.append(v)
                    else:
                        to_update.append(v)

                # Quota check under lock grouped by effective quota, product, variation, subevent, and limits
                quota_groups = {}
                for v in to_update:
                    eff_quota = form.instance.quota if 'productvar' in bulk_fields else v.quota
                    eff_product = form.instance.product if 'productvar' in bulk_fields else v.product
                    eff_variation = form.instance.variation if 'productvar' in bulk_fields else v.variation
                    eff_limit_p = (
                        getattr(form, '_limit_products', [])
                        if 'productvar' in bulk_fields
                        else list(v.limit_products.all())
                    )
                    eff_limit_v = (
                        getattr(form, '_limit_variations', [])
                        if 'productvar' in bulk_fields
                        else list(v.limit_variations.all())
                    )
                    eff_block_quota = (
                        form.cleaned_data['block_quota']
                        if 'block_quota' in bulk_fields
                        else v.block_quota
                    )
                    eff_subevent = (
                        form.cleaned_data['subevent']
                        if 'subevent' in bulk_fields
                        else v.subevent
                    )
                    eff_valid_until = (
                        form.cleaned_data['valid_until']
                        if 'valid_until' in bulk_fields
                        else v.valid_until
                    )
                    eff_max_usages = (
                        form.cleaned_data['max_usages']
                        if 'max_usages' in bulk_fields
                        else v.max_usages
                    )

                    v_data = {
                        'block_quota': eff_block_quota,
                        'subevent': eff_subevent,
                        'valid_until': eff_valid_until,
                        'max_usages': eff_max_usages,
                        'allow_ignore_quota': (
                            form.cleaned_data.get('allow_ignore_quota', False)
                            if 'allow_ignore_quota' in bulk_fields
                            else v.allow_ignore_quota
                        ),
                    }
                    product_changed = 'productvar' in bulk_fields and (
                        eff_quota != v.quota or eff_product != v.product or eff_variation != v.variation
                    )
                    delta = 0
                    is_new_blocker = Voucher.clean_quota_needs_checking(
                        v_data, v, product_changed=product_changed, creating=False
                    )
                    if is_new_blocker:
                        delta = max(0, eff_max_usages - v.redeemed)
                    elif eff_block_quota and (v_data['valid_until'] is None or v_data['valid_until'] >= now()):
                        if 'max_usages' in bulk_fields and eff_max_usages > v.max_usages:
                            delta = eff_max_usages - v.max_usages

                    if delta > 0:
                        group_key = (
                            eff_quota.pk if eff_quota else None,
                            eff_product.pk if eff_product else None,
                            eff_variation.pk if eff_variation else None,
                            eff_subevent.pk if eff_subevent else None,
                            tuple(sorted(p.pk for p in eff_limit_p)),
                            tuple(sorted(var.pk for var in eff_limit_v)),
                        )
                        if group_key not in quota_groups:
                            quota_groups[group_key] = {
                                'delta': 0,
                                'sample_v_data': v_data,
                                'sample_old_inst': v if is_new_blocker else None,
                                'eff_quota': eff_quota,
                                'eff_product': eff_product,
                                'eff_variation': eff_variation,
                                'eff_limit_p': eff_limit_p,
                                'eff_limit_v': eff_limit_v,
                            }
                        quota_groups[group_key]['delta'] += delta

                for group in quota_groups.values():
                    if group['delta'] > 0 and group['sample_v_data']:
                        try:
                            Voucher.clean_quota_check(
                                group['sample_v_data'],
                                group['delta'],
                                group['sample_old_inst'],
                                request.event,
                                group['eff_quota'],
                                group['eff_product'],
                                group['eff_variation'],
                                limit_products=group['eff_limit_p'],
                                limit_variations=group['eff_limit_v'],
                            )
                        except ValidationError as e:
                            messages.error(request, ' '.join(e.messages) if hasattr(e, 'messages') else str(e))
                            return redirect(self.get_success_url())

                scalar_fields = [f for f in bulk_fields if f not in ('productvar', 'value', 'price_mode')]
                update_fields = list(scalar_fields)
                if 'price_mode' in bulk_fields:
                    update_fields.extend(['price_mode', 'value'])
                elif 'value' in bulk_fields:
                    update_fields.append('value')
                if 'productvar' in bulk_fields:
                    update_fields.extend(['quota', 'product', 'variation'])

                for v in to_update:
                    for f in scalar_fields:
                        setattr(v, f, form.cleaned_data[f])
                    if 'price_mode' in bulk_fields:
                        v.price_mode = form.cleaned_data['price_mode']
                        v.value = None if v.price_mode == 'none' else form.cleaned_data.get('value')
                    elif 'value' in bulk_fields:
                        v.value = form.cleaned_data.get('value')
                    if 'productvar' in bulk_fields:
                        v.quota = form.instance.quota
                        v.product = form.instance.product
                        v.variation = form.instance.variation
                    v.save(update_fields=update_fields)

                if 'productvar' in bulk_fields and to_update:
                    v_ids = [v.pk for v in to_update]
                    Voucher.limit_products.through.objects.filter(voucher_id__in=v_ids).delete()
                    Voucher.limit_variations.through.objects.filter(voucher_id__in=v_ids).delete()
                    if getattr(form, '_limit_products', []):
                        Voucher.limit_products.through.objects.bulk_create([
                            Voucher.limit_products.through(voucher_id=vid, product_id=p.pk)
                            for vid in v_ids for p in form._limit_products
                        ], batch_size=500)
                    if getattr(form, '_limit_variations', []):
                        Voucher.limit_variations.through.objects.bulk_create([
                            Voucher.limit_variations.through(voucher_id=vid, productvariation_id=var.pk)
                            for vid in v_ids for var in form._limit_variations
                        ], batch_size=500)

                # Bulk log entries
                change_data = {}
                for f in bulk_fields:
                    val = form.cleaned_data.get(f)
                    if isinstance(val, (datetime.datetime, datetime.date)):
                        val = val.isoformat()
                    elif hasattr(val, 'pk'):
                        val = val.pk
                    elif isinstance(val, decimal.Decimal):
                        val = str(val)
                    change_data[f] = val
                if 'price_mode' in bulk_fields:
                    val = form.cleaned_data.get('value')
                    change_data['value'] = str(val) if isinstance(val, decimal.Decimal) else val
                if 'productvar' in bulk_fields:
                    change_data['productvar'] = form.cleaned_data.get('productvar')

                log_entries = [
                    v.log_action('eventyay.voucher.changed', user=request.user, data=change_data, save=False)
                    for v in to_update
                ]
                created_entries = LogEntry.objects.bulk_create(log_entries, batch_size=200)
                transaction.on_commit(lambda: [
                    (notify.apply_async(args=(e.pk,)) if e.notification_type else None,
                     notify_webhooks.apply_async(args=(e.pk,)) if e.webhook_type else None)
                    for e in created_entries
                ])
                transaction.on_commit(request.event.cache.clear)

            if to_update:
                messages.success(
                    request,
                    _('Your changes have been saved for {count} vouchers.').format(count=len(to_update)),
                )
            else:
                messages.warning(request, _('No vouchers were updated because all selected vouchers were skipped.'))

            return redirect(self.get_success_url())

        return redirect(self.get_success_url())

