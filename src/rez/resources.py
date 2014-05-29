"""
Class for loading and verifying rez metafiles.

Resources are an abstraction of rez's file and directory structure. Currently,
a resource can be a file or directory (with eventual support for other types).
A resource is given a hierarchical name and a file path pattern (like
"{name}/{version}/package.yaml") and are collected under a particular
configuration version.

If the resource is a file, an optional metadata schema can be provided to
validate the contents (e.g. enforce data types and document structure) of the
data. This validation is run after the data is deserialized, so it is decoupled
from the storage format. New resource formats can be added and share the same
validators.

The upshot is that once a resource is registered, instances of the resource can
be iterated over using `iter_resources` without the higher level code requiring
an understanding of the underlying file and folder structure.  This ensures that
the addition of new resources is localized to the registration functions
provided by this module.
"""
import os
import sys
import inspect
import re
import string
from collections import defaultdict
from fnmatch import fnmatch
from rez.settings import settings, Settings
from rez.util import to_posixpath, propertycache, print_warning_once, Namespace
from rez.exceptions import PackageMetadataError, ResourceError
from rez.vendor.version.version import Version, VersionRange
from rez.vendor import yaml
# FIXME: handle this double-module business
from rez.vendor.schema.schema import Schema, Use, And, Or, Optional, SchemaError


# list of resource classes, keyed by config_version
_configs = defaultdict(list)

PACKAGE_NAME_REGSTR = '[a-zA-Z_][a-zA-Z0-9_]*'
VERSION_COMPONENT_REGSTR = '(?:[0-9a-zA-Z_]+)'
VERSION_REGSTR = '%(comp)s(?:[.-]%(comp)s)*' % dict(comp=VERSION_COMPONENT_REGSTR)

def _split_path(path):
    return path.rstrip(os.path.sep).split(os.path.sep)

def _or_regex(strlist):
    return '|'.join('(%s)' % e for e in strlist)

#------------------------------------------------------------------------------
# Base Classes and Functions
#------------------------------------------------------------------------------

def _process_python_objects(data):
    """process special objects.

    Changes made:
      - functions with an `immediate` attribute that evaluates to True will be
        called immediately.
    """
    # FIXME: the `immediate` attribute is used to tell us if a function
    # should be executed immediately on load, but we need to work
    # out the exact syntax.  maybe a 'rex' attribute that conveys
    # the opposite meaning (i.e. defer execution until later) would be better.
    # We could also provide a @rex decorator to set the attribute.
    for k, v in data.iteritems():
        if inspect.isfunction(v) and getattr(v, 'immediate', False):
            data[k] = v()
        elif isinstance(v, dict):
            # because dicts are changed in place, we don't need to re-assign
            _process_python_objects(v)
    return data


def load_python(stream):
    """load a python module into a metadata dictionary.

    - module-level attributes become root entries in the dictionary.
    - module-level functions which take no arguments will be called immediately
        and the returned value will be stored in the dictionary

    Example:

        >>> load_python('''
        config_version = 0
        name = 'foo'
        def requires():
            return ['bar']''')

    Args:
        stream (string, open file object, or code object): stream of python
            code which will be passed to ``exec``

    Returns:
        dict: dictionary of non-private objects added to the globals
    """
    # TODO: support class-based design, where the attributes and methods of the
    # class become values in the dictionary
    g = __builtins__.copy()
    g['Namespace'] = Namespace
    excludes = set(['Namespace', '__builtins__'])
    exec stream in g
    result = {}
    for k, v in g.iteritems():
        if k not in excludes and \
                (k not in __builtins__ or __builtins__[k] != v):
            result[k] = v
    # add in any namespaces used
    result.update(Namespace.get_namespace())
    result = _process_python_objects(result)
    return result


def load_yaml(stream):
    """load a yaml stream into a metadata dictionary.

    Args:
        stream (string, or open file object): stream of text which will be
            passed to ``yaml.load``

    Returns:
        dict
    """

    if hasattr(stream, 'read'):
        text = stream.read()
    else:
        text = stream
    return yaml.load(text) or {}

