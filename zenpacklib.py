#!/usr/bin/env python

##############################################################################
#
# Copyright (C) Zenoss, Inc. 2013-2014, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

"""zenpacklib - ZenPack API abstraction.

This module provides a single integration point for common ZenPacks.

"""

import logging
LOG = logging.getLogger('zen.zenpacklib')

# Suppresses "No handlers could be found for logger" errors if logging
# hasn't been configured.
LOG.addHandler(logging.NullHandler())

import collections
import imp
import importlib
import inspect
import json
import operator
import os
import re
import sys
import math

if __name__ == '__main__':
    import Globals
    from Products.ZenUtils.Utils import unused
    unused(Globals)

from zope.browser.interfaces import IBrowserView
from zope.component import adapts, getGlobalSiteManager
from zope.event import notify
from zope.interface import classImplements, implements
from zope.interface.interface import InterfaceClass

from Products.AdvancedQuery import Eq, Or
from Products.AdvancedQuery.AdvancedQuery import _BaseQuery as BaseQuery
from Products.Five import zcml

from Products.ZenModel.Device import Device as BaseDevice
from Products.ZenModel.DeviceComponent import DeviceComponent as BaseDeviceComponent
from Products.ZenModel.HWComponent import HWComponent as BaseHWComponent
from Products.ZenModel.ManagedEntity import ManagedEntity as BaseManagedEntity
from Products.ZenModel.ZenossSecurity import ZEN_CHANGE_DEVICE
from Products.ZenModel.ZenPack import ZenPack as ZenPackBase
from Products.ZenRelations.Exceptions import ZenSchemaError
from Products.ZenRelations.RelSchema import ToMany, ToManyCont, ToOne
from Products.ZenRelations.ToManyContRelationship import ToManyContRelationship
from Products.ZenRelations.ToManyRelationship import ToManyRelationship
from Products.ZenRelations.ToOneRelationship import ToOneRelationship
from Products.ZenRelations.zPropertyCategory import setzPropertyCategory
from Products.ZenUI3.browser.interfaces import IMainSnippetManager
from Products.ZenUI3.utils.javascript import JavaScriptSnippet
from Products.ZenUtils.guid.interfaces import IGlobalIdentifier
from Products.ZenUtils.Search import makeFieldIndex, makeKeywordIndex
from Products.ZenUtils.Utils import monkeypatch, importClass

from Products import Zuul
from Products.Zuul.catalog.events import IndexingEvent
from Products.Zuul.catalog.global_catalog import ComponentWrapper as BaseComponentWrapper
from Products.Zuul.catalog.global_catalog import DeviceWrapper as BaseDeviceWrapper
from Products.Zuul.catalog.interfaces import IIndexableWrapper, IPathReporter
from Products.Zuul.catalog.paths import DefaultPathReporter, relPath
from Products.Zuul.decorators import info, memoize
from Products.Zuul.form import schema
from Products.Zuul.form.interfaces import IFormBuilder
from Products.Zuul.infos import InfoBase, ProxyProperty
from Products.Zuul.infos.component import ComponentInfo as BaseComponentInfo
from Products.Zuul.infos.component import ComponentFormBuilder as BaseComponentFormBuilder
from Products.Zuul.infos.device import DeviceInfo as BaseDeviceInfo
from Products.Zuul.interfaces import IInfo
from Products.Zuul.interfaces.component import IComponentInfo as IBaseComponentInfo
from Products.Zuul.interfaces.device import IDeviceInfo as IBaseDeviceInfo
from Products.Zuul.routers.device import DeviceRouter
from Products.Zuul.utils import ZuulMessageFactory as _t

from zope.publisher.interfaces.browser import IDefaultBrowserLayer
from zope.viewlet.interfaces import IViewlet

try:
    import yaml
    import yaml.constructor
    YAML_INSTALLED = True
except ImportError:
    YAML_INSTALLED = False

OrderedDict = None

try:
    # included in standard lib from Python 2.7
    from collections import OrderedDict
except ImportError:
    # try importing the backported drop-in replacement
    # it's available on PyPI
    try:
        from ordereddict import OrderedDict
    except ImportError:
        OrderedDict = None

# Exported symbols. These are the only symbols imported by wildcard.
__all__ = (
    # Classes
    'Device',
    'Component',
    'HardwareComponent',

    'TestCase',

    'ZenPackSpec',

    # Functions.
    'enableTesting',
    'ucfirst',
    'relname_from_classname',
    'relationships_from_yuml',
    'catalog_search',
    )

# Must defer definition of TestCase. Otherwise it imports
# BaseTestCase which puts Zope into testing mode.
TestCase = None

# Required for registering ZCSA adapters.
GSM = getGlobalSiteManager()


# Public Classes ############################################################

class ZenPack(ZenPackBase):
    """
    ZenPack loader that handles custom installation and removal tasks.
    """

    # NEW_COMPONENT_TYPES AND NEW_RELATIONS will be monkeypatched in
    # via zenpacklib when this class is instantiated.

    def _buildDeviceRelations(self):
        for d in self.dmd.Devices.getSubDevicesGen():
            d.buildRelations()

    def install(self, app):

        if not YAML_INSTALLED:
            LOG.fatal('PyYAML is required by %s.  Try "easy_install PyYAML" first.' % self.id)
            sys.exit(1)

        if not OrderedDict:
            LOG.fatal('ordereddict is required by %s. Try "easy_install ordereddict" first.' % self.id)
            sys.exit(1)

        # create device classes and set zProperties on them
        for dcname, dcspec in self.device_classes.iteritems():
            if dcspec.create:
                try:
                    self.dmd.Devices.getOrganizer(dcspec.path)
                except KeyError:
                    LOG.info('Creating DeviceClass %s' % dcspec.path)
                    app.dmd.Devices.createOrganizer(dcspec.path)

            dcObject = self.dmd.Devices.getOrganizer(dcspec.path)
            for zprop, value in dcspec.zProperties.iteritems():
                LOG.info('Setting zProperty %s on %s' % (zprop, dcspec.path))
                dcObject.setZenProperty(zprop, value)

        # Load objects.xml now
        super(ZenPack, self).install(app)
        if self.NEW_COMPONENT_TYPES:
            LOG.info('Adding %s relationships to existing devices' % self.id)
            self._buildDeviceRelations()

    def remove(self, app, leaveObjects=False):
        from Products.Zuul.interfaces import ICatalogTool
        if not leaveObjects:
            dc = app.Devices
            for catalog in self.GLOBAL_CATALOGS:
                catObj = getattr(dc, catalog, None)
                if catObj:
                    LOG.info('Removing Catalog %s' % catalog)
                    dc._delObject(catalog)

            if self.NEW_COMPONENT_TYPES:
                LOG.info('Removing %s components' % self.id)
                cat = ICatalogTool(app.zport.dmd)
                for brain in cat.search(types=self.NEW_COMPONENT_TYPES):
                    component = brain.getObject()
                    component.getPrimaryParent()._delObject(component.id)

                # Remove our Device relations additions.
                from Products.ZenUtils.Utils import importClass
                for device_module_id in self.NEW_RELATIONS:
                    Device = importClass(device_module_id)
                    Device._relations = tuple([x for x in Device._relations
                                               if x[0] not in self.NEW_RELATIONS[device_module_id]])

                LOG.info('Removing %s relationships from existing devices.' % self.id)
                self._buildDeviceRelations()

            for dcname, dcspec in self.device_classes.iteritems():
                if dcspec.remove:
                    LOG.info('Removing DeviceClass %s' % dcspec.path)
                    app.dmd.Devices.manage_deleteOrganizer(dcspec.path)

        super(ZenPack, self).remove(app, leaveObjects=leaveObjects)


class CatalogBase(object):
    """Base class that implements cataloging a property"""

    # By Default there is no default catalog created.
    _catalogs = {}

    def get_catalog_name(self, name, scope):
        if scope == 'device':
            return '{}Search'.format(name)
        else:
            name = self.__module__.replace('.', '_')
            return '{}Search'.format(name)

    def get_catalog(self, name, scope, create=True):
        """Return catalog by name."""
        spec = self._get_catalog_spec(name)
        if not spec:
            return

        if scope == 'device':
            try:
                return getattr(self.device(), self.get_catalog_name(name, scope))
            except AttributeError:
                if create:
                    return self._create_catalog(name, 'device')
        else:
            try:
                return getattr(self.dmd.Device, self.get_catalog_name(name, scope))
            except AttributeError:
                if create:
                    return self._create_catalog(name, 'global')
        return

    def get_catalog_scopes(self, name):
        """Return catalog scopes by name."""
        spec = self._get_catalog_spec(name)
        if not spec:
            []

        scopes = [spec['indexes'][x].get('scope', 'device') for x in spec['indexes']]
        if 'both' in scopes:
            scopes = [x for x in scopes if x != 'both']
            scopes.append('device')
            scopes.append('global')
        return set(scopes)

    def get_catalogs(self, whiteList=None):
        """Return all catalogs for this class."""
        catalogs = []
        for name in self._catalogs:
            for scope in self.get_catalog_scopes(name):
                if not whiteList:
                    catalogs.append(self.get_catalog(name, scope))
                else:
                    if scope in whiteList:
                        catalogs.append(self.get_catalog(name, scope, create=False))
        return catalogs

    def _get_catalog_spec(self, name):
        if not hasattr(self, '_catalogs'):
            LOG.error("%s has no catalogs defined", self.id)
            return

        spec = self._catalogs.get(name)
        if not spec:
            LOG.error("%s catalog definition is missing", name)
            return

        if not isinstance(spec, dict):
            LOG.error("%s catalog definition is not a dict", name)
            return

        if not spec.get('indexes'):
            LOG.error("%s catalog definition has no indexes", name)
            return

        return spec

    def _create_catalog(self, name, scope='device'):
        """Create and return catalog defined by name."""
        from Products.ZCatalog.Catalog import CatalogError
        from Products.ZCatalog.ZCatalog import manage_addZCatalog

        from Products.Zuul.interfaces import ICatalogTool

        spec = self._get_catalog_spec(name)
        if not spec:
            return

        if scope == 'device':
            catalog_name = self.get_catalog_name(name, scope)

            device = self.device()
            if not hasattr(device, catalog_name):
                manage_addZCatalog(device, catalog_name, catalog_name)

            zcatalog = device._getOb(catalog_name)
        else:
            catalog_name = self.get_catalog_name(name, scope)
            deviceClass = self.dmd.Devices

            if not hasattr(deviceClass, catalog_name):
                manage_addZCatalog(deviceClass, catalog_name, catalog_name)

            zcatalog = deviceClass._getOb(catalog_name)

        catalog = zcatalog._catalog

        classname = spec.get(
            'class', 'Products.ZenModel.DeviceComponent.DeviceComponent')

        for propname, propdata in spec['indexes'].items():
            index_type = propdata.get('type')
            if not index_type:
                LOG.error("%s index has no type", propname)
                return

            index_factory = {
                'field': makeFieldIndex,
                'keyword': makeKeywordIndex,
                }.get(index_type.lower())

            if not index_factory:
                LOG.error("%s is not a valid index type", index_type)
                return

            try:
                catalog.addIndex(propname, index_factory(propname))
                catalog.addColumn(propname)
            except CatalogError:
                # Index already exists.
                pass
            else:
                if scope == 'device':
                    results = ICatalogTool(device).search(types=(classname,))
                else:
                    results = ICatalogTool(deviceClass).search(types=(classname,))

                for result in results:
                    if hasattr(result.getObject(), 'index_object'):
                        result.getObject().index_object()

        return zcatalog

    def index_object(self, idxs=None):
        """Index in all configured catalogs."""
        for catalog in self.get_catalogs():
            if catalog:
                catalog.catalog_object(self, self.getPrimaryId())

    def unindex_object(self):
        """Unindex from all configured catalogs."""
        for catalog in self.get_catalogs():
            if catalog:
                catalog.uncatalog_object(self.getPrimaryId())


class ModelBase(CatalogBase):

    """Base class for ZenPack model classes."""

    def getIconPath(self):
        """Return relative URL path for class' icon."""
        return getattr(self, 'icon_url', '/zport/dmd/img/icons/noicon.png')


class DeviceBase(ModelBase):

    """First superclass for zenpacklib types created by DeviceTypeFactory.

    Contains attributes that should be standard on all ZenPack Device
    types.

    """

    def search(self, name, *args, **kwargs):
        return catalog_search(self, name, *args, **kwargs)


class ComponentBase(ModelBase):

    """First superclass for zenpacklib types created by ComponentTypeFactory.

    Contains attributes that should be standard on all ZenPack Component
    types.

    """

    factory_type_information = ({
        'actions': ({
            'id': 'perfConf',
            'name': 'Template',
            'action': 'objTemplates',
            'permissions': (ZEN_CHANGE_DEVICE,),
            },),
        },)

    _catalogs = {
        'ComponentBase': {
            'indexes': {
                'id': {'type': 'field'},
                }
            }
        }

    def device(self):
        """Return device under which this component/device is contained."""
        obj = self

        for i in xrange(200):
            if isinstance(obj, BaseDevice):
                return obj

            try:
                obj = obj.getPrimaryParent()
            except AttributeError:
                # While it is generally not normal to have devicecomponents
                # that are not part of a device, it CAN occur in certain
                # non-error situations, such as when it is in the process of
                # being deleted.  In that case, the DeviceComponentProtobuf
                # (Products.ZenMessaging.queuemessaging.adapters) implementation
                # expects device() to return None, not to throw an exception.
                return None

    def getIdForRelationship(self, relationship):
        """Return id in ToOne relationship or None."""
        obj = relationship()
        if obj:
            return obj.id

    def setIdForRelationship(self, relationship, id_):
        """Update ToOne relationship given relationship and id."""
        old_obj = relationship()

        # Return with no action if the relationship is already correct.
        if (old_obj and old_obj.id == id_) or (not old_obj and not id_):
            return

        # Remove current object from relationship.
        if old_obj:
            relationship.removeRelation()

            # Index old object. It might have a custom path reporter.
            notify(IndexingEvent(old_obj.primaryAq(), 'path', False))

        # If there is no new ID to add, we're done.
        if id_ is None:
            return

        # Find and add new object to relationship.
        for result in self.device().search('ComponentBase', id=id_):
            new_obj = result.getObject()
            relationship.addRelation(new_obj)

            # Index remote object. It might have a custom path reporter.
            notify(IndexingEvent(new_obj.primaryAq(), 'path', False))

            # For componentSearch. Would be nice if we could target
            # idxs=['getAllPaths'], but there's a chance that it won't exist
            # yet.
            new_obj.index_object()
            return

        LOG.error("setIdForRelationship (%s): No target found matching id=%s", relationship, id_)

    def getIdsInRelationship(self, relationship):
        """Return a list of object ids in relationship.

        relationship must be of type ToManyContRelationship or
        ToManyRelationship. Raises ValueError for any other type.

        """
        if isinstance(relationship, ToManyContRelationship):
            return relationship.objectIds()
        elif isinstance(relationship, ToManyRelationship):
            return [x.id for x in relationship.objectValuesGen()]

        try:
            type_name = type(relationship.aq_self).__name__
        except AttributeError:
            type_name = type(relationship).__name__

        raise ValueError(
            "invalid type '%s' for getIdsInRelationship()" % type_name)

    def setIdsInRelationship(self, relationship, ids):
        """Update ToMany relationship given relationship and ids."""
        new_ids = set(ids)
        current_ids = set(o.id for o in relationship.objectValuesGen())
        changed_ids = new_ids.symmetric_difference(current_ids)

        query = Or(*[Eq('id', x) for x in changed_ids])

        obj_map = {}
        for result in self.device().search('ComponentBase', query):
            obj_map[result.id] = result.getObject()

        for id_ in new_ids.symmetric_difference(current_ids):
            obj = obj_map.get(id_)
            if not obj:
                LOG.error(
                    "setIdsInRelationship (%s): No targets found matching "
                    "id=%s", relationship, id_)

                continue

            if id_ in new_ids:
                LOG.debug("Adding %s to %s" % (obj, relationship))
                relationship.addRelation(obj)

                # Index remote object. It might have a custom path reporter.
                notify(IndexingEvent(obj, 'path', False))
            else:
                LOG.debug("Removing %s from %s" % (obj, relationship))
                relationship.removeRelation(obj)

                # If the object was not deleted altogether..
                if not isinstance(relationship, ToManyContRelationship):
                    # Index remote object. It might have a custom path reporter.
                    notify(IndexingEvent(obj, 'path', False))

            # For componentSearch. Would be nice if we could target
            # idxs=['getAllPaths'], but there's a chance that it won't exist
            # yet.
            obj.index_object()

    @property
    def containing_relname(self):
        """Return name of containing relationship."""
        return self.get_containing_relname()

    @memoize
    def get_containing_relname(self):
        """Return name of containing relationship."""
        for relname, relschema in self._relations:
            if issubclass(relschema.remoteType, ToManyCont):
                return relname

    @property
    def faceting_relnames(self):
        """Return non-containing relationship names for faceting."""
        return self.get_faceting_relnames()

    @memoize
    def get_faceting_relnames(self):
        """Return non-containing relationship names for faceting."""
        faceting_relnames = []

        for relname, relschema in self._relations:
            if relname in FACET_BLACKLIST:
                continue

            if issubclass(relschema.remoteType, ToMany):
                faceting_relnames.append(relname)

        return faceting_relnames

    def get_facets(self, seen=None):
        """Generate non-containing related objects for faceting."""
        if seen is None:
            seen = set()

        for relname in self.get_faceting_relnames():
            rel = getattr(self, relname, None)
            if not rel or not callable(rel):
                continue

            relobjs = rel()
            if not relobjs:
                continue

            if isinstance(rel, ToOneRelationship):
                # This is really a single object.
                relobjs = [relobjs]

            for obj in relobjs:
                if obj in seen:
                    continue

                yield obj
                seen.add(obj)
                for facet in obj.get_facets(seen=seen):
                    yield facet

    def rrdPath(self):
        """Return filesystem path for RRD files for this component.

        Overrides RRDView to flatten component RRD files into a single
        subdirectory per-component per-device. This allows for the
        possibility of a component changing its contained path within
        the device without losing historical performance data.

        This requires that each component have a unique id within the
        device's namespace.

        """
        original = super(ComponentBase, self).rrdPath()

        try:
            # Zenoss 5 returns a JSONified dict from rrdPath.
            json.loads(original)
        except ValueError:
            # Zenoss 4 and earlier return a string that starts with "Devices/"
            return os.path.join('Devices', self.device().id, self.id)
        else:
            return original

    def getRRDTemplateName(self):
        """Return name of primary template to bind to this component."""
        if self._templates:
            return self._templates[0]

        return ''

    def getRRDTemplates(self):
        """Return list of templates to bind to this component.

        Enhances RRDView.getRRDTemplates by supporting both acquisition
        and inhertence template binding. Additionally supports user-
        defined *-replacement and *-addition monitoring templates that
        can replace or augment the standard templates respectively.

        """
        templates = []

        for template_name in self._templates:
            replacement = self.getRRDTemplateByName(
                '{}-replacement'.format(template_name))

            if replacement:
                templates.append(replacement)
            else:
                template = self.getRRDTemplateByName(template_name)
                if template:
                    templates.append(template)

            addition = self.getRRDTemplateByName(
                '{}-addition'.format(template_name))

            if addition:
                templates.append(addition)

        return templates


