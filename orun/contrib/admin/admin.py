from orun.apps import apps
from orun.contrib.contenttypes.models import Registrable, ref


class MenuItem(Registrable):
    def __init__(self, cls: type):
        self._cls = cls
        self.qualname = f'{cls.__module__}.{cls.__name__}'

    def _register_menu_item(self, item: type, qualname: str, parent=None):
        from .models import Menu
        from orun.utils.text import re_camel_case
        name = getattr(item, 'name', None)
        if not name:
            name = re_camel_case.sub(r' \1', item.__name__).strip()
        action = getattr(item, 'action', None)
        if action:
            # find action id
            action = action.get_id()
        info = {
            'name': name,
            'sequence': getattr(item, 'sequence', 99),
            'icon': getattr(item, 'icon', None),
            'css': getattr(item, 'css', None),
            'parent': parent,
            'action_id': action,
        }
        m = self._register_object(Menu, qualname, info)
        # find children
        for k, child in item.__dict__.items():
            if k.startswith('_') or k == 'action':
                continue
            if isinstance(child, type):
                self._register_menu_item(child, f'{qualname}.{child.__name__}', m)
        return m

    def update_info(self):
        # mount the menu structure
        return self._register_menu_item(self._cls, self.qualname)

