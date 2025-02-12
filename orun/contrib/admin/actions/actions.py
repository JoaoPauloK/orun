import inspect
from typing import List, Optional, Type

from orun.apps import apps
from orun.http import HttpRequest
from orun.utils.translation import gettext
from orun.contrib.contenttypes.models import ContentType, Object, Registrable
from orun.db import models
from orun.conf import settings


class Action(Registrable):
    action_type: str = None
    name: str = None
    schema: str = None
    template_name: str = None
    usage: str = None
    description: str = None
    multiple = False

    def __init__(self, request: HttpRequest = None):
        self.request = request

    @classmethod
    def update_info(cls):
        action_info = {'name': cls.name or cls.__name__, 'qualname': cls.get_qualname()}
        return cls._register_object(apps[cls.action_type], cls.get_qualname(), action_info)


class WindowAction(Action):
    model: str = None
    name: str = None
    domain: dict = None
    view_mode: List[str] = ['list', 'form']
    view_type = 'form'

    @classmethod
    def update_info(cls):
        if (isinstance(cls.model, str) and cls.model not in apps.models) and (inspect.isclass(cls.model) and not issubclass(cls.model, models.Model)):
            print('model not found', cls.model)
            return
        model = apps[cls.model]
        name = cls.name or model._meta.verbose_name_plural
        action_info = {
            'usage': cls.usage,
            'description': cls.description,
            'name': name,
            'model': cls.model,
            'view_model': cls.view_mode,
            'view_type': cls.view_type,
        }
        return cls._register_object(apps['ui.action.window'], cls.get_qualname(), action_info)


class ViewAction(Action):
    action_type = 'ui.action.view'
    template_name: str = None

    @classmethod
    def get_context(cls, request):
        return {}

    @classmethod
    def render(cls, request):
        from orun.template.loader import get_template
        from orun.contrib.admin.models.ui import exec_query, exec_scalar, ref, query
        ctx = cls.get_context(request)
        ctx['env'] = apps
        ctx['settings'] = settings
        ctx['_'] = gettext
        ctx['exec_query'] = exec_query
        ctx['query'] = query
        ctx['exec_scalar'] = exec_scalar
        ctx['models'] = apps
        ctx['ref'] = ref
        return {
            'template': get_template(cls.template_name).render(ctx),
        }
