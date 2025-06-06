import os
from collections import defaultdict

from orun import api
from orun.apps import apps
from orun.db import models
from orun.utils.translation import gettext_lazy as _
from orun.utils.module_loading import import_string
from orun.http import HttpRequest

from orun.contrib.auth.models import Group
from orun.contrib.contenttypes.models import ContentType, ref


class Action(models.Model):
    name = models.CharField(128, _('Name'), null=False, translate=True)
    action_type = models.CharField(32, _('Action Type'), null=False)
    usage = models.TextField(label=_('Usage'))
    description = models.TextField(label=_('Description'))
    # external_id = models.CharField(label=_('External ID'), getter='get_external_id')
    groups = models.OneToManyField('ui.action.groups.rel')
    binding_model = models.ForeignKey('content.type', on_delete=models.CASCADE)
    binding_type = models.SelectionField(
        (
            ('action', _('Action')),
            ('print', _('Print')),
        ),
        default='action',
    )
    multiple = models.BooleanField(default=False, label='Restrict to lists')
    qualname = models.CharField(help_text='System qualified name')

    class Meta:
        name = 'ui.action'
        field_groups = {
            'list_fields': ['name', 'action_type', 'usage']
        }

    def save(self, *args, **kwargs):
        if not self.action_type:
            self.action_type = self.__class__._meta.name
        super(Action, self).save(*args, **kwargs)

    def get_action(self):
        return apps[self.action_type].objects.get(pk=self.pk)

    @api.classmethod
    def load(cls, request: HttpRequest, name_or_id, context=None):
        try:
            name_or_id = int(name_or_id)
        except ValueError:
            if isinstance(name_or_id, str):
                name_or_id = ref(name_or_id)
        info = cls.get(name_or_id).get_action()._get_info(request, context)
        info['type'] = info.pop('action_type')
        return info

    def execute(self):
        raise NotImplemented()

    @classmethod
    def get_bindings(cls, model):
        r = defaultdict(list)
        # TODO: optimize filter by name (plain query)
        obj = apps['content.type'].objects.get_by_natural_key(model)
        for action in cls.objects.filter(binding_model_id=obj.pk):
            r[action.binding_type].append(action)
        return r

    def _get_info(self, request, context):
        return self.to_dict(exclude=['groups'])

    @api.classmethod
    def admin_get_groups(cls, request: HttpRequest, action_id):
        groups = {g.pk: g.allow_by_default for g in Group.objects.only('pk', 'allow_by_default').filter(active=True)}
        groups.update({g.group_id: g.allow for g in ActionGroups.objects.only('group_id').filter(action_id=action_id)})
        return groups

    @api.classmethod
    def admin_set_groups(cls, request: HttpRequest, action_id, groups: dict):
        ActionGroups.objects.filter(action_id=action_id).delete()
        ActionGroups.objects.bulk_create([
            ActionGroups(action_id=action_id, group_id=g, allow=v)
            for g, v in groups.items()
        ])
        return {
            'message': _('Permissions updated'),
        }


class ActionGroups(models.Model):
    action = models.ForeignKey(Action, null=False, on_delete=models.DB_CASCADE)  # permissions must be removed by cascade
    group = models.ForeignKey('auth.group', null=False, on_delete=models.DB_CASCADE)
    allow = models.BooleanField(default=True, label=_('Allow'))

    class Meta:
        name = 'ui.action.groups.rel'
        unique_together = ('action', 'group')