# keep a simple dictionary of loaders for now
metadata_loaders = {}
metadata_loaders['py'] = load_python
metadata_loaders['yaml'] = load_yaml
# hack for info.txt. for now we force .txt to parse using yaml. this format
# will be going away
metadata_loaders['txt'] = metadata_loaders['yaml']


def get_file_loader(filepath):
    scheme = os.path.splitext(filepath)[1][1:]
    try:
        return metadata_loaders[scheme]
    except KeyError:
        raise ResourceError("Unknown metadata storage scheme: %r" % scheme)


# FIXME: add lru_cache here?
def load_file(filepath, loader=None):
    """Read metadata from a file.

    Determines the proper de-serialization scheme based on file extension.

    Args:
        filepath (str): Path to the file from which to read metadata.
        loader (callable or str, optional): callable which will take an open
            file handle and return a metadata dictionary. Can also be a key
            to the `metadata_loaders` dictionary.
    Returns:
        dict: the metadata
    """
    if loader is None:
        loader = get_file_loader(filepath)
    elif isinstance(loader, basestring):
        loader = metadata_loaders[loader]

    with open(filepath, 'r') as f:
        try:
            return loader(f)
        except Exception as e:
            # FIXME: this stack fix is probably specific to `load_python` and
            # should be moved there.
            import traceback
            frames = traceback.extract_tb(sys.exc_traceback)
            while frames and frames[0][0] != filepath:
                frames = frames[1:]
            stack = ''.join(traceback.format_list(frames)).strip()
            raise PackageMetadataError(filepath, "%s\n%s" % (str(e), stack))


#------------------------------------------------------------------------------
# Resources and Configurations
#------------------------------------------------------------------------------

def register_resource(config_version, resource):
    """Register a `Resource` class.

    This informs rez where to find a resource relative to the
    rez search path, and optionally how to validate its data.

    Args:
        resource (Resource): the resource class.
    """
    version_configs = _configs[config_version]

    assert resource.key is not None, \
        "Resource must implement the 'key' attribute"
    # version_configs is a list and not a dict so that it stays ordered
    if resource.key in set(r.key for r in version_configs):
        raise ResourceError("resource already exists: %r" % resource.key)

    version_configs.append(resource)

    if resource.parent_resource:
        Resource._children[resource.parent_resource].append(resource)

#------------------------------------------------------------------------------
# MetadataSchema Implementations
#------------------------------------------------------------------------------

# 'name-1.2'
# FIXME: cast to version.Requirment?
package_requirement = basestring

# TODO: inspect arguments of the function to confirm proper number?
rex_command = Or(callable,     # python function
                 basestring,   # new-style rex
                 )

# make an alias which just so happens to be the same number of characters as
# 'Optional'  so that our schema are easier to read
Required = Schema

# The master package schema.  All resources delivering metadata to the Package
# class must ultimately validate against this master schema. This schema
# intentionally does no casting of types: that should happen on the resource
# schemas.
package_schema = Schema({
    Required('config_version'):         int,
    Optional('uuid'):                   basestring,
    Optional('description'):            basestring,
    Required('name'):                   basestring,
    Required('version'):                Version,
    Optional('authors'):                [basestring],
    Required('timestamp'):              int,
    Optional('config'):                 Settings,
    Optional('help'):                   Or(basestring,
                                           [[basestring]]),
    Optional('tools'):                  [basestring],
    Optional('requires'):               [package_requirement],
    Optional('build_requires'):         [package_requirement],
    Optional('private_build_requires'): [package_requirement],
    Optional('variants'):               [[package_requirement]],
    Optional('commands'):               rex_command,
    # swap-comment these 2 lines if we decide to allow arbitrary root metadata
    Optional('custom'):                 object,
    # Optional(object):                   object
})


