import unittest
from unittest.mock import patch
from conveyor.automation.overnight_checks import PreviewChecks, Scenario


class PreviewCheckTests(unittest.IsolatedAsyncioTestCase):
    def test_production_and_redirect_target_cannot_be_configured(self):
        for url in ['http://127.0.0.1:8787','https://example.com','http://127.0.0.1:18790@evil.com','http://127.0.0.1:18790/?next=x']:
            with self.assertRaises(ValueError):
                PreviewChecks(url,'t'*48)

    async def test_transient_failure_does_not_create_finding(self):
        checks=PreviewChecks('http://127.0.0.1:18790','t'*48,scenarios=(Scenario('health','/api/health'),))
        with patch.object(checks,'check',side_effect=[{'passed':False},{'passed':True}]):
            self.assertEqual(await checks.discover({}),[])

    async def test_reproducible_failure_contains_no_secret(self):
        checks=PreviewChecks('http://127.0.0.1:18790','secret-token-'*4,scenarios=(Scenario('health','/api/health'),))
        with patch.object(checks,'check',return_value={'passed':False,'reason':'HTTP 500'}):
            result=await checks.discover({})
        self.assertEqual(len(result),1)
        self.assertNotIn(checks.token,str(result))
        self.assertIn('GET /api/health',result[0]['reproduction'])