class DeviceIndexableWrapper(BaseDeviceWrapper):

    """Indexing wrapper for ZenPack devices.

    This is required to make sure that key classes are returned by
    objectImplements even if their depth within the inheritence tree
    would otherwise exclude them. Certain searches in Zenoss expect
    objectImplements to contain Device.

    """

    implements(IIndexableWrapper)
    adapts(DeviceBase)

    def objectImplements(self):
        """Return list of implemented interfaces and classes.

        Extends DeviceWrapper by ensuring that Device will always be
        part of the returned list.

        """
        dottednames = super(DeviceIndexableWrapper, self).objectImplements()

        return list(set(dottednames).union([
            'Products.ZenModel.Device.Device',
            ]))


GSM.registerAdapter(DeviceIndexableWrapper, (DeviceBase,), IIndexableWrapper)


class ComponentIndexableWrapper(BaseComponentWrapper):

    """Indexing wrapper for ZenPack components.

    This is required to make sure that key classes are returned by
    objectImplements even if their depth within the inheritence tree
    would otherwise exclude them. Certain searches in Zenoss expect
    objectImplements to contain DeviceComponent and ManagedEntity where
    applicable.

    """

    implements(IIndexableWrapper)
    adapts(ComponentBase)

    def objectImplements(self):
        """Return list of implemented interfaces and classes.

        Extends ComponentWrapper by ensuring that DeviceComponent will
        always be part of the returned list.

        """
        dottednames = super(ComponentIndexableWrapper, self).objectImplements()

        return list(set(dottednames).union([
            'Products.ZenModel.DeviceComponent.DeviceComponent',
            ]))


GSM.registerAdapter(ComponentIndexableWrapper, (ComponentBase,), IIndexableWrapper)


class ComponentPathReporter(DefaultPathReporter):

    """Global catalog path reporter adapter factory for components."""

    implements(IPathReporter)
    adapts(ComponentBase)

    def getPaths(self):
        paths = super(ComponentPathReporter, self).getPaths()

        for facet in self.context.get_facets():
            rp = relPath(facet, facet.containing_relname)
            paths.extend(rp)

        return paths

GSM.registerAdapter(ComponentPathReporter, (ComponentBase,), IPathReporter)


class ComponentFormBuilder(BaseComponentFormBuilder):

    """Base class for all custom FormBuilders.

    Adds support for renderers in the Component Details form.

    """

    implements(IFormBuilder)
    adapts(IInfo)

    def render(self, **kwargs):
        rendered = super(ComponentFormBuilder, self).render(kwargs)
        self.zpl_decorate(rendered)
        return rendered

    def zpl_decorate(self, item):
        if 'items' in item:
            for item in item['items']:
                self.zpl_decorate(item)
            return

        if 'xtype' in item and 'name' in item and item['xtype'] != 'linkfield':
            if item['name'] in self.renderer:
                renderer = self.renderer[item['name']]

                if renderer:
                    item['xtype'] = 'ZPLRenderableDisplayField'
                    item['renderer'] = renderer


def DeviceTypeFactory(name, bases):
    """Return a "ZenPackified" device class given bases tuple."""
    all_bases = (DeviceBase,) + bases

    def index_object(self, idxs=None, noips=False):
        for base in all_bases:
            if hasattr(base, 'index_object'):
                try:
                    base.index_object(self, idxs=idxs, noips=noips)
                except TypeError:
                    base.index_object(self)

    def unindex_object(self):
        for base in all_bases:
            if hasattr(base, 'unindex_object'):
                base.unindex_object(self)

    attributes = {
        'index_object': index_object,
        'unindex_object': unindex_object,
        }

    return type(name, all_bases, attributes)


Device = DeviceTypeFactory(
    'Device', (BaseDevice,))


def ComponentTypeFactory(name, bases):
    """Return a "ZenPackified" component class given bases tuple."""
    all_bases = (ComponentBase,) + bases

    def index_object(self, idxs=None):
        for base in all_bases:
            if hasattr(base, 'index_object'):
                try:
                    base.index_object(self, idxs=idxs)
                except TypeError:
                    base.index_object(self)

    def unindex_object(self):
        for base in all_bases:
            if hasattr(base, 'unindex_object'):
                base.unindex_object(self)

    attributes = {
        'index_object': index_object,
        'unindex_object': unindex_object,
        }

    return type(name, all_bases, attributes)


Component = ComponentTypeFactory(
    'Component', (BaseDeviceComponent, BaseManagedEntity))


HardwareComponent = ComponentTypeFactory(
    'HardwareComponent', (BaseHWComponent,))


class IHardwareComponentInfo(IBaseComponentInfo):

    """Info interface for ZenPackHardwareComponent.

    This exists because Zuul has no HWComponent info interface.
    """

    manufacturer = schema.Entity(title=u'Manufacturer')
    product = schema.Entity(title=u'Model')


class HardwareComponentInfo(BaseComponentInfo):

    """Info adapter factory for ZenPackHardwareComponent.

    This exists because Zuul has no HWComponent info adapter.
    """

    implements(IHardwareComponentInfo)
    adapts(HardwareComponent)

    @property
    @info
    def manufacturer(self):
        """Return Info for hardware product class' manufacturer."""
        product_class = self._object.productClass()
        if product_class:
            return product_class.manufacturer()

    @property
    @info
    def product(self):
        """Return Info for hardware product class."""
        return self._object.productClass()


# ZenPack Configuration #####################################################

FACET_BLACKLIST = (
    'dependencies',
    'dependents',
    'maintenanceWindows',
    'pack',
    'productClass',
    )


class Spec(object):

    """Abstract base class for specifications."""

    def specs_from_param(self, spec_type, param_name, param_dict, apply_defaults=True, leave_defaults=False):
        """Return a normalized dictionary of spec_type instances."""
        if param_dict is None:
            param_dict = {}
        elif not isinstance(param_dict, dict):
            raise TypeError(
                "{!r} argument must be dict or None, not {!r}"
                .format(
                    '{}.{}'.format(spec_type.__name__, param_name),
                    type(param_dict).__name__))
        else:
            if apply_defaults:
                _apply_defaults = globals()['apply_defaults']
                _apply_defaults(param_dict, leave_defaults=leave_defaults)

        specs = OrderedDict()
        for k, v in param_dict.iteritems():
            specs[k] = spec_type(self, k, **(fix_kwargs(v)))

        return specs

    @classmethod
    def init_params(cls):
        """Return a dictionary describing the parameters accepted by __init__"""

        argspec = inspect.getargspec(cls.__init__)
        if argspec.defaults:
            defaults = dict(zip(argspec.args[-len(argspec.defaults):], argspec.defaults))
        else:
            defaults = {}

        params = OrderedDict()
        for op, param, value in re.findall(
            "^\s*:(type|param|yaml_param|yaml_block_style)\s+(\S+):\s*(.*)$",
            cls.__init__.__doc__,
            flags=re.MULTILINE
        ):
            if param not in params:
                params[param] = {'description': None,
                                 'type': None,
                                 'yaml_param': param,
                                 'yaml_block_style': False}
                if param in defaults:
                    params[param]['default'] = defaults[param]

            if op == 'type':
                params[param]['type'] = value

                if 'default' not in params[param] or \
                   params[param]['default'] is None:
                    # For certain types, we know that None doesn't really mean
                    # None.
                    if params[param]['type'].startswith("dict"):
                        params[param]['default'] = {}
                    elif params[param]['type'].startswith("list"):
                        params[param]['default'] = []
                    elif params[param]['type'].startswith("SpecsParameter("):
                        params[param]['default'] = {}
            elif op == 'yaml_param':
                params[param]['yaml_param'] = value
            elif op == 'yaml_block_style':
                params[param]['yaml_block_style'] = bool(value)
            else:
                params[param]['description'] = value

        return params

    def __eq__(self, other):
        if type(self) != type(other):
            return False

        params = self.init_params()
        for p in params:
            if getattr(self, p) != getattr(other, p):
                LOG.debug("Comparing %s %s to %s %s, parameter %s does not match (%s != %s)",
                          self.__class__.__name__, self.name, other.__class__.__name__, other.name, p,
                          getattr(self, p), getattr(other, p))
                return False

        return True


class ZenPackSpec(Spec):

    """Representation of a ZenPack's desired configuration.

    Intended to be used to build a ZenPack declaratively as in the
    following example in a ZenPack's __init__.py:

        from . import zenpacklib

        CFG = zenpacklib.ZenPackSpec(
            name=__name__,

            zProperties={
                'zCiscoAPICHost': {
                    'category': 'Cisco APIC',
                    'type': 'string',
                },
                'zCiscoAPICPort': {
                    'category': 'Cisco APIC',
                    'default': '80',
                },
            },

            classes={
                'APIC': {
                    'base': zenpacklib.Device,
                },
                'FabricPod': {
                    'meta_type': 'Cisco APIC Fabric Pod',
                    'base': zenpacklib.Component,
                },
                'FvTenant': {
                    'meta_type': 'Cisco APIC Tenant',
                    'base': zenpacklib.Component,
                },
            },

            class_relationships=zenpacklib.relationships_from_yuml((
                "[APIC]++-[FabricPod]",
                "[APIC]++-[FvTenant]",
            ))
        )

        CFG.create()

    """

    def __init__(
            self,
            name,
            zProperties=None,
            classes=None,
            class_relationships=None,
            device_classes=None):
        """
            Create a ZenPack Specification

            :param name: Full name of the ZenPack (ZenPacks.zenoss.MyZenPack)
            :type name: str
            :param zProperties: zProperty Specs
            :type zProperties: SpecsParameter(ZPropertySpec)
            :param classes: Class Specs
            :type classes: SpecsParameter(ClassSpec)
            :param device_classes: DeviceClass Specs
            :type device_classes: SpecsParameter(DeviceClassSpec)
            :param class_relationships: Class Relationship Specs
            :type class_relationships: list(RelationshipSchemaSpec)
            :yaml_block_style class_relationships: True
        """
        self.name = name
        self.NEW_COMPONENT_TYPES = []
        self.NEW_RELATIONS = collections.defaultdict(list)

        # zProperties
        self.zProperties = self.specs_from_param(
            ZPropertySpec, 'zProperties', zProperties)

        # Class Relationship Schema
        self.class_relationships = []
        if class_relationships:
            if not isinstance(class_relationships, list):
                raise ValueError("class_relationships must be a list, not a %s" % type(class_relationships))

            for rel in class_relationships:
                self.class_relationships.append(RelationshipSchemaSpec(self, **rel))

        # Classes
        self.classes = self.specs_from_param(ClassSpec, 'classes', classes)
        self.imported_classes = {}

        # Import any external classes referred to in the schema
        for rel in self.class_relationships:
            for relschema in (rel.left_schema, rel.right_schema):
                className = relschema.remoteClass
                if '.' in className and className.split('.')[-1] not in self.classes:
                    module = ".".join(className.split('.')[0:-1])
                    try:
                        kls = importClass(module)
                        self.imported_classes[className] = kls
                    except ImportError:
                        pass

        # Class Relationships
        if classes:
            for classname, classdata in classes.iteritems():
                if 'relationships' not in classdata:
                    classdata['relationships'] = []

                relationships = classdata['relationships']
                for relationship in relationships:
                        # We do not allow the schema to be specified directly.
                        if 'schema' in relationships[relationship]:
                            raise ValueError("Class '%s': 'schema' may not be defined or modified in an individual class's relationship.  Use the zenpack's class_relationships instead." % classname)

        for class_ in self.classes.values():

            # Link the appropriate predefined (class_relationships) schema into place on this class's relationships list.
            for rel in self.class_relationships:
                if class_.name == rel.left_class:
                    if rel.left_relname not in class_.relationships:
                        class_.relationships[rel.left_relname] = ClassRelationshipSpec(class_, rel.left_relname)
                    class_.relationships[rel.left_relname].schema = rel.left_schema

                if class_.name == rel.right_class:
                    if rel.right_relname not in class_.relationships:
                        class_.relationships[rel.right_relname] = ClassRelationshipSpec(class_, rel.right_relname)
                    class_.relationships[rel.right_relname].schema = rel.right_schema

            # Plumb _relations
            for relname, relationship in class_.relationships.iteritems():
                if not relationship.schema:
                    LOG.error("Class '%s': no relationship schema has been defined for relationship '%s'" % (class_.name, relname))
                    continue

                if relationship.schema.remoteClass in self.imported_classes.keys():
                    remoteClass = relationship.schema.remoteClass  # Products.ZenModel.Device.Device
                    relname = relationship.schema.remoteName  # coolingFans
                    modname = relationship.class_.model_class.__module__  # ZenPacks.zenoss.HP.Proliant.CoolingFan
                    className = relationship.class_.model_class.__name__  # CoolingFan
                    remoteClassObj = self.imported_classes[remoteClass]  # Device_obj
                    remoteType = relationship.schema.remoteType  # ToManyCont
                    localType = relationship.schema.__class__  # ToOne
                    remote_relname = relationship.zenrelations_tuple[0]  # products_zenmodel_device_device

                    if relname not in (x[0] for x in remoteClassObj._relations):
                        remoteClassObj._relations += ((relname, remoteType(localType, modname, remote_relname)),)

                    remote_module_id = remoteClassObj.__module__
                    if relname not in self.NEW_RELATIONS[remote_module_id]:
                        self.NEW_RELATIONS[remote_module_id].append(relname)

                    component_type = '.'.join((modname, className))
                    if component_type not in self.NEW_COMPONENT_TYPES:
                        self.NEW_COMPONENT_TYPES.append(component_type)

        # Device Classes
        self.device_classes = self.specs_from_param(
            DeviceClassSpec, 'device_classes', device_classes)

    @property
    def ordered_classes(self):
        """Return ordered list of ClassSpec instances."""
        return sorted(self.classes.values(), key=operator.attrgetter('order'))

    def create(self):
        """Implement specification."""
        self.create_zenpack_class()

        for spec in self.zProperties.itervalues():
            spec.create()

        for spec in self.classes.itervalues():
            spec.create()

        self.create_product_names()
        self.create_ordered_component_tree()
        self.create_global_js_snippet()
        self.create_device_js_snippet()
        self.register_browser_resources()

    def create_product_names(self):
        """Add all classes to ZenPack's productNames list.

        This allows zenchkschema to verify the relationship schemas
        created by create().

        """
        productNames = getattr(self.zenpack_module, 'productNames', [])
        self.zenpack_module.productNames = list(
            set(list(self.classes.iterkeys()) + list(productNames)))

    def create_ordered_component_tree(self):
        """Monkeypatch DeviceRouter.getComponentTree to order components."""
        device_meta_types = {
            x.meta_type
            for x in self.classes.itervalues()
            if x.is_device}

        order = {
            x.meta_type: float(x.order)
            for x in self.classes.itervalues()}

        def getComponentTree(self, uid=None, id=None, **kwargs):
            # We do our own sorting.
            kwargs.pop('sorting_dict', None)

            # original is injected by monkeypatch.
            result = original(self, uid=uid, id=id, **kwargs)

            # Only change the order for custom device types.
            meta_type = self._getFacade().getInfo(uid=uid).meta_type
            if meta_type not in device_meta_types:
                return result

            return sorted(result, key=lambda x: order.get(x['id'], 100.0))

        monkeypatch(DeviceRouter)(getComponentTree)

    def register_browser_resources(self):
        """Register browser resources if they exist."""
        zenpack_path = get_zenpack_path(self.name)

        resource_path = os.path.join(zenpack_path, 'resources')
        if not os.path.isdir(resource_path):
            return

        directives = []
        directives.append(
            '<resourceDirectory name="{name}" directory="{directory}"/>'
            .format(
                name=self.name,
                directory=resource_path))

        def get_directive(name, for_, weight):
            path = os.path.join(resource_path, '{}.js'.format(name))
            if not os.path.isfile(path):
                return

            return (
                '<viewlet'
                '    name="js-{zenpack_name}-{name}"'
                '    paths="/++resource++{zenpack_name}/{name}.js"'
                '    for="{for_}"'
                '    weight="{weight}"'
                '    manager="Products.ZenUI3.browser.interfaces.IJavaScriptSrcManager"'
                '    class="Products.ZenUI3.browser.javascript.JavaScriptSrcBundleViewlet"'
                '    permission="zope.Public"'
                '    />'
                .format(
                    name=name,
                    for_=for_,
                    weight=weight,
                    zenpack_name=self.name))

        directives.append(get_directive('global', '*', 20))

        for spec in self.ordered_classes:
            if spec.is_device:
                for_ = get_symbol_name(self.name, spec.name, spec.name)

                directives.append(get_directive('device', for_, 21))
                directives.append(get_directive(spec.name, for_, 22))

        # Eliminate None items from list of directives.
        directives = tuple(x for x in directives if x)

        if directives:
            zcml.load_string(
                '<configure xmlns="http://namespaces.zope.org/browser">'
                '<include package="Products.Five" file="meta.zcml"/>'
                '<include package="Products.Five.viewlet" file="meta.zcml"/>'
                '{directives}'
                '</configure>'
                .format(
                    name=self.name,
                    directory=resource_path,
                    directives=''.join(directives)))

    def create_js_snippet(self, name, snippet, classes=None):
        """Create, register and return JavaScript snippet for given classes."""
        if isinstance(classes, (list, tuple)):
            classes = tuple(classes)
        else:
            classes = (classes,)

        def snippet_method(self):
            return snippet

        attributes = {
            '__allow_access_to_unprotected_subobjects__': True,
            'weight': 20,
            'snippet': snippet_method,
            }

        snippet_class = create_class(
            get_symbol_name(self.name),
            get_symbol_name(self.name, 'schema'),
            name,
            (JavaScriptSnippet,),
            attributes)

        try:
            target_name = 'global' if classes[0] is None else 'device'
        except Exception:
            target_name = 'global'

        for klass in classes:
            GSM.registerAdapter(
                snippet_class,
                (klass,) + (IDefaultBrowserLayer, IBrowserView, IMainSnippetManager),
                IViewlet,
                'js-snippet-{name}-{target_name}'
                .format(
                    name=self.name,
                    target_name=target_name))

        return snippet_class

    def create_global_js_snippet(self):
        """Create and register global JavaScript snippet."""
        snippets = []
        for spec in self.ordered_classes:
            snippets.append(spec.global_js_snippet)

        snippet = (
            "(function(){{\n"
            "var ZC = Ext.ns('Zenoss.component');\n"
            "{snippets}"
            "}})();\n"
            .format(
                snippets=''.join(snippets)))

        return self.create_js_snippet('global', snippet)

    def create_device_js_snippet(self):
        """Register device JavaScript snippet."""
        snippets = []
        for spec in self.ordered_classes:
            snippets.append(spec.device_js_snippet)

        # Don't register the snippet if there's nothing in it.
        if not [x for x in snippets if x]:
            return

        snippet = (
            "(function(){{\n"
            "var ZC = Ext.ns('Zenoss.component');\n"
            "{link_code}\n"
            "{snippets}"
            "}})();\n"
            .format(
                link_code=JS_LINK_FROM_GRID,
                snippets=''.join(snippets)))

        device_classes = [
            x.model_class
            for x in self.classes.itervalues()
            if Device in x.resolved_bases]

        # Add imported device objects
        for kls in self.imported_classes.itervalues():
            if 'deviceClass' in [x[0] for x in kls._relations]:
                device_classes.append(kls)

        return self.create_js_snippet(
            'device', snippet, classes=device_classes)

    @property
    def zenpack_module(self):
        """Return ZenPack module."""
        return importlib.import_module(self.name)

    @property
    def zenpack_class(self):
        """Return ZenPack class."""
        return self.create_zenpack_class()

    @memoize
    def create_zenpack_class(self):
        """Create ZenPack class."""
        packZProperties = [
            x.packZProperties for x in self.zProperties.itervalues()]

        attributes = {
            'packZProperties': packZProperties
            }

        attributes['device_classes'] = self.device_classes
        attributes['NEW_COMPONENT_TYPES'] = self.NEW_COMPONENT_TYPES
        attributes['NEW_RELATIONS'] = self.NEW_RELATIONS
        attributes['GLOBAL_CATALOGS'] = []
        global_catalog_classes = {}
        for (class_, class_spec) in self.classes.items():
            for (p, property_spec) in class_spec.properties.items():
                if property_spec.index_scope in ('both', 'global'):
                    global_catalog_classes[class_] = True
                    continue
        for class_ in global_catalog_classes:
            catalog = ".".join([self.name, class_]).replace(".", "_")
            attributes['GLOBAL_CATALOGS'].append('{}Search'.format(catalog))

        return create_class(get_symbol_name(self.name),
                            get_symbol_name(self.name, 'schema'),
                            'ZenPack',
                            (ZenPack,),
                            attributes)

    def test_setup(self):
        """Execute from a test suite's afterSetUp method.

        Our test layer appears to wipe out adapter registrations. We
        call this again after the layer has been setup so that
        programatically-registered adapters are in place for testing.

        """
        for spec in self.classes.itervalues():
            spec.test_setup()

        self.create_global_js_snippet()
        self.create_device_js_snippet()


