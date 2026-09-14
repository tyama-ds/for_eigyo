"""OPS fixtures follow the official ST.36 exchange-document schema; no network."""
import copy
import unittest
from unittest.mock import patch

import httpx
import patent_search as provider


RESPONSE = b'''<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns:ops="http://ops.epo.org" xmlns="http://www.epo.org/exchange">
 <ops:biblio-search total-result-count="10000"><ops:query syntax="CQL">ta=battery</ops:query>
 <ops:range begin="1" end="25"/><ops:search-result><exchange-documents>
 <exchange-document system="ops.epo.org" family-id="12345" country="EP" doc-number="4000000" kind="A1">
  <bibliographic-data><publication-reference><document-id document-id-type="docdb">
   <country>EP</country><doc-number>4000000</doc-number><kind>A1</kind><date>20220525</date>
  </document-id></publication-reference>
  <classifications-ipcr>
   <classification-ipcr sequence="1"><text>H01M 10/0562 20100101AFI20220525BHEP</text></classification-ipcr>
   <classification-ipcr sequence="2"><section>C</section><class>04</class><subclass>B</subclass><main-group>35</main-group><subgroup>64</subgroup></classification-ipcr>
   <classification-ipcr sequence="3"><text>not-a-code</text></classification-ipcr>
  </classifications-ipcr>
  <parties><applicants>
   <applicant sequence="1" data-format="original"><applicant-name><name>Example Incorporated</name></applicant-name></applicant>
   <applicant sequence="1" data-format="epodoc"><applicant-name><name>EXAMPLE INC [US]</name></applicant-name></applicant>
  </applicants></parties>
  <invention-title lang="de">Feststoffbatterie</invention-title><invention-title lang="en">Solid state battery</invention-title>
  </bibliographic-data><abstract lang="en"><p>A solid electrolyte and <b>interface</b> coating.</p></abstract>
 </exchange-document>
 <exchange-document status="not found" country="EP" doc-number="1" kind="A1"/>
 </exchange-documents></ops:search-result></ops:biblio-search>
</ops:world-patent-data>'''