class Resource(object):
    """Abstract base class for data resources.

    The `Package` class expects its metadata to match a specific schema, but
    each individual resource may have its own schema specific to the file it
    loads, and that schema is able to modify the data on validation so that it
    conforms to the Package's master schema.

    As an additional conform layer, each resource implements a `load` method,
    which is the main entry point for loading that resource's metadata. By
    default, this method loads the contents of the resource file and validates
    its contents using the Resource's schema, however, this method can also
    be used to mutate the metadata and graft other resources into it.

    In this paradigm, the responsibility for handling variability is shifted
    from the package to the resource. This makes it easier to implement
    different resources that present the same metatada interface to the
    `Package` class.  As long as the `Package` class gets metadata that matches
    its expected schema, it doesn't care how it gets there. The end result is
    that it is much easier to add new file and folder structures to rez
    without the need to modify the `Package` class, which remains a fairly
    static public interface.

    Attributes:
        key (str): The name of the resource. Used with the resource utilty
            functions `iter_resources`, `get_resource`, and `load_resource`
            when the type of resource desired is known. This attribute must be
            overridden by `Resource` subclasses.
        schema (Schema, optional): schema defining the structure of the data
            which will be loaded
        parent_resource (Resource class): the resource above this one in the
            tree.  An instance of this type is passed to `iter_instances`
            on this class to allow this resource to determine which instances
            of itself exist under an instance of its parent resource.
    """
    key = None
    schema = None
    parent_resource = None

    _children = defaultdict(list)  # gets filled by register_resource

    def __init__(self, path, variables):
        """
        Args:
            path (str): path of the file to be loaded.
            variables (dict): variables that define this resource. For example,
                a package has a name and a version. Some of these variables may
                have been used to construct `path`.
        """
        super(Resource, self).__init__()
        self.variables = variables
        self.path = path

    def load(self):
        """load the resource's data.

        Returns:
            The resource data as a dict. The implementation should validate the
            data against the schema, if any.
        """
        raise NotImplemented

    @classmethod
    def from_path(cls, path):
        """Create a resource from a path.

        Returns:
            A `Resource` instance.
        """
        raise NotImplemented

    def __repr__(self):
        return "%s(%r, %r)" % (self.__class__.__name__, self.path,
                               self.variables)

    # --- info

    @classmethod
    def children(cls):
        """Get a tuple of the resource classes which consider this class its
        parent"""
        return tuple(cls._children[cls])

    @classmethod
    def parents(cls):
        """Get a tuple of all the resources above this one, in descending order
        """
        def it():
            if cls.parent_resource:
                for parent in cls.parent_resource.parents():
                    yield parent
                yield cls.parent_resource
        return tuple(it())

    # --- instantiation

    @classmethod
    def iter_instances(cls, parent_resource):
        """Iterate over instances of this class which reside under the given
        parent resource.

        Args:
            parent_resource (Resource): resource instance of the type specified
                by this class's `parent_resource` attribute

        Returns:
            iterator of `Resource` instances
        """
        raise NotImplementedError