class DeviceClassSpec(Spec):

    """Initialize a DeviceClass via Python at install time."""

    def __init__(self, zenpack_spec, path, create=True, zProperties=None,
                 remove=False, templates=None):
        """
            Create a DeviceClass Specification

            :param create: Create the DeviceClass with ZenPack installation, if it does not exist?
            :type create: bool
            :param remove: Remove the DeviceClass when ZenPack is removed?
            :type remove: bool
            :param zProperties: zProperty values to set upon this DeviceClass
            :type zProperties: dict(str)
            :param templates: TODO
            :type templates: SpecsParameter(RRDTemplateSpec)
        """

        self.zenpack_spec = zenpack_spec
        self.path = path.lstrip('/')
        self.create = bool(create)
        self.remove = bool(remove)

        if zProperties is None:
            self.zProperties = {}
        else:
            self.zProperties = zProperties

        self.templates = self.specs_from_param(
            RRDTemplateSpec, 'templates', templates)


class ZPropertySpec(Spec):

    """TODO."""

    def __init__(
            self,
            zenpack_spec,
            name,
            type_='string',
            default=None,
            category=None,
            ):
        """
            Create a ZProperty Specification

            :param type_: ZProperty Type (boolean, int, float, string, password, or lines)
            :yaml_param type_: type
            :type type_: str
            :param default: Default Value
            :type default: ZPropertyDefaultValue
            :param category: ZProperty Category.  This is used for display/sorting purposes.
            :type category: str
        """

        self.zenpack_spec = zenpack_spec
        self.name = name
        self.type_ = type_
        self.category = category

        if default is None:
            self.default = {
                'string': '',
                'password': '',
                'lines': [],
                'boolean': False,
                }.get(self.type_, None)
        else:
            self.default = default

    def create(self):
        """Implement specification."""
        if self.category:
            setzPropertyCategory(self.name, self.category)

    @property
    def packZProperties(self):
        """Return packZProperties tuple for this zProperty."""
        return (self.name, self.default, self.type_)


