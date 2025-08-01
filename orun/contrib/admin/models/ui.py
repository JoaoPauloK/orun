import os
import json
import re
from jinja2 import Environment, FunctionLoader, Template
import logging

from orun.apps import apps
from orun import api
from orun.shortcuts import ref
from orun.template import loader
from orun.conf import settings
from orun.db import models, connection
from orun.utils.translation import gettext, gettext_lazy as _
from orun.utils.xml import etree
from orun.core.serializers.json import OrunJSONEncoder
from orun.contrib.auth.models import Permission

logger = logging.getLogger('orun')


def exec_query(q: str, fmt='json'):
    cur = connection.cursor()
    cur.execute(q)
    res = [list(row) for row in cur.fetchall()]
    if fmt == 'json':
        return json.dumps(res, cls=OrunJSONEncoder)


def query(q: str):
    cur = connection.cursor()
    cur.execute(q)
    return [list(row) for row in cur.fetchall()]


def exec_scalar(q: str):
    cur = connection.cursor()
    cur.execute(q)
    row = cur.fetchall()
    if row:
        return row[0][0]


def get_template(self, template):
    # TODO try to find on db (if not found, try to search on file system)
    app_label = template.split('/', 1)[0]
    addon = apps.addons[app_label]
    f = os.path.join(addon.root_path, addon.template_folder, template)
    if os.path.isfile(f):
        with open(f, encoding='utf-8') as tmpl:
            return tmpl.read()


views_env = Environment()

VIEW_TYPE = {
    'list': 'List',
    'form': 'Form',
    'card': 'Card',
    'chart': 'Chart',
    'calendar': 'Calendar',
    'search': 'Search',
    'template': 'Template',
    'report': 'Report',
    'dashboard': 'Dashboard',
    'custom': 'Custom',
    'class': 'Class',
}


class View(models.Model):
    name = models.CharField(max_length=100)
    active = models.BooleanField(label=_('Active'), default=True)
    parent = models.ForeignKey('self')
    view_type = models.ChoiceField(VIEW_TYPE, default='form', null=False)
    mode = models.ChoiceField(
        (
            ('primary', _('Primary')),
            ('extension', _('Extension'))
        ), default='extension', null=False
    )
    model = models.CharField(128, db_index=True)
    priority = models.IntegerField(_('Priority'), default=99, null=False)
    template_name = models.CharField(max_length=256)
    content = models.TextField(caption=_('Content'))
    ref_id = models.CharField(caption=_('Reference ID'), getter='_get_xml_id')
    # children = models.OneToManyField('self', 'parent')
    class_name = models.CharField(max_length=256, verbose_name='Python Class Name')
    object_type = models.ChoiceField({'system': 'System', 'user': 'User'}, default='system')

    class Meta:
        name = 'ui.view'
        ordering = ('priority', 'name')

    def save(self, *args, **kwargs):
        if not self.pk and self.parent_id is None:
            self.mode = 'primary'
        if self.parent_id and self.view_type != self.parent.view_type:
            self.view_type = self.parent.view_type
        elif self.view_type is None:
            xml = etree.fromstring(self.render({}))
            self.view_type = xml.tag
        super(View, self).save(*args, **kwargs)

    def _get_xml_id(self):
        obj = apps['ir.object'].objects.get_by_object_id(self._meta.name, self.id)
        if obj:
            return obj.name

    def get_content(self, model=None):
        xml = etree.tostring(self.get_xml(model))
        return xml

    def get_xml(self, model, context=None):
        if context is None:
            context = {}
        if model:
            context.update({'opts': model._meta if model else None})
        context['env'] = apps
        return self.compile(context)

    def xpath(self, source, element):
        pos = element.attrib.get('position')
        expr = element.attrib.get('expr')
        target = source
        if expr:
            val = target.xpath(expr)
            if val:
                target = val[0]
                self._merge(target, pos, element)

    def _merge(self, target: etree.HtmlElement, pos: str, element: etree.HtmlElement):
        if pos == 'append':
            for child in element:
                target.append(child)
        elif pos == 'insert':
            for child in reversed(element):
                target.insert(0, etree.fromstring(etree.tostring(child)))
        elif pos == 'before':
            parent = target.getparent()
            idx = parent.index(target)
            for child in reversed(element):
                parent.insert(idx, etree.fromstring(etree.tostring(child)))
        elif pos == 'after':
            parent = target.getparent()
            idx = parent.index(target) + 1
            for child in reversed(element):
                parent.insert(idx, etree.fromstring(etree.tostring(child)))
        elif pos == 'attributes':
            for child in element:
                target.attrib[child.attrib['name']] = child.text
        elif pos == 'replace':
            p = target.getparent()
            idx = p.index(target)
            p.remove(target)
            for child in element:
                p.insert(idx, etree.fromstring(etree.tostring(child)))

    def merge(self, target: etree.HtmlElement, element):
        for child in element:
            if child.tag == 'xpath':
                self.xpath(target, child)
            elif child.tag == 'insert' or child.tag == 'append':
                self._merge(target, child.tag, child)
        for k, v in element.attrib.items():
            target.attrib[k] = v

    def compile(self, context, parent=None):
        view_cls = self.__class__
        # in special cases, extensions without a parent should work as extension for all actions of a given model
        children = view_cls.objects.filter(
            models.Q(parent_id=self.pk) | models.Q(parent_id=None), mode='extension', model=self.model,
            view_type=self.view_type, active=True,
        )
        context['ref'] = ref
        context['exec_scalar'] = exec_scalar
        context['exec_query'] = exec_query
        context['query'] = query
        context['models'] = apps
        xml = etree.fromstring(self._get_content(context))
        if self.parent:
            parent_xml = etree.fromstring(self.parent.render(context))
            self.merge(parent_xml, xml)
            xml = parent_xml

        for child in children:
            self.merge(xml, etree.fromstring(child._get_content(context)))

        # self._eval_permissions(context['user'], xml)
        resolve_refs(xml)
        return xml

    def _eval_permissions(self, user_id, xml):
        """Remove elements without properly permissions"""

        _groups = {}
        children = xml.xpath("//*[@groups]")
        for child in children:
            pass
            # groups = child.attrib['groups']
            # if groups not in _groups:
            #     has_groups = len(list(objects.objects.only('id').filter(
            #         objects.c.model == 'auth.group', objects.c.name.in_(groups.split(',')),
            #         objects.c.object_id.in_(user.groups)
            #     )[:1])) > 0
            #     _groups[groups] = has_groups
            # if not _groups[groups]:
            #     child.getparent().remove(child)
        # python has permissions
        children = xml.xpath("//*[@if-permission]")
        for child in children:
            perm = child.attrib['if-permission']
            child.attrib.pop('if-permission')
            if perm and Permission.has_perm(user_id, self.model, perm):
                child.getparent().remove(child)

    def _get_content(self, context):
        if self.template_name:
            if self.view_type == 'report':
                templ = loader.get_template(self.template_name.split(':')[-1])
            else:
                templ = loader.get_template(self.template_name.split(':')[-1])
                # templ = apps.jinja_env.get_or_select_template(self.template_name.split(':')[-1])
                return templ.render(context)
            res = open(templ.template.filename, encoding='utf-8').read()
        else:
            res = self.content
        return res

    def to_string(self):
        templ = loader.find_template(self.template_name.split(':')[-1])
        with open(templ, encoding='utf-8') as f:
            return f.read()

    def render(self, context):
        from orun.template.loader import get_template
        context['env'] = apps
        context['_'] = gettext
        context['exec_query'] = exec_query
        context['query'] = query
        context['exec_scalar'] = exec_scalar
        context['models'] = apps
        context['ref'] = ref
        if self.view_type in ('dashboard', 'report'):
            context['db'] = {
                'connection': connection
            }
        if self.template_name:
            # context['ref'] = g.env.ref
            templ = self.template_name.split(':')[-1]
            if self.view_type in ('dashboard', 'report'):
                content = get_template(self.template_name)
                if isinstance(Template, str):
                    return Template(content).render(context)
                else:
                    return content.render(context)
                return apps.report_env.get_or_select_template(templ).render(**context)
            return loader.get_template(templ).render(context)
        if self.view_type == 'dashboard':
            return self.content
        return Template(self.content).render(context)

    @classmethod
    def generate_view(self, request, model, view_type='form'):
        opts = model._meta
        return render_template(
            request,
            [
                'views/%s/%s.html' % (opts.name, view_type),
                'views/%s/%s.xml' % (opts.name, view_type),
                'views/%s/%s.xml' % (opts.app_label, view_type),
                'views/%s.xml' % view_type,
            ], context=dict(opts=opts, _=gettext)
        )

    @classmethod
    def render_template(self, request, template, context):
        # find template by ref id
        templ = apps['ir.object'].get_by_natural_key(template).object
        children = list(self.objects.filter(mode='primary', parent=templ.id))
        if children:
            for child in children:
                pass
        else:
            views_env.from_string