class WindowAction(Action):
    VIEW_MODE = (
        ('form', 'Form'),
        ('list', 'List'),
        ('card', 'Card'),
        ('search', 'Search'),
        ('calendar', 'Calendar'),
    )
    view = models.ForeignKey('ui.view', label=_('View'))
    domain = models.TextField(label=_('Domain'))
    context = models.TextField(label=_('Context'))
    model = models.CharField(128, null=False, label=_('Model'))
    object_id = models.BigIntegerField(label=_('Object ID'))
    #content_object = GenericForeignKey()
    view_mode = models.CharField(128, default='list,form', label=_('View Mode'))
    target = models.CharField(16, label=_('Target'), choices=(
        ('current', 'Current Window'),
        ('new', 'New Window'),
    ))
    limit = models.IntegerField(default=100, label=_('Limit'))
    auto_search = models.BooleanField(default=True, label=_('Auto Search'))
    # views = models.TextField(getter='_get_views', editable=False, serializable=True)
    view_list = models.OneToManyField('ui.action.window.view')
    view_type = models.SelectionField(VIEW_MODE, default='form')

    class Meta:
        name = 'ui.action.window'
        field_groups = {
            'list_fields': ['name', 'action_type', 'usage', 'view', 'model', 'view_mode', 'limit', 'auto_search']
        }

    def _get_views(self):
        modes = self.view_mode.split(',')
        views = self.view_list.all()
        modes = {mode: None for mode in modes}
        if self.view_id:
            modes[self.view_type] = self.view_id
        for v in views:
            modes[v.view_mode] = v.view_id
        if 'search' not in modes:
            modes['search'] = None
        return modes

    @classmethod
    def from_model(cls, model):
        if isinstance(model, models.Model):
            model = model._meta.name
        return cls.objects.filter(model=model).first()

    def _get_info(self, request, context):
        info = super()._get_info(request, context)
        # Send action information as katrid.js protocol
        modes = info['viewModes'] = info.pop('view_mode').split(',')
        # info['viewMode'] = info.pop('view_type')
        info['viewMode'] = modes[0]
        model = apps[self.model]
        info['fields'] = model.admin_get_fields_info()
        info['caption'] = info.pop('name')
        info['help_text'] = self.get_help_text(model)
        view_id = self.view_id
        views_info = info['viewsInfo'] = {}
        # check if there's a specified view
        if view_id:
            views_info[self.view.view_type] = model._admin_get_view_info(
                request, view_type=self.view_type, view=view_id, toolbar=True
            )
        views_info.update({
            k: model._admin_get_view_info(request, view_type=k, view=None, toolbar=True) for k in modes if k not in views_info
        })
        info['viewsInfo']['search'] = model._admin_get_view_info(request, view_type='search')
        if not request.user.has_perm(('create', 'update', 'delete'), model._meta.name):
            for vi in info['viewsInfo'].values():
                vi['readonly'] = True
        return info

    def get_help_text(self, model) -> str:
        if model._meta.addon and model._meta.addon.path:
            app_docs = os.path.join(model._meta.addon.docs_path, 'models', model._meta.name, 'index.md')
            if os.path.isfile(app_docs):
                with open(app_docs) as f:
                    return f.read()
            if model._meta.help_text:
                return model._meta.help_text


class WindowActionView(models.Model):
    window_action = models.ForeignKey(WindowAction, null=False)
    sequence = models.SmallIntegerField()
    view = models.ForeignKey('ui.view')
    view_mode = models.SelectionField(WindowAction.VIEW_MODE, label=_('View Type'))

    class Meta:
        name = 'ui.action.window.view'
        title_field = 'view'


class ViewAction(Action):
    view = models.ForeignKey('ui.view', label=_('View'))

    class Meta:
        name = 'ui.action.view'

    def _get_info(self, request, context):
        from orun.contrib.admin.models.ui import View
        res = super()._get_info(request, context)
        # it's a system class
        if self.qualname:
            admin_class = import_string(self.qualname)
            res.update(admin_class.render(None))
            del res['qualname']
        view = res.get('view')
        if view and view['id']:
            res['template'] = View.objects.get(pk=view['id']).get_content()
        return res

    @api.classmethod
    def get_view(cls, id):
        if isinstance(id, list):
            id = id[0]
        view = apps['ui.view'].objects.get(pk=id)
        return {
            'content': view.render({}),
            'type': view.view_type,
        }


class UrlAction(Action):
    url = models.TextField()
    target = models.SelectionField(
        (
            ('new', 'New Window'),
            ('self', 'Current Window'),
        ), default='new', null=False,
    )

    class Meta:
        name = 'ui.action.url'


class ServerAction(Action):
    sequence = models.IntegerField(default=5)
    model = models.ForeignKey('content.type', null=False)
    code = models.TextField(label=_('Python Code'))
    actions = models.ManyToManyField('self')
    target_model = models.ForeignKey('content.type')
    # target_field = models.ForeignKey('content.field')
    lines = models.OneToManyField('ui.action.server.line')

    class Meta:
        name = 'ui.action.server'


class ServerActionLine(models.Model):
    server_action = models.ForeignKey(ServerAction, null=False, on_delete=models.CASCADE)
    # field = models.ForeignKey('content.field')
    value = models.TextField()
    type = models.SelectionField(
        (
            ('value', _('Value')),
            ('expr', _('Python Expression')),
        ), label=_('Evaluation Type')
    )

    class Meta:
        name = 'ui.action.server.line'


class ClientAction(Action):
    tag = models.CharField(512)
    target = models.SelectionField(
        (
            ('current', 'Current Window'),
            ('new', 'New Window'),
            ('fullscreen', 'Full Screen'),
            ('main', 'Main Action of Current Window'),
        ), default='current',
    )
    model_name = models.CharField(label=_('Model'))
    context = models.TextField()
    params = models.TextField()
    view = models.ForeignKey('ui.view')

    class Meta:
        name = 'ui.action.client'

    @api.method(request=True)
    def get_view(self, id, request):
        vw = self.objects.get(id)
        if vw.view:
            return {
                'content': vw.view.render({}),
            }