class ClassSpec(Spec):

    """TODO.


    'impacts' and 'impacted_by' will cause impact adapters to be registered, so the
    relationship is shown in impact, but not in dynamicview. If you would like to
    use dynamicview, you should change:

        'MyComponent': {
            'impacted_by': ['someRelationship']
            'impacts': ['someThingElse']
        }

    To:

        'MyComponent': {
            'dynamicview_views': ['service_view'],
            'dynamicview_relations': {
                'impacted_by': ['someRelationship']
                'impacts': ['someThingElse']
            }
        }

    This will cause your impact relationship to show in both dynamicview and impact.

    There is one important exception though.  Until ZEN-14579 is fixed, if your
    relationship/method returns an object that is not itself part of service_view,
    the dynamicview -> impact export will not include that object.

    To fix this, you must use impacts/impact_by for such relationships:

        'MyComponent': {
            'dynamicview_views': ['service_view'],
            'dynamicview_relations': {
                'impacted_by': ['someRelationship']
                'impacts': ['someThingElse']
            }
            impacted_by': ['someRelationToANonServiceViewThing']
        }

    If you need the object to appear in both DV and impact, include it in both lists.  If
    it would already be exported to impact, because it is in service_view, only use
    dynamicview_relations -> impacts/impacted_by, to avoid slowing down performance due
    to double adapters doing the same thing.
    """

    def __init__(
            self,
            zenpack,
            name,
            base=Component,
            meta_type=None,
            label=None,
            plural_label=None,
            short_label=None,
            plural_short_label=None,
            auto_expand_column='name',
            label_width=80,
            plural_label_width=None,
            content_width=None,
            icon=None,
            order=None,
            properties=None,
            relationships=None,
            impacts=None,
            impacted_by=None,
            monitoring_templates=None,
            filter_display=True,
            dynamicview_views=None,
            dynamicview_group=None,
            dynamicview_relations=None,
            ):
        """
            Create a Class Specification

            :param base: Base Class (defaults to Component)
            :type base: list(class)
            :param meta_type: meta_type (defaults to class name)
            :type meta_type: str
            :param label: Label to use when describing this class in the
                   UI.  If not specified, the default is to use the class name.
            :type label: str
            :param plural_label: Plural form of the label (default is to use the
                  "pluralize" function on the label)
            :type plural_label: str
            :param short_label: If specified, this is a shorter version of the
                   label.
            :type short_label: str
            :param plural_short_label:  If specified, this is a shorter version
                   of the short_label.
            :type plural_short_label: str
            :param auto_expand_column: The name of the column to expand to fill
                   available space in the grid display.  Defaults to the first
                   column ('name').
            :type auto_expand_column: str
            :param label_width: Optionally overrides ZPL's label width
                   calculation with a higher value.
            :type label_width: int
            :param plural_label_width: Optionally overrides ZPL's label width
                   calculation with a higher value.
            :type plural_label_width: int
            :param content_width: Optionally overrides ZPL's content width
                   calculation with a higher value.
            :type content_width: int
            :param icon: Filename (of a file within the zenpack's 'resources/icon'
                   directory).  Default is the {class name}.png
            :type icon: str
            :param order: TODO
            :type order: float
            :param properties: TODO
            :type properties: SpecsParameter(ClassPropertySpec)
            :param relationships: TODO
            :type relationships: SpecsParameter(ClassRelationshipSpec)
            :param impacts: TODO
            :type impacts: list(str)
            :param impacted_by: TODO
            :type impacted_by: list(str)
            :param monitoring_templates: TODO
            :type monitoring_templates: list(str)
            :param filter_display: TODO
            :type filter_display: bool
            :param dynamicview_views: TODO
            :type dynamicview_views: list(str)
            :param dynamicview_group: TODO
            :type dynamicview_group: str
            :param dynamicview_relations: TODO
            :type dynamicview_relations: dict
            # TODO: should make this a spec class, not a plain dict.
        """

        self.zenpack = zenpack
        self.name = name

        # Verify that bases is a tuple of base types.
        if isinstance(base, (tuple, list, set)):
            bases = tuple(base)
        else:
            bases = (base,)

        self.bases = bases
        self.base = self.bases

        self.meta_type = meta_type or self.name
        self.label = label or self.meta_type
        self.plural_label = plural_label or pluralize(self.label)

        if short_label:
            self.short_label = short_label
            self.plural_short_label = plural_short_label or pluralize(self.short_label)
        else:
            self.short_label = self.label
            self.plural_short_label = plural_short_label or self.plural_label

        self.auto_expand_column = auto_expand_column

        self.label_width = int(label_width)
        self.plural_label_width = plural_label_width or self.label_width + 7
        self.content_width = content_width or label_width

        self.icon = icon

        # Force properties into the 5.0 - 5.9 order range.
        if not order:
            self.order = 5.5
        else:
            self.order = 5 + (max(0, min(100, order)) / 100.0)

        # Properties.
        self.properties = self.specs_from_param(
            ClassPropertySpec, 'properties', properties)

        # Relationships.
        self.relationships = self.specs_from_param(
            ClassRelationshipSpec, 'relationships', relationships)

        # Impact.
        self.impacts = impacts
        self.impacted_by = impacted_by

        # Monitoring Templates.
        if monitoring_templates is None:
            self.monitoring_templates = [self.label.replace(' ', '')]
        elif isinstance(monitoring_templates, basestring):
            self.monitoring_templates = [monitoring_templates]
        else:
            self.monitoring_templates = list(monitoring_templates)

        self.filter_display = filter_display

        # Dynamicview Views and Group
        if dynamicview_views is None:
            self.dynamicview_views = ['service_view']
        elif isinstance(dynamicview_views, basestring):
            self.dynamicview_views = [dynamicview_views]
        else:
            self.dynamicview_views = list(dynamicview_views)

        if dynamicview_group is None:
            self.dynamicview_group = self.plural_short_label
        else:
            self.dynamicview_group = dynamicview_group

        # additional relationships to add, beyond IMPACTS and IMPACTED_BY.
        if dynamicview_relations is None:
            self.dynamicview_relations = {}
        else:
            # TAG_NAME: ['relationship', 'or_method']
            self.dynamicview_relations = dict(dynamicview_relations)

    def create(self):
        """Implement specification."""
        self.create_model_schema_class()
        self.create_iinfo_schema_class()
        self.create_info_schema_class()

        self.create_model_class()
        self.create_iinfo_class()
        self.create_info_class()

        if self.is_component or self.is_hardware_component:
            self.create_formbuilder_class()

        self.register_dynamicview_adapters()
        self.register_impact_adapters()

    @property
    @memoize
    def resolved_bases(self):
        """Return tuple of base classes.

        This is different than ClassSpec.bases in that all elements of
        the tuple will be type instances. ClassSpec.bases may contain
        string representations of types.
        """
        resolved_bases = []
        for base in self.bases:
            if isinstance(base, type):
                resolved_bases.append(base)
            else:
                base_spec = self.zenpack.classes[base]
                resolved_bases.append(base_spec.model_class)

        return tuple(resolved_bases)

    def base_class_specs(self, recursive=False):
        """Return tuple of base ClassSpecs.

        Iterates over ClassSpec.bases (possibly recursively) and returns
        instances of the ClassSpec objects for them.
        """
        base_specs = []
        for base in self.bases:
            if isinstance(base, type):
                # bases will contain classes rather than class names when referring
                # to a class outside of this zenpack specification.  Ignore
                # these.
                continue

            class_spec = self.zenpack.classes[base]
            base_specs.append(class_spec)

            if recursive:
                base_specs.extend(class_spec.base_class_specs())

        return tuple(base_specs)

    def subclass_specs(self):
        subclass_specs = []
        for class_spec in self.zenpack.classes.values():
            if self in class_spec.base_class_specs(recursive=True):
                subclass_specs.append(class_spec)

        return subclass_specs

    def inherited_properties(self):
        properties = {}
        for base in self.bases:
            if not isinstance(base, type):
                class_spec = self.zenpack.classes[base]
                properties.update(class_spec.properties)

        properties.update(self.properties)

        return properties

    def inherited_relationships(self):
        relationships = {}
        for base in self.bases:
            if not isinstance(base, type):
                class_spec = self.zenpack.classes[base]
                relationships.update(class_spec.relationships)

        relationships.update(self.relationships)

        return relationships

    def is_a(self, type_):
        """Return True if this class is a subclass of type_."""
        return issubclass(self.model_schema_class, type_)

    @property
    def is_device(self):
        """Return True if this class is a Device."""
        return self.is_a(Device)

    @property
    def is_component(self):
        """Return True if this class is a Component."""
        return self.is_a(Component)

    @property
    def is_hardware_component(self):
        """Return True if this class is a HardwareComponent."""
        return self.is_a(HardwareComponent)

    @property
    def icon_url(self):
        """Return relative URL to icon."""
        icon_filename = self.icon or '{}.png'.format(self.name)

        icon_path = os.path.join(
            get_zenpack_path(self.zenpack.name),
            'resources',
            'icon',
            icon_filename)

        if os.path.isfile(icon_path):
            return '/++resource++{zenpack_name}/icon/{filename}'.format(
                zenpack_name=self.zenpack.name,
                filename=icon_filename)

        return '/zport/dmd/img/icons/noicon.png'

    @property
    def model_schema_class(self):
        """Return model schema class."""
        return self.create_model_schema_class()

    def create_model_schema_class(self):
        """Create and return model schema class."""
        attributes = {
            'zenpack_name': self.zenpack.name,
            'meta_type': self.meta_type,
            'portal_type': self.meta_type,
            'icon_url': self.icon_url,
            'class_label': self.label,
            'class_plural_label': self.plural_label,
            'class_short_label': self.short_label,
            'class_plural_short_label': self.plural_short_label,
            'class_dynamicview_group': self.dynamicview_group,
            }

        properties = []
        relations = []
        templates = []
        catalogs = {}

        # First inherit from bases.
        for base in self.resolved_bases:
            if hasattr(base, '_properties'):
                properties.extend(base._properties)
            if hasattr(base, '_relations'):
                relations.extend(base._relations)
            if hasattr(base, '_templates'):
                templates.extend(base._templates)
            if hasattr(base, '_catalogs'):
                catalogs.update(base._catalogs)

        # Add local properties and catalog indexes.
        for name, spec in self.properties.iteritems():
            if not spec.datapoint:
                attributes[name] = spec.default  # defaults to None
            else:
                # Lookup the datapoint and get the value from rrd
                def datapoint_method(self, default=spec.datapoint_default, cached=spec.datapoint_cached, datapoint=spec.datapoint):
                    if cached:
                        r = self.cacheRRDValue(datapoint, default=default)
                    else:
                        r = self.getRRDValue(datapoint, default=default)

                    if r is not None:
                        if not math.isnan(float(r)):
                            return r
                    return default

                attributes[name] = datapoint_method

            if spec.ofs_dict:
                properties.append(spec.ofs_dict)

            pindexes = spec.catalog_indexes
            if pindexes:
                if self.name not in catalogs:
                    catalogs[self.name] = {
                        'indexes': {
                            'id': {'type': 'field'},
                        }
                    }
                catalogs[self.name]['indexes'].update(pindexes)

        # Add local relations.
        for name, spec in self.relationships.iteritems():
            relations.append(spec.zenrelations_tuple)

            # Add getter and setter to allow modeling. Only for local
            # relationships because base classes will provide methods
            # for their relationships.
            attributes['get_{}'.format(name)] = RelationshipGetter(name)
            attributes['set_{}'.format(name)] = RelationshipSetter(name)

        # Add local templates.
        templates.extend(self.monitoring_templates)

        attributes['_properties'] = tuple(properties)
        attributes['_relations'] = tuple(relations)
        attributes['_templates'] = tuple(templates)
        attributes['_catalogs'] = catalogs

        # Add Impact stuff.
        attributes['impacts'] = self.impacts
        attributes['impacted_by'] = self.impacted_by
        attributes['dynamicview_relations'] = self.dynamicview_relations

        return create_schema_class(
            get_symbol_name(self.zenpack.name, 'schema'),
            self.name,
            self.resolved_bases,
            attributes)

    @property
    def model_class(self):
        """Return model class."""
        return self.create_model_class()

    def create_model_class(self):
        """Create and return model class."""
        return create_stub_class(
            get_symbol_name(self.zenpack.name, self.name),
            self.model_schema_class,
            self.name)

    @property
    def iinfo_schema_class(self):
        """Return I<name>Info schema class."""
        return self.create_iinfo_schema_class()

    def create_iinfo_schema_class(self):
        """Create and return I<name>Info schema class."""
        bases = []
        for base_classname in self.zenpack.classes[self.name].bases:
            if base_classname in self.zenpack.classes:
                bases.append(self.zenpack.classes[base_classname].iinfo_class)

        if not bases:
            if self.is_device:
                bases = [IBaseDeviceInfo]
            elif self.is_component:
                bases = [IBaseComponentInfo]
            elif self.is_hardware_component:
                bases = [IHardwareComponentInfo]
            else:
                bases = [IInfo]

        attributes = {}

        for spec in self.inherited_properties().itervalues():
            attributes.update(spec.iinfo_schemas)

        for i, spec in enumerate(self.containing_components):
            attr = relname_from_classname(spec.name)
            attributes[attr] = schema.Entity(
                title=_t(spec.label),
                group="Relationships",
                order=3 + i / 100.0)

        for spec in self.inherited_relationships().itervalues():
            attributes.update(spec.iinfo_schemas)

        return create_schema_class(
            get_symbol_name(self.zenpack.name, 'schema'),
            'I{}Info'.format(self.name),
            tuple(bases),
            attributes)

    @property
    def iinfo_class(self):
        """Return I<name>Info class."""
        return self.create_iinfo_class()

    def create_iinfo_class(self):
        """Create and return I<Info>Info class."""
        return create_stub_class(
            get_symbol_name(self.zenpack.name, self.name),
            self.iinfo_schema_class,
            'I{}Info'.format(self.name))

    @property
    def info_schema_class(self):
        """Return <name>Info schema class."""
        return self.create_info_schema_class()

    def create_info_schema_class(self):
        """Create and return <name>Info schema class."""
        bases = []
        for base_classname in self.zenpack.classes[self.name].bases:
            if base_classname in self.zenpack.classes:
                bases.append(self.zenpack.classes[base_classname].info_class)

        if not bases:
            if self.is_device:
                bases = [BaseDeviceInfo]
            elif self.is_component:
                bases = [BaseComponentInfo]
            elif self.is_hardware_component:
                bases = [HardwareComponentInfo]
            else:
                bases = [InfoBase]

        attributes = {}
        attributes.update({
            'class_label': ProxyProperty('class_label'),
            'class_plural_label': ProxyProperty('class_plural_label'),
            'class_short_label': ProxyProperty('class_short_label'),
            'class_plural_short_label': ProxyProperty('class_plural_short_label')
        })

        for spec in self.containing_components:
            attr = None
            for rel, spec in self.relationships.items():
                if spec.remote_classname == spec.name:
                    attr = rel
                    continue

            if not attr:
                attr = relname_from_classname(spec.name)

            attributes[attr] = RelationshipInfoProperty(attr)

        for spec in self.inherited_properties().itervalues():
            attributes.update(spec.info_properties)

        for spec in self.inherited_relationships().itervalues():
            attributes.update(spec.info_properties)

        return create_schema_class(
            get_symbol_name(self.zenpack.name, 'schema'),
            '{}Info'.format(self.name),
            tuple(bases),
            attributes)

    @property
    def info_class(self):
        """Return Info subclass."""
        return self.create_info_class()

    def create_info_class(self):
        """Create and return Info subclass."""
        info_class = create_stub_class(
            get_symbol_name(self.zenpack.name, self.name),
            self.info_schema_class,
            '{}Info'.format(self.name))

        classImplements(info_class, self.iinfo_class)
        GSM.registerAdapter(info_class, (self.model_class,), self.iinfo_class)

        return info_class

    @property
    def formbuilder_class(self):
        """Return FormBuilder subclass."""
        return self.create_formbuilder_class()

    def create_formbuilder_class(self):
        """Create and return FormBuilder subclass.

        Includes rendering hints for ComponentFormBuilder.

        """
        bases = (ComponentFormBuilder,)
        attributes = {}
        renderer = {}

        # Find renderers for our properties:
        for propname, spec in self.properties.iteritems():
            renderer[propname] = spec.renderer

        # Find renderers for inherited properties
        for class_spec in self.base_class_specs(recursive=True):
            for propname, spec in class_spec.properties.iteritems():
                renderer[propname] = spec.renderer

        attributes['renderer'] = renderer

        formbuilder = create_class(
            get_symbol_name(self.zenpack.name, self.name),
            get_symbol_name(self.zenpack.name, 'schema'),
            '{}FormBuilder'.format(self.name),
            tuple(bases),
            attributes)

        classImplements(formbuilder, IFormBuilder)
        GSM.registerAdapter(formbuilder, (self.info_class,), IFormBuilder)

        return formbuilder

    def register_dynamicview_adapters(self):
        if not DYNAMICVIEW_INSTALLED:
            return

        if not self.dynamicview_views:
            return

        GSM.registerAdapter(
            DynamicViewRelatable,
            (self.model_class,),
            IRelatable)

        GSM.registerSubscriptionAdapter(
            DynamicViewRelationsProvider,
            required=(self.model_class,),
            provided=IRelationsProvider)

        dvm = DynamicViewMappings()

        groupName = self.model_class.class_dynamicview_group
        weight = 1000 + (self.order * 100)
        icon_url = getattr(self, 'icon_url', '/zport/dmd/img/icons/noicon.png')

        # Make sure the named utility is also registered.  It seems that
        # during unit tests, it may not be, even if the mapping is still
        # present.
        group = GSM.queryUtility(IGroup, groupName)
        if not group:
            group = BaseGroup(groupName, weight, None, icon_url)
            GSM.registerUtility(group, IGroup, groupName)

        for viewName in self.dynamicview_views:
            if groupName not in dvm.getGroupNames(viewName):
                dvm.addMapping(
                    viewName=viewName,
                    groupName=group.name,
                    weight=group.weight,
                    icon=group.icon)

    def register_impact_adapters(self):
        """Register Impact adapters."""

        if not IMPACT_INSTALLED:
            return

        if self.impacts or self.impacted_by:
            GSM.registerSubscriptionAdapter(
                ImpactRelationshipDataProvider,
                required=(self.model_class,),
                provided=IRelationshipDataProvider)

    @property
    def containing_components(self):
        """Return iterable of containing component ClassSpec instances.

        Instances will be sorted shallow to deep.

        """
        containing_specs = []

        for relname, relschema in self.model_schema_class._relations:
            if not issubclass(relschema.remoteType, ToManyCont):
                continue

            remote_classname = relschema.remoteClass.split('.')[-1]
            remote_spec = self.zenpack.classes.get(remote_classname)
            if not remote_spec or remote_spec.is_device:
                continue

            containing_specs.extend(remote_spec.containing_components)
            containing_specs.append(remote_spec)

        return containing_specs

    @property
    def faceting_components(self):
        """Return iterable of faceting component ClassSpec instances."""
        faceting_specs = []

        for relname, relschema in self.model_class._relations:
            if relname in FACET_BLACKLIST:
                continue

            if not issubclass(relschema.remoteType, ToMany):
                continue

            remote_classname = relschema.remoteClass.split('.')[-1]
            remote_spec = self.zenpack.classes.get(remote_classname)
            if remote_spec:
                for class_spec in [remote_spec] + remote_spec.subclass_specs():
                    if class_spec and not class_spec.is_device:
                        faceting_specs.append(class_spec)

        return faceting_specs

    @property
    def filterable_by(self):
        """Return meta_types by which this class can be filtered."""
        if not self.filter_display:
            return []

        containing = {x.meta_type for x in self.containing_components}
        faceting = {x.meta_type for x in self.faceting_components}
        return list(containing | faceting)

    @property
    def containing_js_fields(self):
        """Return list of JavaScript fields for containing components."""
        fields = []

        if self.is_device:
            return fields

        filtered_relationships = {}
        for r in self.relationships.values():
            if r.grid_display is False:
                filtered_relationships[r.remote_classname] = r

        for spec in self.containing_components:
            # grid_display=False
            if spec.name in filtered_relationships:
                continue
            fields.append(
                "{{name: '{}'}}"
                .format(
                    relname_from_classname(spec.name)))

        return fields

    @property
    def containing_js_columns(self):
        """Return list of JavaScript columns for containing components."""
        columns = []

        if self.is_device:
            return columns

        filtered_relationships = {}
        for r in self.relationships.values():
            if r.grid_display is False:
                filtered_relationships[r.remote_classname] = r

        for spec in self.containing_components:
            # grid_display=False
            if spec.name in filtered_relationships:
                continue

            width = max(spec.content_width + 14, spec.label_width + 20)
            renderer = 'Zenoss.render.zenpacklib_entityLinkFromGrid'

            column_fields = [
                "id: '{}'".format(spec.name),
                "dataIndex: '{}'".format(relname_from_classname(spec.name)),
                "header: _t('{}')".format(spec.short_label),
                "width: {}".format(width),
                "renderer: {}".format(renderer),
                ]

            columns.append('{{{}}}'.format(','.join(column_fields)))

        return columns

    @property
    def global_js_snippet(self):
        """Return global JavaScript snippet."""
        return (
            "ZC.registerName("
            "'{meta_type}', _t('{label}'), _t('{plural_label}')"
            ");\n"
            .format(
                meta_type=self.meta_type,
                label=self.label,
                plural_label=self.plural_label))

    @property
    def component_grid_panel_js_snippet(self):
        """Return ComponentGridPanel JavaScript snippet."""
        if self.is_device:
            return ''

        default_fields = [
            "{{name: '{}'}}".format(x) for x in (
                'uid', 'name', 'meta_type', 'class_label', 'status', 'severity',
                'usesMonitorAttribute', 'monitored', 'locking',
                )]

        default_left_columns = [(
            "{"
            "id: 'severity',"
            "dataIndex: 'severity',"
            "header: _t('Events'),"
            "renderer: Zenoss.render.severity,"
            "width: 50"
            "}"
        ), (
            "{"
            "id: 'name',"
            "dataIndex: 'name',"
            "header: _t('Name'),"
            "renderer: Zenoss.render.zenpacklib_entityLinkFromGrid"
            "}"
        )]

        default_right_columns = [(
            "{"
            "id: 'monitored',"
            "dataIndex: 'monitored',"
            "header: _t('Monitored'),"
            "renderer: Zenoss.render.checkbox,"
            "width: 70"
            "}"
        ), (
            "{"
            "id: 'locking',"
            "dataIndex: 'locking',"
            "header: _t('Locking'),"
            "renderer: Zenoss.render.locking_icons,"
            "width: 65"
            "}"
        )]

        fields = []
        ordered_columns = []

        # Keep track of pixel width of custom fields. Exceeding a
        # certain width causes horizontal scrolling of the component
        # grid panel.
        width = 0

        for spec in self.inherited_properties().itervalues():
            fields.extend(spec.js_fields)
            ordered_columns.extend(spec.js_columns)
            width += spec.js_columns_width

        for spec in self.inherited_relationships().itervalues():
            fields.extend(spec.js_fields)
            ordered_columns.extend(spec.js_columns)
            width += spec.js_columns_width

        if width > 750:
            LOG.warning(
                "%s: %s custom columns exceed 750 pixels (%s)",
                self.zenpack.name, self.name, width)

        return (
            "ZC.{meta_type}Panel = Ext.extend(ZC.ZPLComponentGridPanel, {{"
            "    constructor: function(config) {{\n"
            "        config = Ext.applyIf(config||{{}}, {{\n"
            "            componentType: '{meta_type}',\n"
            "            autoExpandColumn: '{auto_expand_column}',\n"
            "            fields: [{fields}],\n"
            "            columns: [{columns}]\n"
            "        }});\n"
            "        ZC.{meta_type}Panel.superclass.constructor.call(this, config);\n"
            "    }}\n"
            "}});\n"
            "\n"
            "Ext.reg('{meta_type}Panel', ZC.{meta_type}Panel);\n"
            .format(
                meta_type=self.meta_type,
                auto_expand_column=self.auto_expand_column,
                fields=','.join(
                    default_fields +
                    self.containing_js_fields +
                    fields),
                columns=','.join(
                    default_left_columns +
                    self.containing_js_columns +
                    ordered_values(ordered_columns) +
                    default_right_columns)))

    @property
    def subcomponent_nav_js_snippet(self):
        """Return subcomponent navigation JavaScript snippet."""
        cases = []
        for meta_type in self.filterable_by:
            cases.append("case '{}': return true;".format(meta_type))

        if not cases:
            return ''

        return (
            "Zenoss.nav.appendTo('Component', [{{\n"
            "    id: 'component_{meta_type}',\n"
            "    text: _t('{plural_label}'),\n"
            "    xtype: '{meta_type}Panel',\n"
            "    subComponentGridPanel: true,\n"
            "    filterNav: function(navpanel) {{\n"
            "        switch (navpanel.refOwner.componentType) {{\n"
            "            {cases}\n"
            "            default: return false;\n"
            "        }}\n"
            "    }},\n"
            "    setContext: function(uid) {{\n"
            "        ZC.{meta_type}Panel.superclass.setContext.apply(this, [uid]);\n"
            "    }}\n"
            "}}]);\n"
            .format(
                meta_type=self.meta_type,
                plural_label=self.plural_short_label,
                cases=' '.join(cases)))

    @property
    def dynamicview_nav_js_snippet(self):
        if DYNAMICVIEW_INSTALLED:
            return (
                "Zenoss.nav.appendTo('Component', [{\n"
                "    id: 'subcomponent_view',\n"
                "    text: _t('Dynamic View'),\n"
                "    xtype: 'dynamicview',\n"
                "    relationshipFilter: 'impacted_by',\n"
                "    viewName: 'service_view'\n"
                "}]);\n"
                )
        else:
            return ""

    @property
    def device_js_snippet(self):
        """Return device JavaScript snippet."""
        return ''.join((
            self.component_grid_panel_js_snippet,
            self.subcomponent_nav_js_snippet,
            self.dynamicview_nav_js_snippet,
            ))

    def test_setup(self):
        """Execute from a test suite's afterSetUp method.

        Our test layer appears to wipe out adapter registrations. We
        call this again after the layer has been setup so that
        programatically-registered adapters are in place for testing.

        """
        self.create_iinfo_class()
        self.create_info_class()
        self.register_dynamicview_adapters()
        self.register_impact_adapters()


