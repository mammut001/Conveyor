"""Operator-defined immutable source probes shared by discovery and verification."""
from conveyor.automation.overnight_process import command,CommandFailure


class SourceProbes:
    def __init__(self,workspaces,probes):
        if len(probes)>10:
            raise ValueError('at most ten source probes are supported')
        for probe in probes:
            if (not probe.get('name') or not probe.get('reproduction') or not probe.get('argv')
                    or any(not isinstance(arg,str) or '\0' in arg for arg in probe['argv'])):
                raise ValueError('each source probe needs name, reproduction and fixed argv')
        self.workspaces=workspaces;self.probes=probes

    async def discover(self,payload):
        if not self.probes:
            return []
        findings=[]
        async with self.workspaces.detached(payload['revision']) as path:
            for probe in self.probes:
                failed=True
                for _ in range(2):
                    try:
                        await command(probe['argv'],cwd=path,timeout=120,sandboxed=True)
                        failed=False
                        break
                    except CommandFailure:
                        pass
                if failed:
                    findings.append({'scenario':probe['name'],'reproduction':probe['reproduction'],
                                     'evidence':f"Independent coordinator-owned probe failed twice at {payload['revision']}."})
        return findings

    def verification_commands(self):
        return [list(probe['argv']) for probe in self.probes]