class FileSystemResource(Resource):
    """A resource that resides on disk.

    Attributes:
        path_pattern (str, optional): a path str, relative to the rez search
            path, containing variable tokens such as ``{name}``.  This is used
            to determine if a resource is compatible with a given file path. If
            a resource does not provide a `path_pattern` it will only be used
            if explictly requested.
        variable_regex (list of (str, str) pairs): the names of the tokens
            which can be expanded within the `path_pattern` and their
            corresponding regular expressions.
    """
    path_pattern = None
    is_file = None
    variable_regex = [('version', VERSION_REGSTR),
                      ('name', PACKAGE_NAME_REGSTR),
                      ]

    # -- path pattern helpers

    @classmethod
    def _expand_pattern(cls, pattern):
        "expand variables in a search pattern with regular expressions"
        # escape literals:
        #   '{package}.{ext}' --> '\{package\}\.\{ext\}'
        pattern = re.escape(pattern)
        # search path cannot be handled at the class-level because it may
        # change after import
        expansions = [('search_path', _or_regex(settings.packages_path))]
        for key, value in cls.variable_regex + expansions:
            # escape key so it matches escaped pattern:
            #   'search_path' --> 'search\_path'
            pattern = pattern.replace(r'\{%s\}' % re.escape(key),
                                      '(?P<%s>%s)' % (key, value))
        return pattern + '$'

    @classmethod
    def _parse_filepart(cls, filepath):
        """parse `filepath` against the resource's `path_pattern`.

        Args:
            filepath (str): path to parse.
        Returns:
            str: part of `filepath` that matched
            dict: dictionary of variables
        """
        if not cls.path_pattern:
            return
        return cls._parse_pattern(filepath, cls.path_pattern,
                                  '_compiled_pattern')

    @classmethod
    def _parse_filepath(cls, filepath):
        """parse `filepath` against the joined `path_pattern` of this resource
        and all of its parents.

        Args:
            filepath (str): path to parse.
        Returns:
            str: part of `filepath` that matched
            dict: dictionary of variables
        """
        hierachy = cls.parents() + (cls,)
        parts = [r.path_pattern for r in hierachy]
        if any(p for p in parts if p is None):
            raise ResourceError("All path resources must have path patterns")

        pattern = os.path.sep.join(parts)
        return cls._parse_pattern(filepath, pattern,
                                  '_compiled_full_pattern')

    @classmethod
    def _parse_pattern(cls, filepath, pattern, class_storage):
        """
        Returns:
            str: the part of `filepath` that matches `pattern`
            dict: the variables in `pattern` that matched
        """
        if not hasattr(cls, class_storage):
            pattern = cls._expand_pattern(pattern)
            pattern = r'^' + pattern
            reg = re.compile(pattern)
            setattr(cls, class_storage, reg)
        else:
            reg = getattr(cls, class_storage)

        m = reg.match(to_posixpath(filepath))
        if m:
            return m.group(0), m.groupdict()

    # -- instantiation

    @classmethod
    def iter_instances(cls, parent_resource):
        """Iterate over instances of this class which reside under the given
        parent resource.

        Args:
            parent_resource (Resource): resource instance of the type specified
                by this class's `parent_resource` attribute

        Returns:
            iterator of `Resource` instances
        """
        # FIXME: cache these disk crawls
        for path in os.listdir(parent_resource.path):
            fullpath = os.path.join(parent_resource.path, path)
            if os.path.isfile(fullpath) == cls.is_file:
                match = cls._parse_filepart(path)
                if match is not None:
                    variables = match[1]
                    variables.update(parent_resource.variables)
                    yield cls(fullpath, variables)

    @classmethod
    def from_path(cls, path):
        """Create a resource from a file path"""

        if not cls.path_pattern:
            raise ResourceError("Cannot create resource %r from %r: "
                                "does not have path patterns" %
                                (cls.key, path))
        filepath = os.path.abspath(path)
        result = cls._parse_filepath(filepath)
        if result is None:
            raise ResourceError("Cannot create resource %r from %r: "
                                "file did not match path patterns" %
                                (cls.key, filepath))
        match_path, variables = result
        return cls(filepath, variables)


class FolderResource(FileSystemResource):
    "A resource representing a directory on disk"
    is_file = False


class FileResource(FileSystemResource):
    "A resource representing a file on disk"
    variable_regex = FileSystemResource.variable_regex + \
        [('ext', _or_regex(metadata_loaders.keys()))]
    is_file = True
    loader = None

    def load(self):
        """load the resource data.

        For a file this means use `load_file` to deserialize the data, and then
        validate it against the `Schema` instance provided by the resource's
        `schema` attribute.

        This gives the resource a chance to do further modifications to the
        loaded metadata (beyond what is possible or practical to do with the
        schema), for example, changing the name of keys, or grafting on data
        loaded from other reources.
        """
        if os.path.isfile(self.path):
            data = load_file(self.path, self.loader)
            if self.schema:
                try:
                    return self.schema.validate(data)
                except SchemaError, err:
                    raise PackageMetadataError(self.path, str(err))
            else:
                return data


class PackagesRoot(FolderResource):
    """Represents a root directory in Settings.pakcages_path"""
    key = 'folder.packages_root'
    path_pattern = '{search_path}'