class ClassPropertySpec(Spec):

    """TODO."""

    def __init__(
            self,
            class_spec,
            name,
            type_='string',
            label=None,
            short_label=None,
            index_type=None,
            label_width=80,
            default=None,
            content_width=None,
            display=True,
            details_display=True,
            grid_display=True,
            renderer=None,
            order=None,
            editable=False,
            api_only=False,
            api_backendtype='property',
            enum=None,
            datapoint=None,
            datapoint_default=None,
            datapoint_cached=True,
            index_scope='device'
            ):
        """
        Create a Class Property Specification

            :param type_: Property Data Type (TODO (enum))
            :yaml_param type_: type
            :type type_: str
            :param label: Label to use when describing this property in the
                   UI.  If not specified, the default is to use the name of the
                   property.
            :type label: str
            :param short_label: If specified, this is a shorter version of the
                   label, used, for example, in grid table headings.
            :type short_label: str
            :param index_type: TODO (enum)
            :type index_type: str
            :param label_width: Optionally overrides ZPL's label width
                   calculation with a higher value.
            :type label_width: int
            :param default: Default Value
            :type default: str
            :param content_width: Optionally overrides ZPL's content width
                   calculation with a higher value.
            :type content_width: int
            :param display: If this is set to False, this property will be
                   hidden from the UI completely.
            :type display: bool
            :param details_display: If this is set to False, this property
                   will be hidden from the "details" portion of the UI.
            :type details_display: bool
            :param grid_display: If this is set to False, this property
                   will be hidden from the "grid" portion of the UI.
            :type grid_display: bool
            :param renderer: Optional name of a javascript renderer to apply
                   to this property, rather than passing the text through
                   unformatted.
            :type renderer: str
            :param order: TODO
            :type order: float
            :param editable: TODO
            :type editable: bool
            :param api_only: TODO
            :type api_only: bool
            :param api_backendtype: TODO (enum)
            :type api_backendtype: str
            :param enum: TODO
            :type enum: list(str)
            :param datapoint: TODO (validate datapoint name)
            :type datapoint: str
            :param datapoint_default: TODO  - DEPRECATE (use default instead)
            :type datapoint_default: str
            :param datapoint_cached: TODO
            :type datapoint_cached: bool
            :param index_scope: TODO (enum)
            :type index_scope: str

        """

        self.class_spec = class_spec
        self.name = name
        self.default = default
        self.type_ = type_
        self.label = label or self.name
        self.short_label = short_label or self.label
        self.index_type = index_type
        self.index_scope = index_scope
        self.label_width = label_width
        self.content_width = content_width or label_width
        self.display = display
        self.details_display = details_display
        self.grid_display = grid_display
        self.renderer = renderer

        # pick an appropriate default renderer for this property.
        if type_ == 'entity' and not self.renderer:
            self.renderer = 'Zenoss.render.zenpacklib_entityLinkFromGrid'

        self.editable = bool(editable)
        self.api_only = bool(api_only)
        self.api_backendtype = api_backendtype
        if isinstance(enum, (set, list, tuple)):
            enum = dict(enumerate(enum))
        self.enum = enum
        self.datapoint = datapoint
        self.datapoint_default = datapoint_default
        self.datapoint_cached = bool(datapoint_cached)
        # Force api mode when a datapoint is supplied
        if self.datapoint:
            self.api_only = True
            self.api_backendtype = 'method'

        if self.api_backendtype not in ('property', 'method'):
            raise TypeError(
                "Property '%s': api_backendtype must be 'property' or 'method', not '%s'"
                % (name, self.api_backendtype))

        if self.index_scope not in ('device', 'global', 'both'):
            raise TypeError(
                "Property '%s': index_scope must be 'device', 'global', or 'both', not '%s'"
                % (name, self.index_scope))

        # Force properties into the 4.0 - 4.9 order range.
        if not order:
            self.order = 4.5
        else:
            self.order = 4 + (max(0, min(100, order)) / 100.0)

    @property
    def ofs_dict(self):
        """Return OFS _properties dictionary."""
        if self.api_only:
            return None

        return {
            'id': self.name,
            'label': self.label,
            'type': self.type_,
            }

    @property
    def catalog_indexes(self):
        """Return catalog indexes dictionary."""
        if not self.index_type:
            return {}

        return {
            self.name: {'type': self.index_type,
                        'scope': self.index_scope},
            }

    @property
    def iinfo_schemas(self):
        """Return IInfo attribute schema dict.

        Return None if type has no known schema.

        """
        schema_map = {
            'boolean': schema.Bool,
            'int': schema.Int,
            'float': schema.Float,
            'lines': schema.Text,
            'string': schema.TextLine,
            'password': schema.Password,
            'entity': schema.Entity
            }

        if self.type_ not in schema_map:
            return {}

        if self.details_display is False:
            return {}

        return {
            self.name: schema_map[self.type_](
                title=_t(self.label),
                alwaysEditable=self.editable,
                order=self.order)
            }

    @property
    def info_properties(self):
        """Return Info properties dict."""
        if self.api_backendtype == 'method':
            return {
                self.name: MethodInfoProperty(self.name),
                }
        else:
            if not self.enum:
                return {self.name: ProxyProperty(self.name), }
            else:
                return {self.name: EnumInfoProperty(self.name, self.enum), }

    @property
    def js_fields(self):
        """Return list of JavaScript fields."""
        if self.grid_display is False:
            return []
        else:
            return ["{{name: '{}'}}".format(self.name)]

    @property
    def js_columns_width(self):
        """Return integer pixel width of JavaScript columns."""
        if self.grid_display:
            return max(self.content_width + 14, self.label_width + 20)
        else:
            return 0

    @property
    def js_columns(self):
        """Return list of JavaScript columns."""

        if self.grid_display is False:
            return []

        column_fields = [
            "id: '{}'".format(self.name),
            "dataIndex: '{}'".format(self.name),
            "header: _t('{}')".format(self.short_label),
            "width: {}".format(self.js_columns_width),
            ]

        if self.renderer:
            column_fields.append("renderer: {}".format(self.renderer))

        return [
            OrderAndValue(
                order=self.order,
                value='{{{}}}'.format(','.join(column_fields))),
            ]


class RelationshipSchemaSpec(Spec):
    """TODO."""

    def __init__(
        self,
        zenpack_spec=None,
        left_class=None,
        left_relname=None,
        left_type=None,
        right_type=None,
        right_class=None,
        right_relname=None
    ):
        """
            Create a Relationship Schema specification.  This describes both sides
            of a relationship (left and right).

            :param left_class: TODO
            :type left_class: class
            :param left_relname: TODO
            :type left_relname: str
            :param left_type: TODO
            :type left_type: reltype
            :param right_type: TODO
            :type right_type: reltype
            :param right_class: TODO
            :type right_class: class
            :param right_relname: TODO
            :type right_relname: str

        """

        if not RelationshipSchemaSpec.valid_orientation(left_type, right_type):
            raise ZenSchemaError("In %s(%s) - (%s)%s, invalid orientation- left and right may be reversed." % (left_class, left_relname, right_relname, right_class))

        self.zenpack_spec = zenpack_spec
        self.left_class = left_class
        self.left_relname = left_relname
        self.left_schema = self.make_schema(left_type, right_type, right_class, right_relname)
        self.right_class = right_class
        self.right_relname = right_relname
        self.right_schema = self.make_schema(right_type, left_type, left_class, left_relname)

    @classmethod
    def valid_orientation(cls, left_type, right_type):
        # The objects in a relationship are always ordered left to right
        # so that they can be easily compared and consistently represented.
        #
        # The valid combinations are:

        # 1:1 - One To One
        if right_type == 'ToOne' and left_type == 'ToOne':
            return True

        # 1:M - One To Many
        if right_type == 'ToOne' and left_type == 'ToMany':
            return True

        # 1:MC - One To Many (Containing)
        if right_type == 'ToOne' and left_type == 'ToManyCont':
            return True

        # M:M - Many To Many
        if right_type == 'ToMany' and left_type == 'ToMany':
            return True

        return False

    _relTypeCardinality = {
        ToOne: '1',
        ToMany: 'M',
        ToManyCont: 'MC'
    }

    _relTypeClasses = {
        "ToOne": ToOne,
        "ToMany": ToMany,
        "ToManyCont": ToManyCont
    }

    _relTypeNames = {
        ToOne: "ToOne",
        ToMany: "ToMany",
        ToManyCont: "ToManyCont"
    }

    @property
    def left_type(self):
        return self._relTypeNames.get(self.right_schema.__class__)

    @property
    def right_type(self):
        return self._relTypeNames.get(self.left_schema.__class__)

    @property
    def left_cardinality(self):
        return self._relTypeCardinality.get(self.right_schema.__class__)

    @property
    def right_cardinality(self):
        return self._relTypeCardinality.get(self.left_schema.__class__)

    @property
    def default_left_relname(self):
        return relname_from_classname(self.right_class, plural=self.right_cardinality != '1')

    @property
    def default_right_relname(self):
        return relname_from_classname(self.left_class, plural=self.left_cardinality != '1')

    @property
    def cardinality(self):
        return '%s:%s' % (self.left_cardinality, self.right_cardinality)

    def make_schema(self, relTypeName, remoteRelTypeName, remoteClass, remoteName):
        relType = self._relTypeClasses.get(relTypeName, None)
        if not relType:
            raise ValueError("Unrecognized Relationship Type '%s'" % relTypeName)

        remoteRelType = self._relTypeClasses.get(remoteRelTypeName, None)
        if not remoteRelType:
            raise ValueError("Unrecognized Relationship Type '%s'" % remoteRelTypeName)

        schema = relType(remoteRelType, remoteClass, remoteName)

        # Qualify unqualified classnames.
        if '.' not in schema.remoteClass:
            schema.remoteClass = '{}.{}'.format(
                self.zenpack_spec.name, schema.remoteClass)

        return schema


class ClassRelationshipSpec(Spec):

    """TODO."""

    def __init__(
            self,
            class_,
            name,
            schema=None,
            label=None,
            short_label=None,
            label_width=None,
            content_width=None,
            display=True,
            details_display=True,
            grid_display=True,
            renderer=None,
            render_with_type=False,
            order=None,
            ):
        """
        Create a Class Relationship Specification

            :param label: Label to use when describing this relationship in the
                   UI.  If not specified, the default is to use the name of the
                   relationship's target class.
            :type label: str
            :param short_label: If specified, this is a shorter version of the
                   label, used, for example, in grid table headings.
            :type short_label: str
            :param label_width: Optionally overrides ZPL's label width
                   calculation with a higher value.
            :type label_width: int
            :param content_width:  Optionally overrides ZPL's content width
                   calculation with a higher value.
            :type content_width: int
            :param display: If this is set to False, this relationship will be
                   hidden from the UI completely.
            :type display: bool
            :param details_display: If this is set to False, this relationship
                   will be hidden from the "details" portion of the UI.
            :type details_display: bool
            :param grid_display:  If this is set to False, this relationship
                   will be hidden from the "grid" portion of the UI.
            :type grid_display: bool
            :param renderer: The default javascript renderer for a relationship
                   provides a link with the title of the target object,
                   optionally with the object's type (if render_with_type is
                   set).  If something more specific is required, a javascript
                   renderer function name may be provided.
            :type renderer: str
            :param render_with_type: Indicates that when an object is linked to,
                   it should be shown along with its type.  This is particularly
                   useful when the relationship's target is a base class that
                   may have several subclasses, such that the base class +
                   target object is not sufficiently descriptive on its own.
            :type render_with_type: bool
            :param order: TODO
            :type order: float
        """

        self.class_ = class_
        self.name = name
        self.schema = schema
        self.label = label
        self.short_label = short_label
        self.label_width = label_width
        self.content_width = content_width
        self.display = display
        self.details_display = details_display
        self.grid_display = grid_display
        self.renderer = renderer
        self.render_with_type = render_with_type
        self.order = order

        if not self.display:
            self.details_display = False
            self.grid_display = False

        if self.renderer is None:
            self.renderer = 'Zenoss.render.zenpacklib_entityTypeLinkFromGrid' \
                if self.render_with_type else 'Zenoss.render.zenpacklib_entityLinkFromGrid'

    @property
    def zenrelations_tuple(self):
        return (self.name, self.schema)

    @property
    def remote_classname(self):
        return self.schema.remoteClass.split('.')[-1]

    @property
    def iinfo_schemas(self):
        """Return IInfo attribute schema dict."""
        remote_spec = self.class_.zenpack.classes.get(self.remote_classname)
        imported_class = self.class_.zenpack.imported_classes.get(self.schema.remoteClass)
        if not (remote_spec or imported_class):
            return {}

        schemas = {}

        if not self.details_display:
            return {}

        if imported_class:
            remote_spec = imported_class
            remote_spec.label = remote_spec.meta_type

        if isinstance(self.schema, (ToOne)):
            schemas[self.name] = schema.Entity(
                title=_t(self.label or remote_spec.label),
                group="Relationships",
                order=self.order or 3.0)
        else:
            relname_count = '{}_count'.format(self.name)
            schemas[relname_count] = schema.Int(
                title=_t(u'Number of {}'.format(self.label or remote_spec.plural_label)),
                group="Relationships",
                order=self.order or 6.0)

        return schemas

    @property
    def info_properties(self):
        """Return Info properties dict."""
        properties = {}

        if not isinstance(self.schema, (ToOne)):
            relname_count = '{}_count'.format(self.name)
            properties[relname_count] = RelationshipLengthProperty(self.name)

        properties[self.name] = RelationshipInfoProperty(self.name)

        return properties

    @property
    def js_fields(self):
        """Return list of JavaScript fields."""
        remote_spec = self.class_.zenpack.classes.get(self.remote_classname)

        # do not show if grid turned off
        if self.grid_display is False:
            return []

        # No reason to show a column for the device since we're already
        # looking at the device.
        if not remote_spec or remote_spec.is_device:
            return []

        # Don't include containing relationships. They're handled by
        # the class.
        if issubclass(self.schema.remoteType, ToManyCont):
            return []

        if isinstance(self.schema, ToOne):
            fieldname = self.name
        else:
            fieldname = '{}_count'.format(self.name)

        return ["{{name: '{}'}}".format(fieldname)]

    @property
    def js_columns_width(self):
        """Return integer pixel width of JavaScript columns."""
        if not self.grid_display:
            return 0

        remote_spec = self.class_.zenpack.classes.get(self.remote_classname)

        # No reason to show a column for the device since we're already
        # looking at the device.
        if not remote_spec or remote_spec.is_device:
            return 0

        if isinstance(self.schema, ToOne):
            return max(
                (self.content_width or remote_spec.content_width) + 14,
                (self.label_width or remote_spec.label_width) + 20)
        else:
            return (self.label_width or remote_spec.plural_label_width) + 20

    @property
    def js_columns(self):
        """Return list of JavaScript columns."""
        if not self.grid_display:
            return []

        remote_spec = self.class_.zenpack.classes.get(self.remote_classname)

        # No reason to show a column for the device since we're already
        # looking at the device.
        if not remote_spec or remote_spec.is_device:
            return []

        # Don't include containing relationships. They're handled by
        # the class.
        if issubclass(self.schema.remoteType, ToManyCont):
            return []

        if isinstance(self.schema, ToOne):
            fieldname = self.name
            header = self.short_label or self.label or remote_spec.short_label
            renderer = self.renderer
        else:
            fieldname = '{}_count'.format(self.name)
            header = self.short_label or self.label or remote_spec.plural_short_label
            renderer = None

        column_fields = [
            "id: '{}'".format(fieldname),
            "dataIndex: '{}'".format(fieldname),
            "header: _t('{}')".format(header),
            "width: {}".format(self.js_columns_width),
            ]

        if renderer:
            column_fields.append("renderer: {}".format(renderer))

        return [
            OrderAndValue(
                order=self.order or remote_spec.order,
                value='{{{}}}'.format(','.join(column_fields))),
            ]


class RRDTemplateSpec(Spec):

    """TODO."""

    def __init__(
            self,
            deviceclass_spec,
            name,
            description=None,
            targetPythonClass=None,
            thresholds=None,
            datasources=None,
            graphs=None
            ):
        """
        Create an RRDTemplate Specification


            :param description: TODO
            :type description: str
            :param targetPythonClass: TODO
            :type targetPythonClass: str
            :param thresholds: TODO
            :type thresholds: SpecsParameter(RRDThresholdSpec)
            :param datasources: TODO
            :type datasources: SpecsParameter(RRDDatasourceSpec)
            :param graphs: TODO
            :type graphs: SpecsParameter(GraphDefinitionSpec)

        """

        self.deviceclass_spec = deviceclass_spec
        self.name = name
        self.targetPythonClass = targetPythonClass

        self.thresholds = self.specs_from_param(
            RRDThresholdSpec, 'thresholds', thresholds)

        self.datasources = self.specs_from_param(
            RRDDatasourceSpec, 'datasources', datasources)

        self.graphs = self.specs_from_param(
            GraphDefinitionSpec, 'graphs', graphs)


class RRDThresholdSpec(Spec):

    """TODO."""

    def __init__(
            self,
            template_spec,
            dsnames=None,
            minval=None,
            maxval=None,
            eventClass=None,
            severity=None,
            escalateCount=None,
            enabled=None
            ):
        """
        Create an RRDTemplate Specification

            :param dsnames: TODO
            :type dsnames: list(str)
            :param minval: TODO
            :type minval: str
            :param maxval: TODO
            :type maxval: str
            :param eventClass: TODO
            :type eventClass: str
            :param severity: TODO
            :type severity: int
            :param escalateCount: TODO
            :type escalateCount: int
            :param enabled: TODO
            :type enabled: bool

        """

        self.template_spec = template_spec
        self.dsnames = dsnames
        self.minval = minval
        self.maxval = maxval
        self.eventClass = eventClass
        self.severity = severity
        self.escalateCount = escalateCount
        self.enabled = enabled