class CustomView(models.Model):
    user = models.ForeignKey('auth.user', null=False)
    view = models.ForeignKey(View, null=False)
    content = models.TextField()

    class Meta:
        name = 'ui.view.custom'


class Filter(models.Model):
    name = models.CharField(256, null=False, verbose_name=_('Name'))
    user = models.ForeignKey('auth.user', on_delete=models.CASCADE)
    domain = models.TextField()
    context = models.TextField()
    sort = models.TextField()
    params = models.TextField()
    is_default = models.BooleanField(default=False)
    is_shared = models.BooleanField(default=True)
    action = models.ForeignKey('ui.action', verbose_name=_('Action'))
    # query = models.ForeignKey('ir.query')
    active = models.BooleanField(default=True)

    class Meta:
        name = 'ui.filter'


def resolve_refs(xml: etree.HtmlElement):
    # find action refs
    actions = xml.xpath('//action[@ref]')
    for action in actions:
        ref = action.attrib['ref']
        obj = apps['ir.object'].objects.get_by_natural_key(ref).content_object
        if obj:
            action.attrib['data-action'] = str(obj.pk)
            action.attrib['data-action-type'] = obj.action_type
            if not action.text:
                action.text = obj.name
            action.attrib.pop('ref')
        else:
            raise apps['ui.action'].ObjectDoesNotExists
    pass

# class UserDefinedFunctions(models.Model):
#     """User custom functions for client-side admin operations"""
#     model = models.ForeignKey('content.type')
#     action = models.ForeignKey('ui.action')
#     view_type = models.ChoiceField(VIEW_TYPE)
#     name = models.CharField(250, label=_('Function Name'))
#     active = models.BooleanField(default=True)
#     description = models.TextField(label=_('Description'), help_text=_('Description of the function purpose'))
#     language = models.CharField(default='js')
#     use_preprocess = models.BooleanField(
#         default=False, help_text=_('Preprocess the code using template processor before function execution')  # the code will be preprocessed using jinja2 before sent to client
#     )
#     code = models.TextField()
#
#     class Meta:
#         name = 'ui.udf'