class NameFolder(FolderResource):
    key = 'folder.name'
    path_pattern = '{name}'
    parent_resource = PackagesRoot


class VersionFolder(FolderResource):
    key = 'folder.version'
    path_pattern = '{version}'
    parent_resource = NameFolder


# -- deprecated

class MetadataFolder(FolderResource):
    key = 'folder.metadata'
    path_pattern = '.metadata'
    parent_resource = VersionFolder


class ReleaseTimestampResource(FileResource):
    # Deprecated
    key = 'release.timestamp'
    path_pattern = 'release_time.txt'
    parent_resource = MetadataFolder
    schema = Use(int)


class ReleaseInfoResource(FileResource):
    # Deprecated
    key = 'release.info'
    path_pattern = 'info.txt'
    parent_resource = MetadataFolder
    schema = Schema({
        Required('ACTUAL_BUILD_TIME'): int,
        Required('BUILD_TIME'): int,
        Required('USER'): basestring,
        Optional('SVN'): basestring
    })

# -- END deprecated


class ReleaseDataResource(FileResource):
    key = 'release.data'
    path_pattern = 'release.yaml'
    parent_resource = VersionFolder

    schema = Schema({
        Required('timestamp'): int,
        Required('revision'): basestring,
        Required('changelog'): basestring,
        Required('release_message'): basestring,
        Required('previous_version'): basestring,
        Required('previous_revision'): basestring
    })


class BasePackageResource(FileResource):
    """
    Abstract class providing the standard set of package metadata.
    """

    def convert_to_rex(self, commands):
        from rez.util import convert_old_commands, print_warning_once
        if settings.warn("old_commands"):
            print_warning_once("%s is using old-style commands."
                               % self.path)

        return convert_old_commands(commands)

    @propertycache
    def schema(self):
        return Schema({
            Required('config_version'):         0,  # this will only match 0
            Optional('uuid'):                   basestring,
            Optional('description'):            And(basestring,
                                                    Use(string.strip)),
            Required('name'):                   self.variables['name'],
            Optional('authors'):                [basestring],
            Optional('config'):                 And(dict,
                                                    Use(lambda x:
                                                        Settings(overrides=x))),
            Optional('help'):                   Or(basestring,
                                                   [[basestring]]),
            Optional('tools'):                  [basestring],
            Optional('requires'):               [package_requirement],
            Optional('build_requires'):         [package_requirement],
            Optional('private_build_requires'): [package_requirement],
            Optional('variants'):               [[package_requirement]],
            Optional('commands'):               Or(rex_command,
                                                   And([basestring],
                                                       Use(self.convert_to_rex))),
            # swap-comment these 2 lines if we decide to allow arbitrary root metadata
            Optional('custom'):                 object,
            # basestring: object
        })

    def load_timestamp(self):
        timestamp = 0
        try:
            release_data = load_resource(
                0,
                resource_keys=['release.data'],
                search_path=self.variables['search_path'],
                variables=self.variables)
            timestamp = release_data.get('timestamp', 0)
        except ResourceError:
            try:
                timestamp = load_resource(
                    0,
                    resource_keys=['release.timestamp'],
                    search_path=self.variables['search_path'],
                    variables=self.variables)
            except ResourceError:
                pass
        if not timestamp:
            # FIXME: should we deal with is_local here or in rez.packages?
            if not timestamp and settings.warn("untimestamped"):
                print_warning_once("Package is not timestamped: %s" %
                                   self.path)
        return timestamp


class VersionlessPackageResource(BasePackageResource):
    key = 'package.versionless'
    path_pattern = 'package.{ext}'
    parent_resource = NameFolder

    def load(self):
        data = super(VersionlessPackageResource, self).load()
        data['timestamp'] = self.load_timestamp()
        data['version'] = Version()

        return data


