from unittest import TestCase, mock

from pulpcore.app import settings as pulp_settings
from pulpcore.tasking.connection import get_redis_connection


class TestMergeSettings(TestCase):

    def test_no_override_config(self):
        """Assert the default config is returned when no override config exists"""
        self.assertEqual({'key': 'value'}, pulp_settings.merge_settings({'key': 'value'}, {}))

    def test_no_overlap(self):
        """Assert that merging two dicts with no shared keys works"""
        default = {'default_1': 'value', 'default_2': 'value'}
        override = {'1': 'value', '2': 'value'}
        expected = {'default_1': 'value', 'default_2': 'value', '1': 'value', '2': 'value'}

        merged = pulp_settings.merge_settings(default, override)
        self.assertEqual(merged, expected)

    def test_simple(self):
        """Assert that merging two dicts with shared keys causes the override to be applied"""
        default = {'key1': 'value', 'key2': 'value'}
        override = {'key1': 'Circus of values', 'key3': 'value'}
        expected = {'key1': 'Circus of values', 'key2': 'value', 'key3': 'value'}

        merged = pulp_settings.merge_settings(default, override)
        self.assertEqual(merged, expected)

    def test_recursive(self):
        """Assert that merging two dicts with shared keys causes the override to be applied"""
        default = {'key1': {'subkey': 'value', 'subkey2': 'value'}}
        override = {'key1': {'subkey': 'VALUE'}}
        expected = {'key1': {'subkey': 'VALUE', 'subkey2': 'value'}}

        merged = pulp_settings.merge_settings(default, override)
        self.assertEqual(merged, expected)


class TestLoadSettings(TestCase):

    def test_no_settings_files(self):
        """Assert the default settings are used if no setting files are provided"""
        settings = pulp_settings.load_settings()

        self.assertEqual(settings, pulp_settings._DEFAULT_PULP_SETTINGS)

    def test_redis_connection(self):
        """Assert if Redis get_redis_connection returns valid parameters from server.yaml"""
        settings = pulp_settings.load_settings()
        redis_conn = get_redis_connection().connection_pool

        self.assertEqual(settings['REDIS']['HOST'], redis_conn.connection_kwargs['host'])
        self.assertEqual(settings['REDIS']['PORT'], redis_conn.connection_kwargs['port'])
        self.assertEqual(settings['REDIS']['PASSWORD'], redis_conn.connection_kwargs['password'])

    def test_settings_file(self):
        """Assert loading a file merges the file settings with the default"""
        override = """
        ALLOWED_HOSTS:
          - subdomains.all.the.way.down.www.example.com
        """
        expected = pulp_settings._DEFAULT_PULP_SETTINGS.copy()
        expected['ALLOWED_HOSTS'] = ['subdomains.all.the.way.down.www.example.com']

        mocked_open = mock.mock_open(read_data=override)
        with mock.patch('pulpcore.app.settings.open', mocked_open, create=True):
            settings = pulp_settings.load_settings(['somefile'])

        self.assertEqual(settings, expected)
        mocked_open.assert_called_with('somefile')