class OPSSearchTests(unittest.TestCase):
    def setUp(self):
        provider._TOKEN_CACHE.clear()
        self.settings = {'ops_key': 'test-key', 'ops_secret': 'test-secret',
                         'proxy': 'http://proxy.invalid:8080', 'ca_bundle': ''}
        self.calls, self.options = [], []
        self.real_client = httpx.Client

    def client_factory(self, handler):
        def factory(**options):
            self.options.append(options)
            return self.real_client(transport=httpx.MockTransport(handler), trust_env=False)
        return factory

    def handler(self, request):
        self.calls.append(request)
        if request.url.path.endswith('/accesstoken'):
            self.assertEqual(request.method, 'POST')
            self.assertTrue(request.headers['authorization'].startswith('Basic '))
            self.assertEqual(request.content, b'grant_type=client_credentials')
            return httpx.Response(200, json={'access_token': 'test-token', 'expires_in': '1199'})
        self.assertEqual(request.url.host, 'ops.epo.org')
        self.assertEqual(request.headers['authorization'], 'Bearer test-token')
        return httpx.Response(200, content=RESPONSE)

    def test_parse_multilingual_biblio_and_structured_ipc(self):
        result = provider.parse_ops_xml(RESPONSE)
        self.assertEqual(result['total'], 10000)
        self.assertTrue(result['total_is_lower_bound'])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['unavailable_count'], 1)
        record = result['patents'][0]
        self.assertEqual(record['id'], 'EP4000000A1')
        self.assertEqual(record['title'], 'Solid state battery')
        self.assertEqual(record['abstract'], 'A solid electrolyte and interface coating.')
        self.assertEqual(record['ipc'], 'H01M10/0562; C04B35/64')
        self.assertEqual(record['ipc_unparsed'], ['not-a-code'])
        self.assertEqual(record['year'], '2022')
        self.assertEqual(record['family'], record['family_id'])
        self.assertEqual(record['applicant'], 'EXAMPLE INC [US]')
        self.assertEqual(record['fterm'], '')

    def test_requests_use_fixed_endpoint_proxy_and_bounded_range(self):
        before = copy.deepcopy(self.settings)
        with patch.object(provider.httpx, 'Client', self.client_factory(self.handler)):
            first = provider.search_ops('ta = "solid-state battery"', self.settings, 4)
            second = provider.search_ops('ic = "H01M10/0562"', self.settings, 4)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(first['request_count'], 2)
        self.assertEqual(second['request_count'], 1)
        self.assertEqual(self.calls[1].url.params['Range'], '1-4')
        self.assertEqual(self.calls[1].url.params['q'], 'ta = "solid-state battery"')
        self.assertEqual(str(self.calls[0].url), provider.TOKEN_URL)
        self.assertEqual(self.calls[1].url.path, '/3.2/rest-services/published-data/search/biblio')
        self.assertEqual(self.options[0]['proxy'], self.settings['proxy'])
        self.assertFalse(self.options[0]['trust_env'])
        self.assertFalse(self.options[0]['follow_redirects'])
        self.assertEqual(before, self.settings)

    def test_unconfigured_and_invalid_inputs_never_create_client(self):
        with patch.object(provider.httpx, 'Client') as client:
            with self.assertRaisesRegex(ValueError, '未設定'):
                provider.search_ops('ta=battery', {})
            for query, limit in [('', 2), ('x' * 2001, 2), ('ta=battery\n', 2), ('ta=battery', 26), ('ta=battery', True)]:
                with self.subTest(query=query[:20], limit=limit), self.assertRaises(ValueError):
                    provider.search_ops(query, self.settings, limit)
            client.assert_not_called()

    def test_status_reveals_no_credentials(self):
        status = provider.ops_settings_status(self.settings)
        self.assertTrue(status['configured'])
        self.assertNotIn('test-key', str(status))
        self.assertNotIn('test-secret', str(status))

    def test_connection_configuration_error_redacts_proxy(self):
        with patch.object(provider.httpx, 'Client', side_effect=ValueError('invalid proxy-password')):
            with self.assertRaises(ValueError) as error:
                provider.search_ops('ta=battery', self.settings)
            self.assertNotIn('proxy-password', str(error.exception))

    def test_http_errors_do_not_retry_or_echo_provider_secrets(self):
        for status in (401, 403, 429, 503, 302):
            provider._TOKEN_CACHE.clear()
            calls = []
            def reject(request):
                calls.append(request)
                return httpx.Response(status, content=b'test-secret proxy-password', headers={'Location': 'https://other.invalid'})
            with self.subTest(status=status), patch.object(provider.httpx, 'Client', self.client_factory(reject)):
                with self.assertRaises(ValueError) as error:
                    provider.search_ops('ta=battery', self.settings)
                self.assertNotIn('test-secret', str(error.exception))
                self.assertNotIn('proxy-password', str(error.exception))
                self.assertEqual(len(calls), 1)

    def test_bad_xml_and_entities_are_rejected(self):
        for content in (b'<html>proxy sign-in</html>', b'not XML',
                        b'<!DOCTYPE doc [<!ENTITY x "entity">]><doc>&x;</doc>'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                provider.parse_ops_xml(content)

    def test_empty_search_response(self):
        result = provider.parse_ops_xml(b'<ops:biblio-search xmlns:ops="http://ops.epo.org" total-result-count="0"/>')
        self.assertEqual(result['patents'], [])
        self.assertEqual(result['total'], 0)
        self.assertFalse(result['truncated'])

    def test_response_size_cap_before_parsing(self):
        with self.assertRaisesRegex(ValueError, '4MB'):
            provider.parse_ops_xml(b'x' * (provider.MAX_RESPONSE_BYTES + 1))


if __name__ == '__main__':
    unittest.main()
