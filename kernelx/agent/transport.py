"""HTTPS at-least-once transport; server must honor the durable receipt contract."""
import json
import os
import tarfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from .storage import verify_bundle
from ..protocol import digest


class HTTPTransport:
    def __init__(self,url,token_env=None,timeout=15):
        parsed=urlparse(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('HTTPS upload endpoint required; credentials belong in an environment reference')
        self.url=url; self.token_env=token_env; self.timeout=timeout

    def __call__(self,source):
        source=Path(source); manifest=verify_bundle(source)
        directory=source.parent.parent/'uploads'; directory.mkdir(exist_ok=True)
        archive=directory/(manifest['bundle_id']+'.tar')
        # Send a bounded stream from disk; never load the full PROF into memory.
        if not archive.exists():
            temporary=archive.with_suffix('.tmp')
            with tarfile.open(temporary,'w') as tar:
                for row in manifest['files']: tar.add(source/row['path'],arcname=row['path'],recursive=False)
                tar.add(source/'sealed.json',arcname='sealed.json',recursive=False)
            os.replace(temporary,archive)
        headers={'Content-Type':'application/x-tar','Content-Length':str(archive.stat().st_size),
                 'X-KernelX-Bundle-ID':manifest['bundle_id'],'X-KernelX-Manifest-SHA256':digest(manifest)}
        if self.token_env:
            token=os.environ.get(self.token_env)
            if not token: raise ValueError('upload credential environment reference is empty')
            headers['Authorization']='Bearer '+token
        with archive.open('rb') as body:
            request=urllib.request.Request(self.url,data=body,headers=headers,method='POST')
            with urllib.request.urlopen(request,timeout=self.timeout) as response:
                data=response.read(65537)
                if len(data)>65536: raise ValueError('oversized receipt')
                return json.loads(data)
