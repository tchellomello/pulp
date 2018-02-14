from gettext import gettext as _

from django.core import validators

from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from pulpcore.app import models
from pulpcore.app.serializers import (
    BaseURLField,
    DetailIdentityField,
    DetailRelatedField,
    FileField,
    GenericKeyValueRelatedField,
    LatestVersionField,
    MasterModelSerializer,
    ModelSerializer,
)
from rest_framework_nested.relations import (NestedHyperlinkedIdentityField,
                                             NestedHyperlinkedRelatedField)
from rest_framework_nested.serializers import NestedHyperlinkedModelSerializer


class RepositorySerializer(ModelSerializer):
    _href = serializers.HyperlinkedIdentityField(
        view_name='repositories-detail'
    )
    _versions_href = serializers.HyperlinkedIdentityField(
        view_name='versions-list',
        lookup_url_kwarg='repository_pk',
    )
    _latest_version_href = LatestVersionField()
    name = serializers.CharField(
        help_text=_('A unique name for this repository.'),
        validators=[UniqueValidator(queryset=models.Repository.objects.all())]
    )

    description = serializers.CharField(
        help_text=_('An optional description.'),
        required=False
    )

    notes = GenericKeyValueRelatedField(
        help_text=_('A mapping of string keys to string values, for storing notes on this object.'),
        required=False
    )

    class Meta:
        model = models.Repository
        fields = ModelSerializer.Meta.fields + ('_versions_href', '_latest_version_href', 'name',
                                                'description', 'notes')


class ImporterSerializer(MasterModelSerializer):
    """
    Every importer defined by a plugin should have an Importer serializer that inherits from this
    class. Please import from `pulpcore.plugin.serializers` rather than from this module directly.
    """
    _href = DetailIdentityField()
    name = serializers.CharField(
        help_text=_('A unique name for this importer.'),
        validators=[UniqueValidator(queryset=models.Importer.objects.all())]
    )
    feed_url = serializers.CharField(
        help_text='The URL of an external content source.',
        required=False,
    )
    download_policy = serializers.ChoiceField(
        help_text='The policy for downloading content.',
        allow_blank=False,
        choices=models.Importer.DOWNLOAD_POLICIES,
    )
    sync_mode = serializers.ChoiceField(
        help_text='How the importer should sync from the upstream repository.',
        allow_blank=False,
        choices=models.Importer.SYNC_MODES,
    )
    validate = serializers.BooleanField(
        help_text='If True, the plugin will validate imported artifacts.',
        required=False,
    )
    ssl_ca_certificate = FileField(
        help_text='A PEM encoded CA certificate used to validate the server '
                  'certificate presented by the remote server.',
        write_only=True,
        required=False,
    )
    ssl_client_certificate = FileField(
        help_text='A PEM encoded client certificate used for authentication.',
        write_only=True,
        required=False,
    )
    ssl_client_key = FileField(
        help_text='A PEM encoded private key used for authentication.',
        write_only=True,
        required=False,
    )
    ssl_validation = serializers.BooleanField(
        help_text='If True, SSL peer validation must be performed.',
        required=False,
    )
    proxy_url = serializers.CharField(
        help_text='The proxy URL. Format: scheme://user:password@host:port',
        required=False,
    )
    username = serializers.CharField(
        help_text='The username to be used for authentication when syncing.',
        write_only=True,
        required=False,
    )
    password = serializers.CharField(
        help_text='The password to be used for authentication when syncing.',
        write_only=True,
        required=False,
    )
    last_synced = serializers.DateTimeField(
        help_text='Timestamp of the most recent successful sync.',
        read_only=True
    )
    last_updated = serializers.DateTimeField(
        help_text='Timestamp of the most recent update of the importer.',
        read_only=True
    )

    class Meta:
        abstract = True
        model = models.Importer
        fields = MasterModelSerializer.Meta.fields + (
            'name', 'feed_url', 'download_policy', 'sync_mode', 'validate', 'ssl_ca_certificate',
            'ssl_client_certificate', 'ssl_client_key', 'ssl_validation', 'proxy_url',
            'username', 'password', 'last_synced', 'last_updated',
        )


class PublisherSerializer(MasterModelSerializer):
    """
    Every publisher defined by a plugin should have an Publisher serializer that inherits from this
    class. Please import from `pulpcore.plugin.serializers` rather than from this module directly.
    """
    _href = DetailIdentityField()
    name = serializers.CharField(
        help_text=_('A unique name for this publisher.'),
        validators=[UniqueValidator(queryset=models.Publisher.objects.all())]
    )
    last_updated = serializers.DateTimeField(
        help_text=_('Timestamp of the most recent update of the publisher configuration.'),
        read_only=True
    )
    auto_publish = serializers.BooleanField(
        help_text=_('An indication that the automatic publish may happen when'
                    ' the repository content has changed.'),
        required=False
    )
    last_published = serializers.DateTimeField(
        help_text=_('Timestamp of the most recent successful publish.'),
        read_only=True
    )
    distributions = serializers.HyperlinkedRelatedField(
        many=True,
        read_only=True,
        view_name='distributions-detail',
    )

    class Meta:
        abstract = True
        model = models.Publisher
        fields = MasterModelSerializer.Meta.fields + (
            'name', 'last_updated', 'auto_publish', 'last_published', 'distributions',
        )