class RRDDatasourceSpec(Spec):

    """TODO."""

    def __init__(
            self,
            template_spec,
            name,
            sourcetype=None,
            enabled=None,
            component=None,
            eventClass=None,
            eventKey=None,
            severity=None,
            commandTemplate=None,
            cycletime=None,
            datapoints=None
            ):
        """
        Create an RRDDatasource Specification

            :param sourcetype: TODO
            :type sourcetype: str
            :yaml_param sourcetype: type
            :param enabled: TODO
            :type enabled: bool
            :param component: TODO
            :type component: str
            :param eventClass: TODO
            :type eventClass: str
            :param eventKey: TODO
            :type eventKey: str
            :param severity: TODO
            :type severity: int
            :param commandTemplate: TODO
            :type commandTemplate: str
            :param cycletime: TODO
            :type cycletime: int
            :param datapoints: TODO
            :type datapoints: SpecsParameter(RRDDatapointSpec)
        """
        self.template_spec = template_spec
        self.name = name
        self.sourcetype = sourcetype
        self.enabled = enabled
        self.component = component
        self.eventClass = eventClass
        self.eventKey = eventKey
        self.severity = severity
        self.commandTemplate = commandTemplate
        self.cycletime = cycletime


class RRDDatapointSpec(Spec):

    """TODO."""

    def __init__(
            self,
            datasource_spec,
            rrdtype=None,
            createCmd=None,
            isrow=None,
            rrdmin=None,
            rrdmax=None,
            description=None
            ):
        """
        Create an RRDDatapoint Specification

        :param rrdtype: TODO
        :type rrdtype: RRDType
        :param createCmd: TODO
        :type createCmd: str
        :param isrow: TODO
        :type isrow: bool
        :param rrdmin: TODO
        :type rrdmin: str
        :param rrdmax: TODO
        :type rrdmax: str
        :param description: TODO
        :type description: str

        """
        self.datasource_spec = datasource_spec
        self.rrdtype = rrdtype
        self.createCmd = createCmd
        self.isrow = isrow
        self.rrdmin = rrdmin
        self.rrdmax = rrdmax
        self.description = description


class GraphDefinitionSpec(Spec):
    """TODO."""

    def __init__(
            self,
            template_spec,
            height=None,
            width=None,
            units=None,
            log=None,
            base=None,
            miny=None,
            maxy=None,
            custom=None,
            hasSummary=None,
            sequence=None,
            graphpoints=None,
            comments=None,
            ):
        """
        Create a GraphDefinition Specification

        :param height TODO
        :type height: int
        :param width TODO
        :type width: int
        :param units TODO
        :type units: str
        :param log TODO
        :type log: bool
        :param base TODO
        :type base: bool
        :param miny TODO
        :type miny: int
        :param maxy TODO
        :type maxy: int
        :param custom: TODO
        :type custom: str
        :param hasSummary: TODO
        :type hasSummary: bool
        :param sequence TODO
        :type sequence: long
        :param graphpoints: TODO
        :type graphpoints: SpecsParameter(GraphPointSpec)
        :param comments: TODO
        :type comments: list(str)
        """

        self.template_spec = template_spec

        self.height = height
        self.width = width
        self.units = units
        self.log = log
        self.base = base
        self.miny = miny
        self.maxy = maxy
        self.custom = custom
        self.hasSummary = hasSummary
        self.sequence = sequence
        self.graphpoints = graphpoints
        self.graphpoints = self.specs_from_param(
            GraphPointSpec, 'graphpoints', graphpoints)
        self.comments = comments


class GraphPointSpec(Spec):
    """TODO."""

    def __init__(
            self,
            template_spec,
            name=None,
            dpName=None,
            lineType=None,
            lineWidth=None,
            stacked=None,
            format=None,
            legend=None,
            limit=None,
            rpn=None,
            cFunc=None,
            colorindex=None,
            color=None,
            includeThresholds=False
            ):
        """
        Create a GraphPoint Specification

            :param dpName: TODO
            :type dpName: str
            :param lineType: TODO
            :type lineType: LineType
            :param lineWidth: TODO
            :type lineWidth: long
            :param stacked: TODO
            :type stacked: bool
            :param format: TODO
            :type format: str
            :param legend: TODO
            :type legend: str
            :param limit: TODO
            :type limit: long
            :param rpn: TODO
            :type rpn: str
            :param cFunc: TODO
            :type cFunc: str
            :param color: TODO
            :type color: str
            :param colorindex: TODO
            :type colorindex: int
            :param includeThresholds: TODO
            :type includeThresholds: bool

        """

        self.template_spec = template_spec
        self.name = name
        self.dpName = dpName
        self.lineType = lineType
        self.lineWidth = lineWidth
        self.stacked = stacked
        self.format = format
        self.legend = legend
        self.limit = limit
        self.rpn = rpn
        self.cFunc = cFunc
        self.colorindex = colorindex
        self.color = color
        self.includeThresholds = includeThresholds


# YAML Import/Export ########################################################

if YAML_INSTALLED:
    from yaml import Dumper, Loader

    def relschemaspec_to_str(spec):
        # Omit relation names that are their defaults.
        left_optrelname = "" if spec.left_relname == spec.default_left_relname else "(%s)" % spec.left_relname
        right_optrelname = "" if spec.right_relname == spec.default_right_relname else "(%s)" % spec.right_relname

        return "%s%s %s:%s %s%s" % (
            spec.left_class,
            left_optrelname,
            spec.left_cardinality,
            spec.right_cardinality,
            right_optrelname,
            spec.right_class
        )

    def str_to_relschemaspec(schemastr):
        schema_pattern = re.compile(
            r'^\s*(?P<left>\S+)'
            r'\s+(?P<cardinality>1:1|1:M|1:MC|M:M)'
            r'\s+(?P<right>\S+)\s*$',
        )

        class_rel_pattern = re.compile(
            r'(\((?P<pre_relname>[^\)\s]+)\))?'
            r'(?P<class>[^\(\s]+)'
            r'(\((?P<post_relname>[^\)\s]+)\))?'
        )

        m = schema_pattern.search(schemastr)
        if not m:
            raise ValueError("RelationshipSchemaSpec '%s' is not valid" % schemastr)

        ml = class_rel_pattern.search(m.group('left'))
        if not ml:
            raise ValueError("RelationshipSchemaSpec '%s' left side is not valid" % m.group('left'))

        mr = class_rel_pattern.search(m.group('right'))
        if not mr:
            raise ValueError("RelationshipSchemaSpec '%s' right side is not valid" % m.group('right'))

        reltypes = {
            '1:1': ('ToOne', 'ToOne'),
            '1:M': ('ToMany', 'ToOne'),
            '1:MC': ('ToManyCont', 'ToOne'),
            'M:M': ('ToMany', 'ToMany')
        }

        left_class = ml.group('class')
        right_class = mr.group('class')
        left_type = reltypes.get(m.group('cardinality'))[0]
        right_type = reltypes.get(m.group('cardinality'))[1]

        left_relname = ml.group('pre_relname') or ml.group('post_relname')
        if left_relname is None:
            left_relname = relname_from_classname(right_class, plural=left_type != 'ToOne')

        right_relname = mr.group('pre_relname') or mr.group('post_relname')
        if right_relname is None:
            right_relname = relname_from_classname(left_class, plural=right_type != 'ToOne')

        return dict(
            left_class=left_class,
            left_relname=left_relname,
            left_type=left_type,
            right_type=right_type,
            right_class=right_class,
            right_relname=right_relname
        )

    def class_to_str(class_):
        return class_.__module__ + "." + class_.__name__

    def str_to_class(classstr):
        if "." not in classstr:
            # TODO: Support non qualfied class names, searching zenpack, zenpacklib,
            # and ZenModel namespaces

            # An unqualified class name is assumed to be referring to one in
            # the classes defined in this ZenPackSpec.   We can't validate this,
            # or return a class object for it, if this is the case.  So we
            # return no class object, and the caller will assume that it
            # it referrs to a class being defined.
            return None

        modname, classname = classstr.rsplit(".", 1)

        try:
            class_ = getattr(importlib.import_module(modname), classname)
        except Exception, e:
            raise ValueError("Class '%s' is not valid: %s" % (classstr, e))

        return class_

    def severity_to_str(value):
        '''
        Return string representation for severity given a numeric value.
        '''
        try:
            severity = int(value)
        except (TypeError, ValueError):
            severity = {
                5: 'crit',
                4: 'err',
                3: 'warn',
                2: 'info',
                1: 'debug',
                0: 'clear'
                }.get(value.lower())

        if severity is None:
            raise ValueError("'%s' is not a valid value for severity.", value)

        return severity

    def str_to_severity(value):
        '''
        Return numeric severity given a string representation of severity.
        '''
        try:
            severity = int(value)
        except (TypeError, ValueError):
            severity = {
                'crit': 5, 'critical': 5,
                'err': 4, 'error': 4,
                'warn': 3, 'warning': 3,
                'info': 2, 'information': 2, 'informational': 2,
                'debug': 1, 'debugging': 1,
                'clear': 0,
                }.get(value.lower())

        if severity is None:
            raise ValueError("'%s' is not a valid value for severity." % value)

        return severity

    def yaml_error(loader, e):
        # Given a MarkedYAMLError exception, either log or raise
        # the error, depending on the 'fatal' argument.
        fatal = not getattr(loader, 'warnings', False)
        setattr(loader, 'yaml_errored', True)

        if fatal:
            raise e

        message = []

        mark = e.context_mark or e.problem_mark
        if mark:
            position = "%s:%s:%s" % (mark.name, mark.line+1, mark.column+1)
        else:
            position = "[unknown]"
        if e.context is not None:
            message.append(e.context)

        if e.problem is not None:
            message.append(e.problem)

        if e.note is not None:
            message.append("(note: " + e.note + ")")

        print "%s: %s" % (position, ",".join(message))

    def construct_specsparameters(loader, node, spectype):
        spec_class = {x.__name__: x for x in Spec.__subclasses__()}.get(spectype, None)

        if not spec_class:
            yaml_error(loader, yaml.constructor.ConstructorError(
                None, None,
                "Unrecognized Spec class %s" % spectype,
                node.start_mark))
            return

        if not isinstance(node, yaml.MappingNode):
            yaml_error(loader, yaml.constructor.ConstructorError(
                None, None,
                "expected a mapping node, but found %s" % node.id,
                node.start_mark))
            return

        specs = OrderedDict()
        for spec_key_node, spec_value_node in node.value:
            try:
                spec_key = str(loader.construct_scalar(spec_key_node))
            except yaml.MarkedYAMLError, e:
                yaml_error(loader, e)

            specs[spec_key] = construct_spec(spec_class, loader, spec_value_node)

        return specs

    def represent_relschemaspec(dumper, data):
        return dumper.represent_str(relschemaspec_to_str(data))

    def construct_relschemaspec(loader, node):
        schemastr = str(loader.construct_scalar(node))
        return str_to_relschemaspec(schemastr)

    def represent_spec(dumper, obj, yaml_tag=u'tag:yaml.org,2002:map', defaults=None):
        """
        Generic representer for serializing specs to YAML.  Rather than using
        the default PyYAML representer for python objects, we very carefully
        build up the YAML according to the parameter definitions in the __init__
        of each spec class.  This same format is used by construct_spec (the YAML
        constructor) to ensure that the spec objects are built consistently,
        whether it is done via YAML or the API.
        """

        mapping = {}
        cls = obj.__class__
        param_defs = cls.init_params()
        for param in param_defs:
            type_ = param_defs[param]['type']

            try:
                value = getattr(obj, param)
            except AttributeError:
                raise yaml.representer.RepresenterError(
                    "Unable to serialize %s object: %s, a supported parameter, is not accessible as a property." %
                    (cls.__name__, param))
                continue

            # Figure out what the default value is.  First, consider the default
            # value for this parameter (globally):
            default_value = param_defs[param].get('default', None)

            # Now, we need to handle 'DEFAULTS'.  If we're in a situation
            # where that is supported, and we're outputting a spec that
            # would be affected by it (not DEFAULTS itself, in other words),
            # then we look at the default value for this parameter, in case
            # it has changed the global default for this parameter.
            if hasattr(obj, 'name') and obj.name != 'DEFAULTS' and defaults is not None:
                default_value = getattr(defaults, param, default_value)

            if value == default_value:
                # If the value is a default value, we can omit it from the export.
                continue

            # If the value is null and the type is a list or dictionary, we can
            # assume it was some optional nested data and omit it.
            if value is None and (
               type_.startswith('dict') or
               type_.startswith('list') or
               type_.startswith('SpecsParameter')):
                continue

            if type_ == 'ZPropertyDefaultValue':
                # For zproperties, the actual data type of a default value
                # depends on the defined type of the zProperty.
                try:
                    type_ = {
                        'boolean': "bool",
                        'int': "int",
                        'float': "float",
                        'string': "str",
                        'password': "str",
                        'lines': "list(str)"
                    }.get(obj.type_, 'str')
                except KeyError:
                    type_ = "str"

            yaml_param = dumper.represent_str(param_defs[param]['yaml_param'])
            try:
                if type_ == "bool":
                    mapping[yaml_param] = dumper.represent_bool(value)
                elif type_.startswith("dict"):
                    mapping[yaml_param] = dumper.represent_dict(value)
                elif type_ == "float":
                    mapping[yaml_param] = dumper.represent_float(value)
                elif type_ == "int":
                    mapping[yaml_param] = dumper.represent_int(value)
                elif type_ == "list(class)":
                    # The "class" in this context is either a class reference or
                    # a class name (string) that refers to a class defined in
                    # this ZenPackSpec.
                    classes = [isinstance(x, type) and class_to_str(x) or x for x in value]
                    mapping[yaml_param] = dumper.represent_list(classes)
                elif type_.startswith("list"):
                    mapping[yaml_param] = dumper.represent_list(value)
                elif type_ == "str":
                    mapping[yaml_param] = dumper.represent_str(value)
                elif type_ == 'RelationshipSchemaSpec':
                    mapping[yaml_param] = dumper.represent_str(relschemaspec_to_str(value))
                elif type_ == 'Severity':
                    mapping[yaml_param] = dumper.represent_str(severity_to_str(value))
                else:
                    m = re.match('^SpecsParameter\((.*)\)$', type_)
                    if m:
                        spectype = m.group(1)
                        specmapping = OrderedDict()
                        keys = sorted(value)
                        defaults = None
                        if 'DEFAULTS' in keys:
                            keys.remove('DEFAULTS')
                            keys.insert(0, 'DEFAULTS')
                            defaults = value['DEFAULTS']
                        for key in keys:
                            spec = value[key]
                            if type(spec).__name__ != spectype:
                                raise yaml.representer.RepresenterError(
                                    "Unable to serialize %s object (%s):  Expected an object of type %s" %
                                    (type(spec).__name__, key, spectype))
                            else:
                                specmapping[dumper.represent_str(key)] = represent_spec(dumper, spec, defaults=defaults)

                        specmapping_value = []
                        node = yaml.MappingNode(yaml_tag, specmapping_value)
                        specmapping_value.extend(specmapping.items())
                        mapping[yaml_param] = node

                    else:
                        raise yaml.representer.RepresenterError(
                            "Unable to serialize %s object: %s, a supported parameter, is of an unrecognized type (%s)." %
                            (cls.__name__, param, type_))
            except yaml.representer.RepresenterError:
                raise
            except Exception, e:
                raise yaml.representer.RepresenterError(
                    "Unable to serialize %s object (param %s, type %s, value %s): %s" %
                    (cls.__name__, param, type_, value, e))

            if param_defs[param]['yaml_block_style']:
                mapping[yaml_param].flow_style = False

        mapping_value = []
        node = yaml.MappingNode(yaml_tag, mapping_value)
        mapping_value.extend(mapping.items())

        # Return a node describing the mapping (dictionary) of the params
        # used to build this spec.
        return node

    def construct_spec(cls, loader, node):
        """
        Generic constructor for deserializing specs from YAML.   Should be
        the opposite of represent_spec, and works in the same manner (with its
        parsing and validation directed by the init_params of each spec class)
        """

        if issubclass(cls, RRDDatapointSpec) and isinstance(node, yaml.ScalarNode):
            # Special case- we allow for a shorthand in specifying datapoint specs.
            return dict(shorthand=loader.construct_scalar(node))

        param_defs = cls.init_params()
        params = {}
        if not isinstance(node, yaml.MappingNode):
            yaml_error(loader, yaml.constructor.ConstructorError(
                None, None,
                "expected a mapping node, but found %s" % node.id,
                node.start_mark))

        # TODO: When deserializing, we should check if required properties are present.

        param_name_map = {}
        for param in param_defs:
            param_name_map[param_defs[param]['yaml_param']] = param

        for key_node, value_node in node.value:
            key = param_name_map[str(loader.construct_scalar(key_node))]

            if key not in param_defs:
                yaml_error(loader, yaml.constructor.ConstructorError(
                    None, None,
                    "Unrecognized parameter '%s' found while processing %s" % (key, cls.__name__),
                    key_node.start_mark))
                continue

            expected_type = param_defs[key]['type']

            if expected_type == 'ZPropertyDefaultValue':
                # For zproperties, the actual data type of a default value
                # depends on the defined type of the zProperty.

                try:
                    zPropType = [x[1].value for x in node.value if x[0].value == 'type'][0]
                except Exception:
                    # type was not specified, so we assume the default (string)
                    zPropType = 'string'

                try:
                    expected_type = {
                        'boolean': "bool",
                        'int': "int",
                        'float': "float",
                        'string': "str",
                        'password': "str",
                        'lines': "list(str)"
                    }.get(zPropType, 'str')
                except KeyError:
                    yaml_error(loader, yaml.constructor.ConstructorError(
                        None, None,
                        "Invalid zProperty type_ '%s' for property %s found while processing %s" % (zPropType, key, cls.__name__),
                        key_node.start_mark))
                    continue

            try:
                if expected_type == "bool":
                    params[key] = loader.construct_yaml_bool(value_node)
                elif expected_type.startswith("dict(SpecsParameter("):
                    m = re.match('^dict\(SpecsParameter\((.*)\)\)$', expected_type)
                    if m:
                        spectype = m.group(1)

                        if not isinstance(node, yaml.MappingNode):
                            yaml_error(loader, yaml.constructor.ConstructorError(
                                None, None,
                                "expected a mapping node, but found %s" % node.id,
                                node.start_mark))
                            continue
                        specs = OrderedDict()
                        for spec_key_node, spec_value_node in value_node.value:
                            spec_key = str(loader.construct_scalar(spec_key_node))

                            specs[spec_key] = construct_specsparameters(loader, spec_value_node, spectype)
                        params[key] = specs
                    else:
                        raise Exception("Unable to determine specs parameter type in '%s'" % expected_type)
                elif expected_type.startswith("dict"):
                    params[key] = loader.construct_mapping(value_node)
                elif expected_type == "float":
                    params[key] = float(loader.construct_scalar(value_node))
                elif expected_type == "int":
                    params[key] = int(loader.construct_scalar(value_node))
                elif expected_type == "list(class)":
                    classnames = loader.construct_sequence(value_node)
                    classes = []
                    for c in classnames:
                        class_ = str_to_class(c)
                        if class_ is None:
                            # local reference to a class being defined in
                            # this zenpack.  (ideally we should verify that
                            # the name is valid, but this is not possible
                            # in a one-pass parsing of the yaml).
                            classes.append(c)
                        else:
                            classes.append(class_)
                    # ZPL defines "class" as either a string representing a
                    # class in this definition, or a class object representing
                    # an external class.
                    params[key] = classes
                elif expected_type == "list(RelationshipSchemaSpec)":
                    schemaspecs = []
                    for s in loader.construct_sequence(value_node):
                        schemaspecs.append(str_to_relschemaspec(s))
                    params[key] = schemaspecs
                elif expected_type.startswith("list"):
                    params[key] = loader.construct_sequence(value_node)
                elif expected_type == "str":
                    params[key] = str(loader.construct_scalar(value_node))
                elif expected_type == 'RelationshipSchemaSpec':
                    schemastr = str(loader.construct_scalar(value_node))
                    params[key] = str_to_relschemaspec(schemastr)
                elif expected_type == 'Severity':
                    severitystr = str(loader.construct_scalar(value_node))
                    params[key] = str_to_severity(severitystr)
                else:
                    m = re.match('^SpecsParameter\((.*)\)$', expected_type)
                    if m:
                        spectype = m.group(1)
                        params[key] = construct_specsparameters(loader, value_node, spectype)
                    else:
                        raise Exception("Unhandled type '%s'" % expected_type)

            except yaml.constructor.ConstructorError, e:
                yaml_error(loader, e)
            except Exception, e:
                yaml_error(loader, yaml.constructor.ConstructorError(
                    None, None,
                    "Unable to deserialize %s object (param %s): %s" % (cls.__name__, key_node.value, e),
                    value_node.start_mark))

        return params

    def represent_zenpackspec(dumper, obj):
        return represent_spec(dumper, obj, yaml_tag=u'!ZenPackSpec')

    def construct_zenpackspec(loader, node):
        params = construct_spec(ZenPackSpec, loader, node)
        name = params.pop("name")

        fatal = not getattr(loader, 'warnings', False)
        yaml_errored = getattr(loader, 'yaml_errored', False)

        try:
            return ZenPackSpec(name, **params)
        except Exception, e:
            if yaml_errored and not fatal:
                LOG.error("(possibly because of earlier errors) %s" % e)
            else:
                raise

        return None

    class WarningLoader(yaml.Loader):
        warnings = True
        yaml_errored = False

    Dumper.add_representer(ZenPackSpec, represent_zenpackspec)
    Dumper.add_representer(DeviceClassSpec, represent_spec)
    Dumper.add_representer(ZPropertySpec, represent_spec)
    Dumper.add_representer(ClassSpec, represent_spec)
    Dumper.add_representer(ClassPropertySpec, represent_spec)
    Dumper.add_representer(ClassRelationshipSpec, represent_spec)
    Dumper.add_representer(RelationshipSchemaSpec, represent_relschemaspec)
    Loader.add_constructor(u'!ZenPackSpec', construct_zenpackspec)

    class SpecParams(object):
        def __init__(self, **kwargs):
            # Initialize with default values
            params = self.__class__.init_params()
            for param in params:
                if 'default' in params[param]:
                    setattr(self, param, params[param]['default'])

            # Overlay any named parameters
            self.__dict__.update(kwargs)

        @classmethod
        def init_params(cls):
            # Pull over the params for the underlying Spec class,
            # correcting nested Specs to SpecsParams instead.
            try:
                spec_base = [x for x in cls.__bases__ if issubclass(x, Spec)][0]
            except Exception:
                raise Exception("Spec Base Not Found for %s" % cls.__name__)

            params = spec_base.init_params()
            for p in params:
                params[p]['type'] = params[p]['type'].replace("Spec)", "SpecParams)")

            return params


    class ZenPackSpecParams(SpecParams, ZenPackSpec):
        def __init__(self, name, zProperties=None, class_relationships=None, classes=None, device_classes=None, **kwargs):
            SpecParams.__init__(self, **kwargs)
            self.name = name

            self.zProperties = self.specs_from_param(
                ZPropertySpecParams, 'zProperties', zProperties, leave_defaults=True)

            self.class_relationships = []
            if class_relationships:
                if not isinstance(class_relationships, list):
                    raise ValueError("class_relationships must be a list, not a %s" % type(class_relationships))

                for rel in class_relationships:
                    self.class_relationships.append(RelationshipSchemaSpec(self, **rel))

            self.classes = self.specs_from_param(
                ClassSpecParams, 'classes', classes, leave_defaults=True)

            self.device_classes = self.specs_from_param(
                DeviceClassSpecParams, 'device_classes', device_classes, leave_defaults=True)

    class DeviceClassSpecParams(SpecParams, DeviceClassSpec):
        def __init__(self, zenpack_spec, path, zProperties=None, **kwargs):
            SpecParams.__init__(self, **kwargs)
            self.path = path
            self.zProperties = zProperties

    class ZPropertySpecParams(SpecParams, ZPropertySpec):
        def __init__(self, zenpack_spec, name, **kwargs):
            SpecParams.__init__(self, **kwargs)
            self.name = name

    class ClassSpecParams(SpecParams, ClassSpec):
        def __init__(self, zenpack_spec, name, base=None, properties=None, relationships=None, monitoring_templates=[], **kwargs):
            SpecParams.__init__(self, **kwargs)
            self.name = name

            if isinstance(base, (tuple, list, set)):
                self.base = tuple(base)
            else:
                self.base = (base,)

            if isinstance(monitoring_templates, (tuple, list, set)):
                self.monitoring_templates = list(monitoring_templates)
            else:
                self.monitoring_templates = [monitoring_templates]

            self.properties = self.specs_from_param(
                ClassPropertySpecParams, 'properties', properties, leave_defaults=True)

            self.relationships = self.specs_from_param(
                ClassRelationshipSpecParams, 'relationships', relationships, leave_defaults=True)

    class ClassPropertySpecParams(SpecParams, ClassPropertySpec):
        def __init__(self, class_spec, name, **kwargs):
            SpecParams.__init__(self, **kwargs)
            self.name = name

    class ClassRelationshipSpecParams(SpecParams, ClassRelationshipSpec):
        def __init__(self, class_spec, name, **kwargs):
            SpecParams.__init__(self, **kwargs)
            self.name = name

    Dumper.add_representer(ZenPackSpecParams, represent_zenpackspec)
    Dumper.add_representer(DeviceClassSpecParams, represent_spec)
    Dumper.add_representer(ZPropertySpecParams, represent_spec)
    Dumper.add_representer(ClassSpecParams, represent_spec)
    Dumper.add_representer(ClassPropertySpecParams, represent_spec)
    Dumper.add_representer(ClassRelationshipSpecParams, represent_spec)