class VersionedPackageResource(BasePackageResource):
    key = 'package.versioned'
    path_pattern = 'package.{ext}'
    parent_resource = VersionFolder

    @propertycache
    def schema(self):
        schema = super(VersionedPackageResource, self).schema._schema
        schema = schema.copy()
        schema.update({
            Required('version'): And(self.variables['version'],
                                     Use(Version))
        })
        return Schema(schema)

    def load(self):
        data = super(VersionedPackageResource, self).load()
        data['timestamp'] = self.load_timestamp()

        return data


class CombinedPackageFamilyResource(BasePackageResource):
    """
    A single file containing multiple versioned packages.

    A combined package consists of a single file and thus does not have a
    directory in which to put package resources.
    """
    key = 'package_family.combined'
    path_pattern = '{name}.{ext}'
    parent_resource = PackagesRoot

    @propertycache
    def schema(self):
        schema = super(CombinedPackageFamilyResource, self).schema._schema
        schema = schema.copy()
        schema.update({
            Optional('versions'): [Use(Version)],
            Optional('version_overrides'): {
                Use(VersionRange): {
                    Optional('help'):                   Or(basestring,
                                                           [[basestring]]),
                    Optional('tools'):                  [basestring],
                    Optional('requires'):               [package_requirement],
                    Optional('build_requires'):         [package_requirement],
                    Optional('private_build_requires'): [package_requirement],
                    Optional('variants'):               [[package_requirement]],
                    Optional('commands'):               Or(rex_command,
                                                           And([basestring],
                                                               Use(self.convert_to_rex))),
                    # swap-comment these 2 lines if we decide to allow arbitrary root metadata
                    Optional('custom'):                 object,
                    # basestring:                         object
                }
            }
        })
        return Schema(schema)

    def load(self):
        data = super(CombinedPackageFamilyResource, self).load()

        # convert 'versions' from a list of `Version` to a list of complete
        # package data
        versions = data.pop('versions', [Version()])
        overrides = data.pop('version_overrides', {})
        if versions:
            new_versions = []
            for version in versions:
                # FIXME: order matters here: use OrderedDict or make
                # version_overrides a list instead of a dict?
                ver_data = data.copy()
                for ver_range in sorted(overrides.keys()):
                    if version in ver_range:
                        ver_data.update(overrides[ver_range])
                        break
                ver_data['version'] = version
                new_versions.append(ver_data)

            data['versions'] = new_versions
        return data


class CombinedPackageResource(CombinedPackageFamilyResource):
    """A versioned package that is contained within a
    `CombinedPackageFamilyResource`.
    """
    key = 'package.combined'
    parent_resource = CombinedPackageFamilyResource

    # FIXME is load() missing here??

    @classmethod
    def iter_instances(cls, parent_resource):
        data = parent_resource.load()
        for ver_data in data['versions']:
            variables = parent_resource.variables.copy()
            variables['version'] = str(ver_data['version'])
            yield cls(parent_resource.path, variables)


class BuiltPackageResource(VersionedPackageResource):
    """A package that is built with the intention to release.

    Same as `VersionedPackageResource`, but stricter about the existence of
    certain metadata.

    This resource has no path_pattern because it is strictly for validation
    during the build process.
    """
    key = 'package.built'
    path_pattern = None
    parent_resource = None

    @property
    def schema(self):
        schema = super(BuiltPackageResource, self).schema._schema
        schema = schema.copy()
        # swap optional to required:
        for key, value in schema.iteritems():
            if key._schema in ('uuid', 'description', 'authors'):
                newkey = Required(key._schema)
                schema[newkey] = schema.pop(key)
        return Schema(schema)


register_resource(0, VersionedPackageResource)

register_resource(0, VersionlessPackageResource)

register_resource(0, BuiltPackageResource)

register_resource(0, ReleaseInfoResource)

register_resource(0, ReleaseTimestampResource)

register_resource(0, ReleaseDataResource)

register_resource(0, NameFolder)

register_resource(0, VersionFolder)

register_resource(0, CombinedPackageFamilyResource)

register_resource(0, CombinedPackageResource)


#------------------------------------------------------------------------------
# Main Entry Points
#------------------------------------------------------------------------------

