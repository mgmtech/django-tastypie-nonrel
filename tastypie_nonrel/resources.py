from django.conf.urls.defaults import url
from django.core.exceptions import ObjectDoesNotExist
from tastypie.resources import ModelResource
from tastypie.fields import CharField, DateTimeField, BooleanField, FloatField, IntegerField, FileField
from tastypie_nonrel.fields import DictField, ListField, EmbeddedModelField, EmbeddedListField
from tastypie.http import HttpGone, HttpCreated, HttpAccepted
from tastypie.utils import trailing_slash, dict_strip_unicode_keys
from tastypie.exceptions import ImmediateHttpResponse, NotFound
from tastypie.bundle import Bundle
from fields import EmbeddedCollection

class MongoResource(ModelResource):
    """Minor enhancements to the stock ModelResource to allow subresources."""
    def dispatch_subresource(self, request, subresource_name, **kwargs):
        field = self.fields[subresource_name]
        resource = field.to_class()
        request_type = kwargs.pop('request_type')
        return resource.dispatch(request_type, request, **kwargs)


    @classmethod
    def api_field_from_django_field(cls, f, default=CharField):
        """
        Returns the field type that would likely be associated with each
        Django type.
        """
        result = default

        if f.get_internal_type() in ('DateField', 'DateTimeField'):
            result = DateTimeField
        elif f.get_internal_type() in ('BooleanField', 'NullBooleanField'):
            result = BooleanField
        elif f.get_internal_type() in ('DecimalField', 'FloatField'):
            result = FloatField
        elif f.get_internal_type() in ('IntegerField', 'PositiveIntegerField', 'PositiveSmallIntegerField', 'SmallIntegerField'):
            result = IntegerField
        elif f.get_internal_type() in ('FileField', 'ImageField'):
            result = FileField
        elif f.get_internal_type() == 'DictField':
            result = DictField
        elif f.get_internal_type() == 'ListField':
            result = ListField
            if hasattr(f.item_field, 'embedded_model'):
                result = EmbeddedListField
        elif f.get_internal_type() == 'EmbeddedModelField':
            result = EmbeddedModelField
        # TODO: Perhaps enable these via introspection. The reason they're not enabled
        #       by default is the very different ``__init__`` they have over
        #       the other fields.
        # elif f.get_internal_type() == 'ForeignKey':
        #     result = ForeignKey
        # elif f.get_internal_type() == 'ManyToManyField':
        #     result = ManyToManyField

        return result


    @classmethod
    def get_fields(cls, fields=None, excludes=None):
        """
        Given any explicit fields to include and fields to exclude, add
        additional fields based on the associated model.
        """
        final_fields = {}
        fields = fields or []
        excludes = excludes or []

        if not cls._meta.object_class:
            return final_fields

        for f in cls._meta.object_class._meta.fields:
            # If the field name is already present, skip
            if f.name in cls.base_fields:
                continue

            # If field is not present in explicit field listing, skip
            if fields and f.name not in fields:
                continue

            # If field is in exclude list, skip
            if excludes and f.name in excludes:
                continue

            if cls.should_skip_field(f):
                continue

            api_field_class = cls.api_field_from_django_field(f)

            kwargs = {
                'attribute': f.name,
                'help_text': f.help_text,
            }

            if f.null is True:
                kwargs['null'] = True

            kwargs['unique'] = f.unique

            if not f.null and f.blank is True:
                kwargs['default'] = ''

            if f.get_internal_type() == 'TextField':
                kwargs['default'] = ''

            if f.has_default():
                kwargs['default'] = f.default

            if hasattr(f, 'embedded_model'):
                kwargs["to"] = f.embedded_model
            elif hasattr(f, 'item_field') and hasattr(f.item_field, 'embedded_model'):
                kwargs["of"] = f.item_field.embedded_model

            final_fields[f.name] = api_field_class(**kwargs)
            final_fields[f.name].instance_name = f.name

        return final_fields


    def base_urls(self):
        base = super(MongoResource, self).base_urls()

        embedded = ((name, obj) for name, obj in self.fields.items() if isinstance(obj, EmbeddedCollection))

        embedded_urls = []

        for name, obj in embedded:
            embedded_urls.extend([
                url(r"^(?P<resource_name>%s)/(?P<pk>\w[\w-]*)/(?P<subresource_name>%s)%s$" %
                    (self._meta.resource_name, name, trailing_slash()),
                    self.wrap_view('dispatch_subresource'),
                    {'request_type': 'list'},
                    name='api_dispatch_subresource_list'),

                url(r"^(?P<resource_name>%s)/(?P<pk>\w[\w-]*)/(?P<subresource_name>%s)/(?P<index>\w[\w-]*)%s$" %
                    (self._meta.resource_name, name, trailing_slash()),
                    self.wrap_view('dispatch_subresource'),
                    {'request_type': 'detail'},
                    name='api_dispatch_subresource_detail')
                ])
        return embedded_urls + base