# Public Functions ##########################################################

def enableTesting():
    """Enable test mode. Only call from code under tests/.

    If this is called from production code it will cause all Zope
    clients to start in test mode. Which isn't useful for anything but
    unit testing.

    """
    global TestCase

    if TestCase:
        return

    from Products.ZenTestCase.BaseTestCase import BaseTestCase
    from transaction._transaction import Transaction

    class TestCase(BaseTestCase):

        # As in BaseTestCase, the default behavior is to disable
        # all logging from within a unit test.  To enable it,
        # set disableLogging = False in your subclass.  This is
        # recommended during active development, but is too noisy
        # to leave as the default.
        disableLogging = True

        def afterSetUp(self):
            super(TestCase, self).afterSetUp()

            # Not included with BaseTestCase. Needed to test that UI
            # components have been properly registered.
            from Products.Five import zcml
            import Products.ZenUI3
            zcml.load_config('configure.zcml', Products.ZenUI3)

            zenpack_module_name = '.'.join(self.__module__.split('.')[:-2])
            zenpack_module = importlib.import_module(zenpack_module_name)

            zenpackspec = getattr(zenpack_module, 'CFG', None)
            if not zenpackspec:
                raise NameError(
                    "name {!r} is not defined"
                    .format('.'.join((zenpack_module_name, 'CFG'))))

            zenpackspec.test_setup()

            import Products.ZenEvents
            zcml.load_config('meta.zcml', Products.ZenEvents)

            try:
                import ZenPacks.zenoss.DynamicView
                zcml.load_config('configure.zcml', ZenPacks.zenoss.DynamicView)
            except ImportError:
                return

            try:
                import ZenPacks.zenoss.Impact
                zcml.load_config('meta.zcml', ZenPacks.zenoss.Impact)
                zcml.load_config('configure.zcml', ZenPacks.zenoss.Impact)
            except ImportError:
                return

            # BaseTestCast.afterSetUp already hides transaction.commit. So we also
            # need to hide transaction.abort.
            self._transaction_abort = Transaction.abort
            Transaction.abort = lambda *x: None

        def beforeTearDown(self):
            super(TestCase, self).beforeTearDown()

            if hasattr(self, '_transaction_abort'):
                Transaction.abort = self._transaction_abort

        # If the exception occurs during setUp, beforeTearDown is not called,
        # so we also need to restore abort here as well:
        def _close(self):
            if hasattr(self, '_transaction_abort'):
                Transaction.abort = self._transaction_abort

            super(TestCase, self)._close()


def ucfirst(text):
    """Return text with the first letter uppercased.

    This differs from str.capitalize and str.title methods in that it
    doesn't lowercase the remainder of text.

    """
    return text[0].upper() + text[1:]


def relname_from_classname(classname, plural=False):
    """Return relationship name given classname and plural flag."""

    if '.' in classname:
        classname = classname.replace('.', '_').lower()

    relname = list(classname)
    for i, c in enumerate(classname):
        if relname[i].isupper():
            relname[i] = relname[i].lower()
        else:
            break

    return ''.join((''.join(relname), 's' if plural else ''))


def relationships_from_yuml(yuml):
    """Return schema relationships definition given yuml text.

    The yuml text required is a subset of what is supported by yUML
    (http://yuml.me). See the following example:

        // Containing relationships.
        [APIC]++ -[FabricPod]
        [APIC]++ -[FvTenant]
        [FvTenant]++ -[VzBrCP]
        [FvTenant]++ -[FvAp]
        [FvAp]++ -[FvAEPg]
        [FvAEPg]++ -[FvRsProv]
        [FvAEPg]++ -[FvRsCons]
        // Non-containing relationships.
        [FvBD]1 -.- *[FvAEPg]
        [VzBrCP]1 -.- *[FvRsProv]
        [VzBrCP]1 -.- *[FvRsCons]

    The created relationships are given default names that orginarily
    should be used. However, in some cases such as when one class has
    multiple relationships to the same class, relationships must be
    explicitly named. That would be done as in the following example:

        // Explicitly-Named Relationships
        [Pool]*default_sr -.-default_for_pools 0..1[SR]
        [Pool]*suspend_image_sr -.-suspend_image_for_pools *[SR]
        [Pool]*crash_dump_sr -.-crash_dump_for_pools *[SR]

    The yuml parameter can be specified either as a newline-delimited
    string, or as a tuple or list of relationships.

    """
    classes = []
    match_comment = re.compile(r'^\s*//').search

    match_line = re.compile(
        r'\[(?P<left_classname>[^\]]+)\]'
        r'(?P<left_cardinality>[\.\*\+\d]*)'
        r'(?P<left_relname>[a-zA-Z_]*)'
        r'\s*?'
        r'(?P<relationship_separator>[\-\.]+)'
        r'(?P<right_relname>[a-zA-Z_]*)'
        r'\s*?'
        r'(?P<right_cardinality>[\.\*\+\d]*)'
        r'\[(?P<right_classname>[^\]]+)\]'
        ).search

    if isinstance(yuml, basestring):
        yuml_lines = yuml.strip().splitlines()

    for line in yuml_lines:
        if match_comment(line):
            continue

        match = match_line(line)
        if not match:
            LOG.error("parse error in relationships_from_yuml at %s" % line)
            continue

        left_class = match.group('left_classname')
        right_class = match.group('right_classname')
        left_relname = match.group('left_relname')
        left_cardinality = match.group('left_cardinality')
        right_relname = match.group('right_relname')
        right_cardinality = match.group('right_cardinality')

        if '++' in left_cardinality:
            left_type = 'ToManyCont'
        elif '*' in right_cardinality:
            left_type = 'ToMany'
        else:
            left_type = 'ToOne'

        if '++' in right_cardinality:
            right_type = 'ToManyCont'
        elif '*' in left_cardinality:
            right_type = 'ToMany'
        else:
            right_type = 'ToOne'

        if not left_relname:
            left_relname = relname_from_classname(
                right_class, plural=left_type != 'ToOne')

        if not right_relname:
            right_relname = relname_from_classname(
                left_class, plural=right_type != 'ToOne')

        # Order them correctly (larger one on the right)
        if RelationshipSchemaSpec.valid_orientation(left_type, right_type):
            classes.append(dict(
                left_class=left_class,
                left_relname=left_relname,
                left_type=left_type,
                right_type=right_type,
                right_class=right_class,
                right_relname=right_relname
            ))
        else:
            # flip them around
            classes.append(dict(
                left_class=right_class,
                left_relname=right_relname,
                left_type=right_type,
                right_type=left_type,
                right_class=left_class,
                right_relname=left_relname
            ))

    return classes


def MethodInfoProperty(method_name):
    """Return a property with the Infos for object(s) returned by a method.

    A list of Info objects is returned for methods returning a list, or a single
    one for those returning a single value.
    """
    def getter(self):
        try:
            return Zuul.info(getattr(self._object, method_name)())
        except TypeError:
            # If not callable avoid the traceback and send the property
            return Zuul.info(getattr(self._object, method_name))

    return property(getter)


def EnumInfoProperty(data, enum):
    """Return a property filtered via an enum."""
    def getter(self, data, enum):
        if not enum:
            return ProxyProperty(data)
        else:
            data = getattr(self._object, data, None)
            try:
                data = int(data)
                return Zuul.info(enum[data])
            except Exception:
                return Zuul.info(data)

    return property(lambda x: getter(x, data, enum))


def RelationshipInfoProperty(relationship_name):
    """Return a property with the Infos for object(s) in the relationship.

    A list of Info objects is returned for ToMany relationships, and a
    single Info object is returned for ToOne relationships.

    """
    def getter(self):
        return Zuul.info(getattr(self._object, relationship_name)())

    return property(getter)


def RelationshipLengthProperty(relationship_name):
    """Return a property representing number of objects in relationship."""
    def getter(self):
        relationship = getattr(self._object, relationship_name)
        try:
            return relationship.countObjects()
        except Exception:
            return len(relationship())

    return property(getter)


def RelationshipGetter(relationship_name):
    """Return getter for id or ids in relationship_name."""
    def getter(self):
        try:
            relationship = getattr(self, relationship_name)
            if isinstance(relationship, ToManyRelationship):
                return self.getIdsInRelationship(getattr(self, relationship_name))
            elif isinstance(relationship, ToOneRelationship):
                return self.getIdForRelationship(relationship)
        except Exception:
            LOG.error(
                "error getting %s ids for %s",
                relationship_name, self.getPrimaryUrlPath())
            raise

    return getter


