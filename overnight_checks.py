"""Deterministic preview acceptance checks, usable by discovery and verification."""
from __future__ import annotations
import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    name: str
    path: str
    expected_status: int = 200
    json_fields: tuple = ()
    contains: str = ''


DEFAULT_SCENARIOS = (
    Scenario('Workbench page loads', '/', contains='<html'),
    Scenario('Authenticated service health', '/api/health', json_fields=(('ok',True),('schema_version',1))),
    Scenario('Authenticated sessions endpoint', '/api/sessions'),
)


class PreviewChecks:
    def __init__(self, base_url, token, *, scenarios=DEFAULT_SCENARIOS, timeout=10):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or parsed.port != 18790 or parsed.path not in ('','/')
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('checks must target the isolated local preview port')
        if len(token) < 32 or not 0 < timeout <= 30:
            raise ValueError('invalid preview credentials or deadline')
        for scenario in scenarios:
            if scenario.path not in {'/','/api/health','/api/sessions'}:
                raise ValueError('only approved read-only scenarios can run')
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.scenarios = scenarios
        self.timeout = timeout

    def check(self, scenario):
        req = urllib.request.Request(self.base_url+scenario.path,
                                     headers={'Authorization':'Bearer '+self.token})
        # Refuse redirects: bearer credentials must never reach another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(req,timeout=self.timeout) as response:
                code = response.status
                body = response.read(1_000_001)
        except urllib.error.HTTPError as exc:
            return {'passed':False,'reason':f'HTTP {exc.code}'}
        except (OSError,ValueError):
            return {'passed':False,'reason':'preview request unavailable'}
        if code != scenario.expected_status or len(body) > 1_000_000:
            return {'passed':False,'reason':'unexpected response status or size'}
        text = body.decode('utf-8',errors='replace')
        if scenario.contains and scenario.contains not in text.lower():
            return {'passed':False,'reason':'required page content missing'}
        if scenario.json_fields:
            try:
                data = json.loads(text)
                if not isinstance(data,dict) or any(data.get(key) != value for key,value in scenario.json_fields):
                    return {'passed':False,'reason':'health response does not satisfy contract'}
            except ValueError:
                return {'passed':False,'reason':'invalid JSON response'}
        return {'passed':True}

    async def discover(self, payload):
        findings = []
        for scenario in self.scenarios:
            first = await asyncio.to_thread(self.check,scenario)
            if first['passed']:
                continue
            # Only reproducible failures become issues. Do not include response bodies
            # or authorization headers in evidence.
            second = await asyncio.to_thread(self.check,scenario)
            if second['passed']:
                continue
            findings.append({'scenario':scenario.name,
                             'reproduction':f'GET {scenario.path}: expected HTTP {scenario.expected_status} and scenario contract',
                             'evidence':json.dumps({'first':first,'second':second})})
        return findings