class ExporterSerializer(MasterModelSerializer):
    _href = DetailIdentityField()
    name = serializers.CharField(
        help_text=_('The exporter unique name.'),
        validators=[UniqueValidator(queryset=models.Exporter.objects.all())]
    )
    last_updated = serializers.DateTimeField(
        help_text=_('Timestamp of the last update.'),
        read_only=True
    )
    last_export = serializers.DateTimeField(
        help_text=_('Timestamp of the last export.'),
        read_only=True
    )

    class Meta:
        abstract = True
        model = models.Exporter
        fields = MasterModelSerializer.Meta.fields + (
            'name',
            'last_updated',
            'last_export',
        )


class DistributionSerializer(ModelSerializer):
    _href = serializers.HyperlinkedIdentityField(
        view_name='distributions-detail'
    )
    name = serializers.CharField(
        help_text=_('The name of the distribution. Ex, `rawhide` and `stable`.'),
        validators=[validators.MaxLengthValidator(
            models.Distribution._meta.get_field('name').max_length,
            message=_('Distribution name length must be less than {} characters').format(
                models.Distribution._meta.get_field('name').max_length
            )),
            UniqueValidator(queryset=models.Repository.objects.all())]
    )
    base_path = serializers.CharField(
        help_text=_('The base (relative) path component of the published url.'),
        validators=[validators.MaxLengthValidator(
            models.Distribution._meta.get_field('base_path').max_length,
            message=_('Distribution base_path length must be less than {} characters').format(
                models.Distribution._meta.get_field('base_path').max_length
            )),
            UniqueValidator(queryset=models.Distribution.objects.all()),
        ],
    )
    http = serializers.BooleanField(
        help_text=_('The publication is distributed using HTTP.'),
    )
    https = serializers.BooleanField(
        help_text=_('The publication is distributed using HTTPS.')
    )
    publisher = DetailRelatedField(
        required=False,
        help_text=_('Publications created by this publisher and repository are automatically'
                    'served as defined by this distribution'),
        queryset=models.Publisher.objects.all(),
    )
    publication = serializers.HyperlinkedRelatedField(
        required=False,
        help_text=_('The publication being served as defined by this distribution'),
        queryset=models.Publication.objects.exclude(complete=False),
        view_name='publications-detail'
    )
    repository = serializers.HyperlinkedRelatedField(
        required=False,
        help_text=_('Publications created by this repository and publisher are automatically'
                    'served as defined by this distribution'),
        queryset=models.Repository.objects.all(),
        view_name='repositories-detail'
    )
    base_url = BaseURLField(
        source='base_path', read_only=True,
        help_text=_('The URL for accessing the publication as defined by this distribution.')
    )

    class Meta:
        model = models.Distribution
        fields = ModelSerializer.Meta.fields + (
            'name', 'base_path', 'http', 'https', 'publisher', 'publication', 'base_url',
            'repository',
        )


class PublicationSerializer(ModelSerializer):
    _href = serializers.HyperlinkedIdentityField(
        view_name='publications-detail'
    )
    publisher = DetailRelatedField(
        help_text=_('The publisher that created this publication.'),
        queryset=models.Publisher.objects.all()
    )
    distributions = serializers.HyperlinkedRelatedField(
        help_text=_('This publication is currently being served as'
                    'defined by these distributions.'),
        many=True,
        read_only=True,
        view_name='distributions-detail',
    )
    created = serializers.DateTimeField(
        help_text=_('Timestamp of when the publication was created.'),
        read_only=True
    )
    repository_version = NestedHyperlinkedRelatedField(
        view_name='versions-detail',
        lookup_field='number',
        parent_lookup_kwargs={'repository_pk': 'repository__pk'},
        read_only=True,
    )

    class Meta:
        model = models.Publication
        fields = ModelSerializer.Meta.fields + (
            'publisher',
            'created',
            'distributions',
            'repository_version',
        )


class RepositoryVersionSerializer(ModelSerializer, NestedHyperlinkedModelSerializer):
    _href = NestedHyperlinkedIdentityField(
        view_name='versions-detail',
        lookup_field='number', parent_lookup_kwargs={'repository_pk': 'repository__pk'},
    )
    _content_href = NestedHyperlinkedIdentityField(
        view_name='versions-content',
        lookup_field='number', parent_lookup_kwargs={'repository_pk': 'repository__pk'},
    )
    _added_href = NestedHyperlinkedIdentityField(
        view_name='versions-added-content',
        lookup_field='number', parent_lookup_kwargs={'repository_pk': 'repository__pk'},
    )
    _removed_href = NestedHyperlinkedIdentityField(
        view_name='versions-removed-content',
        lookup_field='number', parent_lookup_kwargs={'repository_pk': 'repository__pk'},
    )
    number = serializers.IntegerField(
        read_only=True
    )
    created = serializers.DateTimeField(
        help_text=_('Timestamp of creation.'),
        read_only=True
    )
    content_summary = serializers.DictField(
        help_text=_('A list of counts of each type of content in this version.'),
        read_only=True
    )
    add_content_units = serializers.ListField(
        help_text=_('A list of content units to add to a new repository version'),
        write_only=True
    )
    remove_content_units = serializers.ListField(
        help_text=_('A list of content units to remove from the latest repository version'),
        write_only=True
    )

    class Meta:
        model = models.RepositoryVersion
        fields = ('_href', '_content_href', '_added_href', '_removed_href', 'number', 'created',
                  'content_summary', 'add_content_units', 'remove_content_units')