def RelationshipSetter(relationship_name):
    """Return setter for id or ides in relationship_name."""
    def setter(self, id_or_ids):
        try:
            relationship = getattr(self, relationship_name)
            if isinstance(relationship, ToManyRelationship):
                self.setIdsInRelationship(relationship, id_or_ids)
            elif isinstance(relationship, ToOneRelationship):
                self.setIdForRelationship(relationship, id_or_ids)
        except Exception:
            LOG.error(
                "error setting %s ids for %s",
                relationship_name, self.getPrimaryUrlPath())
            raise

    return setter


# Private Types #############################################################

OrderAndValue = collections.namedtuple('OrderAndValue', ['order', 'value'])


# Private Functions #########################################################

def get_zenpack_path(zenpack_name):
    """Return filesystem path for given ZenPack."""
    zenpack_module = importlib.import_module(zenpack_name)
    return os.path.dirname(zenpack_module.__file__)


def ordered_values(iterable):
    """Return ordered list of values for iterable of OrderAndValue instances."""
    return [
        x.value for x in sorted(iterable, key=operator.attrgetter('order'))]


def pluralize(text):
    """Return pluralized version of text.

    Totally naive implementation currently. Could use a third party
    library if we knew it would be installed.
    """
    if text.endswith('s'):
        return '{}es'.format(text)

    return '{}s'.format(text)


def fix_kwargs(kwargs):
    """Return kwargs with reserved words suffixed with _."""
    new_kwargs = {}
    for k, v in kwargs.items():
        if k in ('class', 'type'):
            new_kwargs['{}_'.format(k)] = v
        else:
            new_kwargs[k] = v

    return new_kwargs


def update(d, u):
    """Return dict d updated with nested data from dict u."""
    for k, v in u.iteritems():
        if isinstance(v, collections.Mapping):
            r = update(d.get(k, {}), v)
            d[k] = r
        else:
            d[k] = u[k]
    return d


def catalog_search(scope, name, *args, **kwargs):
    """Return iterable of matching brains in named catalog."""
    catalog = getattr(scope, '{}Search'.format(name), None)
    if not catalog:
        return []

    if args:
        if isinstance(args[0], BaseQuery):
            return catalog.evalAdvancedQuery(args[0])
        elif isinstance(args[0], dict):
            return catalog(args[0])
        else:
            raise TypeError(
                "search() argument must be a BaseQuery or a dict, "
                "not {0!r}"
                .format(type(args[0]).__name__))

    return catalog(**kwargs)


def apply_defaults(dictionary, default_defaults=None, leave_defaults=False):
    """Modify dictionary to put values from DEFAULTS key into other keys.

    Unless leave_defaults is set to True, the DEFAULTS key will no longer exist
    in dictionary. dictionary must be a dictionary of dictionaries.

    Example usage:

        >>> mydict = {
        ...     'DEFAULTS': {'is_two': False},
        ...     'key1': {'number': 1},
        ...     'key2': {'number': 2, 'is_two': True},
        ... }
        >>> apply_defaults(mydict)
        >>> print mydict
        {
            'key1': {'number': 1, 'is_two': False},
            'key2': {'number': 2, 'is_two': True},
        }

    """
    if default_defaults:
        dictionary.setdefault('DEFAULTS', {})
        for default_key, default_value in default_defaults.iteritems():
            dictionary['DEFAULTS'].setdefault(default_key, default_value)

    if 'DEFAULTS' in dictionary:
        if leave_defaults:
            defaults = dictionary.get('DEFAULTS')
        else:
            defaults = dictionary.pop('DEFAULTS')
        for k, v in dictionary.iteritems():
            dictionary[k] = dict(defaults, **v)


def get_symbol_name(*args):
    """Return fully-qualified symbol name given path args.

    Example usage:

        >>> get_symbol_name('ZenPacks.example.Name')
        'ZenPacks.example.Name'

        >>> get_symbol_name('ZenPacks.example.Name', 'schema')
        'ZenPacks.example.Name.schema'

        >>> get_symbol_name('ZenPacks.example.Name', 'schema', 'APIC')
        'ZenPacks.example.Name.schema.APIC'

        >>> get_symbol_name('ZenPacks.example.Name', 'schema.Pool')
        'ZenPacks.example.Name.schema.Pool'

    No verification is done. Names for symbols that don't exist may
    be returned.

    """
    return '.'.join(x for x in args if x)


def create_module(*args):
    """Import and return module given path args.

    See get_symbol_name documentation for usage. May raise ImportError.

    """
    module_name = get_symbol_name(*args)
    try:
        return importlib.import_module(module_name)
    except ImportError:
        module = imp.new_module(module_name)
        module.__name__ = module_name
        sys.modules[module_name] = module

        module_parts = module_name.split('.')

        if len(module_parts) > 1:
            parent_module_name = get_symbol_name(*module_parts[:-1])
            parent_module = create_module(parent_module_name)
            setattr(parent_module, module_parts[-1], module)

    return importlib.import_module(module_name)


def get_class_factory(klass):
    """Return class factory for class."""
    if issubclass(klass, IInfo):
        return InterfaceClass
    else:
        return type


def create_schema_class(schema_module, classname, bases, attributes):
    """Create and return described schema class."""
    if isinstance(schema_module, basestring):
        schema_module = create_module(schema_module)

    schema_class = getattr(schema_module, classname, None)
    if schema_class:
        return schema_class

    class_factory = get_class_factory(bases[0])
    schema_class = class_factory(classname, tuple(bases), attributes)
    schema_class.__module__ = schema_module.__name__
    setattr(schema_module, classname, schema_class)

    return schema_class


def create_stub_class(module, schema_class, classname):
    """Create and return described stub class."""
    if isinstance(module, basestring):
        module = create_module(module)

    concrete_class = getattr(module, classname, None)
    if concrete_class:
        return concrete_class

    class_factory = get_class_factory(schema_class)
    stub_class = class_factory(classname, (schema_class,), {})
    stub_class.__module__ = module.__name__
    setattr(module, classname, stub_class)

    return stub_class


def create_class(module, schema_module, classname, bases, attributes):
    """Create and return described class."""
    if isinstance(module, basestring):
        module = create_module(module)

    schema_class = create_schema_class(
        schema_module, classname, bases, attributes)

    return create_stub_class(module, schema_class, classname)


# Impact Stuff ##############################################################

try:
    from ZenPacks.zenoss.Impact.impactd.relations import ImpactEdge, DSVRelationshipProvider, RelationshipEdgeError
    from ZenPacks.zenoss.Impact.impactd.interfaces import IRelationshipDataProvider
except ImportError:
    IMPACT_INSTALLED = False
else:
    IMPACT_INSTALLED = True

try:
    from ZenPacks.zenoss.DynamicView import BaseRelation, BaseGroup
    from ZenPacks.zenoss.DynamicView import TAG_IMPACTED_BY, TAG_IMPACTS, TAG_ALL
    from ZenPacks.zenoss.DynamicView.interfaces import IRelatable, IRelationsProvider, IGroup
    from ZenPacks.zenoss.DynamicView.dynamicview import DynamicViewMappings
    from ZenPacks.zenoss.DynamicView.model.adapters import BaseRelatable, BaseRelationsProvider

except ImportError:
    DYNAMICVIEW_INSTALLED = False
else:
    DYNAMICVIEW_INSTALLED = True

if IMPACT_INSTALLED:
    class ImpactRelationshipDataProvider(object):

        """Generic Impact RelationshipDataProvider adapter factory.

        Implements IRelationshipDataProvider.

        Creates impact relationships by introspecting the adapted object's
        impacted_by and impacts properties.

        """

        implements(IRelationshipDataProvider)
        adapts(DeviceBase, ComponentBase)

        def __init__(self, adapted):
            self.adapted = adapted

        @property
        def relationship_provider(self):
            """Return string indicating from where generated edges came.

            Required by IRelationshipDataProvider.

            """
            return getattr(self.adapted, 'zenpack_name', 'ZenPack')

        def belongsInImpactGraph(self):
            """Return True so generated edges will show in impact graph.

            Required by IRelationshipDataProvider.

            """
            return True

        def getEdges(self):
            """Generate ImpactEdge instances for adapted object.

            Required by IRelationshipDataProvider.

            """
            provider = self.relationship_provider
            myguid = IGlobalIdentifier(self.adapted).getGUID()
            impacted_by = getattr(self.adapted, 'impacted_by', [])
            if impacted_by:
                for methodname in impacted_by:
                    for impactor_guid in self.get_remote_guids(methodname):
                        yield ImpactEdge(impactor_guid, myguid, provider)

            impacts = getattr(self.adapted, 'impacts', [])
            if impacts:
                for methodname in impacts:
                    for impactee_guid in self.get_remote_guids(methodname):
                        yield ImpactEdge(myguid, impactee_guid, provider)

        def get_remote_guids(self, methodname):
            """Generate object GUIDs returned by adapted.methodname()."""
            method = getattr(self.adapted, methodname, None)
            if not method or not callable(method):
                LOG.warning(
                    "no %r relationship or method for %r",
                    methodname,
                    self.adapted.meta_type)

                return

            r = method()
            if not r:
                return

            try:
                for obj in r:
                    yield IGlobalIdentifier(obj).getGUID()

            except TypeError:
                yield IGlobalIdentifier(r).getGUID()

if DYNAMICVIEW_INSTALLED:
    class DynamicViewRelatable(BaseRelatable):
        """Generic DynamicView Relatable adapter (IRelatable)

        Places object into a group based upon the class name.
        """

        implements(IRelatable)
        adapts(DeviceBase, ComponentBase)

        @property
        def id(self):
            return self._adapted.getPrimaryId()

        @property
        def name(self):
            return self._adapted.titleOrId()

        @property
        def tags(self):
            return set([self._adapted.meta_type])

        @property
        def group(self):
            return self._adapted.class_dynamicview_group

    class DynamicViewRelationsProvider(BaseRelationsProvider):
        """Generic DynamicView RelationsProvider subscription adapter (IRelationsProvider)

        Creates impact relationships by introspecting the adapted object's
        impacted_by and impacts properties.

        Note that these impact relationships will also be exposed through to
        impact, so it is not necessary to activate both
        ImpactRelationshipDataProvider and DynamicViewRelatable /
        DynamicViewRelationsProvider for a given model class.
        """
        implements(IRelationsProvider)
        adapts(DeviceBase, ComponentBase)

        def relations(self, type=TAG_ALL):
            target = IRelatable(self._adapted)

            for tag in (TAG_ALL, type):
                relations = getattr(self._adapted, 'dynamicview_relations', {})
                for methodname in relations.get(tag, []):
                    for remote in self.get_remote_relatables(methodname):
                        yield BaseRelation(target, remote, type)

        def get_remote_relatables(self, methodname):
            """Generate object relatables returned by adapted.methodname()."""
            method = getattr(self._adapted, methodname, None)
            if not method or not callable(method):
                LOG.warning(
                    "no %r relationship or method for %r",
                    methodname,
                    self._adapted.meta_type)

                return

            r = method()
            if not r:
                return

            try:
                for obj in r:
                    yield IRelatable(obj)

            except TypeError:
                yield IRelatable(r)


# Templates #################################################################

JS_LINK_FROM_GRID = """
Ext.apply(Zenoss.render, {
    zenpacklib_entityLinkFromGrid: function(obj, metaData, record, rowIndex, colIndex) {
        if (!obj)
            return;

        if (typeof(obj) == 'string')
            obj = record.data;

        if (!obj.title && obj.name)
            obj.title = obj.name;

        var isLink = false;

        if (this.refName == 'componentgrid') {
            // Zenoss >= 4.2 / ExtJS4
            if (colIndex != 1 || this.subComponentGridPanel)
                isLink = true;
        } else {
            // Zenoss < 4.2 / ExtJS3
            if (!this.panel || this.panel.subComponentGridPanel)
                isLink = true;
        }

        if (isLink) {
            return '<a href="javascript:Ext.getCmp(\\'component_card\\').componentgrid.jumpToEntity(\\''+obj.uid+'\\', \\''+obj.meta_type+'\\');">'+obj.title+'</a>';
        } else {
            return obj.title;
        }
    },

    zenpacklib_entityTypeLinkFromGrid: function(obj, metaData, record, rowIndex, colIndex) {
        if (!obj)
            return;

        if (typeof(obj) == 'string')
            obj = record.data;

        if (!obj.title && obj.name)
            obj.title = obj.name;

        var isLink = false;

        if (this.refName == 'componentgrid') {
            // Zenoss >= 4.2 / ExtJS4
            if (colIndex != 1 || this.subComponentGridPanel)
                isLink = true;
        } else {
            // Zenoss < 4.2 / ExtJS3
            if (!this.panel || this.panel.subComponentGridPanel)
                isLink = true;
        }

        if (isLink) {
            return '<a href="javascript:Ext.getCmp(\\'component_card\\').componentgrid.jumpToEntity(\\''+obj.uid+'\\', \\''+obj.meta_type+'\\');">'+obj.title+'</a> (' + obj.class_label + ')';
        } else {
            return obj.title;
        }
    }

});

ZC.ZPLComponentGridPanel = Ext.extend(ZC.ComponentGridPanel, {
    subComponentGridPanel: false,

    jumpToEntity: function(uid, meta_type) {
        var tree = Ext.getCmp('deviceDetailNav').treepanel;
        var tree_selection_model = tree.getSelectionModel();
        var components_node = tree.getRootNode().findChildBy(
            function(n) {
                if (n.data) {
                    // Zenoss >= 4.2 / ExtJS4
                    return n.data.text == 'Components';
                }

                // Zenoss < 4.2 / ExtJS3
                return n.text == 'Components';
            });

        var component_card = Ext.getCmp('component_card');

        if (components_node.data) {
            // Zenoss >= 4.2 / ExtJS4
            component_card.setContext(components_node.data.id, meta_type);
        } else {
            // Zenoss < 4.2 / ExtJS3
            component_card.setContext(components_node.id, meta_type);
        }

        component_card.selectByToken(uid);

        var component_type_node = components_node.findChildBy(
            function(n) {
                if (n.data) {
                    // Zenoss >= 4.2 / ExtJS4
                    return n.data.id == meta_type;
                }

                // Zenoss < 4.2 / ExtJS3
                return n.id == meta_type;
            });

        if (component_type_node.select) {
            tree_selection_model.suspendEvents();
            component_type_node.select();
            tree_selection_model.resumeEvents();
        } else {
            tree_selection_model.select([component_type_node], false, true);
        }
    }
});

Ext.reg('ZPLComponentGridPanel', ZC.ZPLComponentGridPanel);

Zenoss.ZPLRenderableDisplayField = Ext.extend(Zenoss.DisplayField, {
    constructor: function(config) {
        if (typeof(config.renderer) == 'string') {
          config.renderer = eval(config.renderer)
        }
        Zenoss.ZPLRenderableDisplayField.superclass.constructor.call(this, config);
    }
});

Ext.reg('ZPLRenderableDisplayField', 'Zenoss.ZPLRenderableDisplayField');

""".strip()


if __name__ == '__main__':
    from Products.ZenUtils.ZenScriptBase import ZenScriptBase

    class ZPLCommand(ZenScriptBase):
        def run(self):
            args = sys.argv[1:]

            if len(args) == 2 and args[0] == 'lint':
                filename = args[1]

                with open(filename, 'r') as file:
                    linecount = len(file.readlines())

                # Change our logging output format.
                logging.getLogger().handlers = []
                for logger in logging.Logger.manager.loggerDict.values():
                    logger.handlers = []
                handler = logging.StreamHandler(sys.stdout)
                formatter = logging.Formatter(
                    fmt='%s:%s:0: %%(message)s' % (filename, linecount))
                handler.setFormatter(formatter)
                logging.getLogger().addHandler(handler)

                try:
                    with open(filename, 'r') as stream:
                        yaml.load(stream, Loader=WarningLoader)
                except Exception, e:
                    LOG.exception(e)

            elif len(args) == 3 and args[0] == 'py_to_yaml':
                zenpack_name = args[1]
                filename = args[2]

                # create a dummy zenpacklib sufficient to be used in an
                # __init__.py, so we can capture export the data.
                zenpacklib_module = create_module("zenpacklib")
                zenpacklib_module.ZenPackSpec = type('ZenPackSpec', (dict,), {})

                def zpl_create(self):
                    zenpacklib_module.CFG = dict(self)
                zenpacklib_module.ZenPackSpec.create = zpl_create

                stream = open(filename, 'r')
                inputfile = stream.read()

                # tweak the input slightly.
                inputfile = re.sub(r'from .* import zenpacklib', '', inputfile)

                g = dict(zenpacklib=zenpacklib_module)
                l = dict()
                exec inputfile in g, l

                CFG = zenpacklib_module.CFG
                CFG['name'] = zenpack_name

                # convert the cfg dictionary to yaml
                specparams = ZenPackSpecParams(**CFG)
                outputfile = yaml.dump(specparams)

                # tweak the yaml slightly.
                outputfile = outputfile.replace("__builtin__.object", "object")

                print outputfile

            else:
                print "Usage: %s lint <file.yaml> | py_to_yaml <zenpack name> <__init__.py>" % sys.argv[0]

    script = ZPLCommand()
    script.run()