class MongoListResource(ModelResource):
    """An embedded MongoDB list acting as a collection. Used in conjunction with
       a EmbeddedCollection.
    """

    def __init__(self, parent=None, attribute=None, api_name=None):
        self.parent = parent
        self.attribute = attribute
        self.instance = None
        super(MongoListResource, self).__init__(api_name)


    def dispatch(self, request_type, request, **kwargs):
        self.instance = self.safe_get(request, **kwargs)
        return super(MongoListResource, self).dispatch(request_type, request, **kwargs)

    def safe_get(self, request, **kwargs):
        filters = self.remove_api_resource_names(kwargs)
        try:
            del(filters['index'])
        except KeyError:
            pass

        try:
            return self.parent.cached_obj_get(request=request, **filters)
        except ObjectDoesNotExist:
            raise ImmediateHttpResponse(response=HttpGone())


    def remove_api_resource_names(self, url_dict):
        kwargs_subset = url_dict.copy()

        for key in ['api_name', 'resource_name', 'subresource_name']:
            try:
                del(kwargs_subset[key])
            except KeyError:
                pass

        return kwargs_subset

    def get_object_list(self, request):
        if not self.instance:
            return []

        def add_index(index, obj):
            obj.pk = index
            return obj

        return [add_index(index, obj) for index, obj in enumerate(getattr(self.instance, self.attribute))]

    def obj_get_list(self, request=None, **kwargs):
        return self.get_object_list(request)

    def obj_get(self, request=None, **kwargs):
        index = int(kwargs['index'])
        try:
            return self.get_object_list(request)[index]
        except IndexError:
            raise ImmediateHttpResponse(response=HttpGone())

    def obj_create(self, bundle, request=None, **kwargs):
        bundle = self.full_hydrate(bundle)
        getattr(self.instance, self.attribute).append(bundle.obj)
        self.instance.save()
        return bundle

    def obj_update(self, bundle, request=None, **kwargs):
        index = int(kwargs['index'])
        try:
            bundle.obj = self.get_object_list(request)[index]
        except IndexError:
            raise NotFound("A model instance matching the provided arguments could not be found.")
        bundle = self.full_hydrate(bundle)
        new_index = int(bundle.data['id'])
        lst = getattr(self.instance, self.attribute)
        lst.pop(index)
        lst.insert(new_index, bundle.obj)
        self.instance.save()
        return bundle

    def obj_delete(self, request=None, **kwargs):
        index = int(kwargs['index'])
        self.obj_get(request, **kwargs)
        getattr(self.instance, self.attribute).pop(index)
        self.instance.save()

    def obj_delete_list(self, request=None, **kwargs):
        setattr(self.instance, self.attribute, [])
        self.instance.save()

    def put_detail(self, request, **kwargs):
        """
        Either updates an existing resource or creates a new one with the
        provided data.

        Calls ``obj_update`` with the provided data first, but falls back to
        ``obj_create`` if the object does not already exist.

        If a new resource is created, return ``HttpCreated`` (201 Created).
        If an existing resource is modified, return ``HttpAccepted`` (204 No Content).
        """
        deserialized = self.deserialize(request, request.raw_post_data, format=request.META.get('CONTENT_TYPE', 'application/json'))
        bundle = self.build_bundle(data=dict_strip_unicode_keys(deserialized))
        self.is_valid(bundle, request)

        try:
            updated_bundle = self.obj_update(bundle, request=request, **kwargs)
            return HttpAccepted()
        except:
            updated_bundle = self.obj_create(bundle, request=request, **kwargs)
            return HttpCreated(location=self.get_resource_uri(updated_bundle))


    def get_resource_uri(self, bundle_or_obj):
        if isinstance(bundle_or_obj, Bundle):
            obj = bundle_or_obj.obj
        else:
            obj = bundle_or_obj


        kwargs = {
            'resource_name': self.parent._meta.resource_name,
            'subresource_name': self.attribute
        }
        if self.instance:
            kwargs['pk'] = self.instance.pk


        kwargs['index'] = obj.pk


        if self._meta.api_name is not None:
            kwargs['api_name'] = self._meta.api_name

        ret = self._build_reverse_url('api_dispatch_subresource_detail',
                                       kwargs=kwargs)

        return ret