def list_resource_classes(config_version, keys=None):
    """List resource classes matching the search criteria.

    Args:
        keys (list of str): Name(s) of the type of `Resources` to list. If None,
            all resource types are listed.

    Returns:
        List of `Resource` subclass types.
    """
    resource_classes = _configs.get(config_version)
    if keys:
        resource_classes = [r for r in resource_classes if
                            any(fnmatch(r.key, k) for k in keys)]
    return resource_classes


def _iter_resources(parent_resource):
    # FIXME limited scope
    for child_class in parent_resource.children():
        for child in child_class.iter_instances(parent_resource):
            yield child
            for grand_child in _iter_resources(child):
                yield grand_child


def iter_resources(config_version, resource_keys=None, search_path=None,
                   variables=None):
    """Iterate over `Resource` instances.

    Args:
        resource_keys (str or list of str): Name(s) of the type of `Resources`
            to find. If None, all resource types are searched.
        search_path (list of str, optional): List of root paths under which
            to search for resources.  These typically correspond to the rez
            packages path. Defaults to configured packages path.
        variables (dict, optional): variables which should be used to
            fill the resource's path patterns (e.g. to expand the variables in
            braces in the string '{name}/{version}/package.{ext}')
    """
    def _is_subset(d1, d2):
        return set(d1.items()).issubset(d2.items())
    if isinstance(search_path, basestring):
        search_path = [search_path]
    search_path = settings.default(search_path, "packages_path")

    resource_classes = tuple(list_resource_classes(config_version,
                                                   resource_keys))

    for path in search_path:
        resource = PackagesRoot(path, {'search_path': path})
        for child in _iter_resources(resource):
            if isinstance(child, resource_classes) and (
                    variables is None or
                    _is_subset(variables, child.variables)):
                yield child


def get_resource(config_version, filepath=None, resource_keys=None,
                 search_path=None, variables=None):
    """Find and instantiate a `Resource` instance.

    Provide `resource_keys` and `search_path` and `variables`, or just
    `filepath`.

    Returns the first match.

    Args:
        resource_keys (str or list of str): Name(s) of the type of `Resources`
            to find.
        search_path (list of str, optional): List of root paths under which
            to search for resources. These typicall correspond to the rez
            packages path.
        filepath (str): file to load
        variables (dict, optional): variables which should be used to
            fill the resource's path patterns (e.g. to expand the variables in
            braces in the string '{name}/{version}/package.{ext}')
    """
    if filepath is None and resource_keys is None and variables is None:
        raise ValueError("You must provide either filepath or "
                         "resource_keys + variables")

    if filepath:
        resource_classes = list_resource_classes(config_version, resource_keys)
        assert resource_classes

        for resource_class in resource_classes:
            try:
                return resource_class.from_path(filepath)
            except ResourceError, err:
                pass
        raise ResourceError("Could not find resource matching file %r" %
                            filepath)
    else:
        it = iter_resources(config_version, resource_keys, search_path,
                            variables)
        try:
            return it.next()
        except StopIteration:
            raise ResourceError("Could not find resource matching key(s): %s" %
                                ', '.join(['%r' % r for r in resource_keys]))


def load_resource(config_version, filepath=None, resource_keys=None,
                  search_path=None, variables=None):
    """Find a resource and load its metadata.

    Provide `resource_keys` and `search_path` and `variables`, or just
    `filepath`.

    Returns the first match.

    Args:
    resource_keys (str or list of str): Name(s) of the type of `Resources`
    to find.
    search_path (list of str, optional): List of root paths under which
    to search for resources. These typicall correspond to the rez
    packages path.
    filepath (str): file to load
    variables (dict, optional): variables which should be used to
    fill the resource's path patterns (e.g. to expand the variables in
    braces in the string '{name}/{version}/package.{ext}')
    """
    return get_resource(config_version, filepath, resource_keys, search_path,
                        variables).load()





#    Copyright 2008-2012 Dr D Studios Pty Limited (ACN 127 184 954) (Dr. D Studios)
#
#    This file is part of Rez.
#
#    Rez is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Rez is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with Rez.  If not, see <http://www.gnu.org/licenses/>.
