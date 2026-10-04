"""Build one portable offline overnight collector from the current checkout."""
import base64
import hashlib
import io
from pathlib import Path
import subprocess
import textwrap
import zipfile

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

def build():
    original = (ROOT/'kernelx/library_benchmark.py').read_text()
    start = original.index('    import torch\n', original.index('def _invoke'))
    end = original.index('    # First invocation', start)
    replacement = '''    from .offline_benchmark_setup import setup
    torch, torch_npu, module, function = setup(spec)
    device = spec['device']; lib = spec['library']
'''
    worker = original[:start] + replacement + original[end:]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        sources = {p.relative_to(ROOT).as_posix(): p.read_bytes()
                   for p in (ROOT/'kernelx').rglob('*')
                   if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py','.json','.cpp')}
        sources['kernelx/offline_benchmark.py'] = worker.encode()
        sources['kernelx/offline_benchmark_setup.py'] = (HERE/'benchmark_setup.py').read_bytes()
        sources['kernelx/offline_ci.py'] = (HERE/'ci.py').read_bytes()
        sources['kernelx/offline_ci_worker.py'] = (HERE/'ci_worker.py').read_bytes()
        ci_root = HERE/'ci_sources'
        if not (ci_root/'sources.json').is_file():
            raise RuntimeError('CI source snapshots are required; missing '+str(ci_root))
        for path in ci_root.rglob('*'):
            if path.is_file():
                sources['kernelx/offline_ci_sources/'+path.relative_to(ci_root).as_posix()] = path.read_bytes()
        for name, value in sorted(sources.items()):
            info = zipfile.ZipInfo(name, date_time=(2026,1,1,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
    payload = buffer.getvalue()
    commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    template = (HERE/'driver.py').read_text()
    encoded = '\n'.join(textwrap.wrap(base64.b64encode(payload).decode(),100))
    script = template.replace('__PAYLOAD__',encoded).replace('__SHA256__',hashlib.sha256(payload).hexdigest()).replace('__COMMIT__',commit)
    output = ROOT/'deploy/offline/kernelx_overnight.py'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(script)
    print(str(output), output.stat().st_size, 'bytes')
    return output

if __name__ == '__main__':
    build()
