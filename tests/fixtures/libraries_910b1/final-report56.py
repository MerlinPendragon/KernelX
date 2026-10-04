import json
from pathlib import Path
from kernelx.efficiency import generate
from kernelx.agent.fleet import Fleet
root=Path('/home/lxb/kernelx-issues56');index=json.loads((root/'pilot-index.json').read_text());bundles=[Path(p) for p in index['bundles']];fleet=Fleet(root/'control')
for row in fleet.db.execute('SELECT report FROM dispatches WHERE state=?',('FAILED',)):
 for attempt in json.loads(row['report'])['attempts']:bundles.append(Path(attempt['output']))
fleet.close()
print(generate(bundles,json.loads((root/'inventory.json').read_text()),root/'report-final',root/'control',root/'center'))
report=json.loads((root/'report-final/report.json').read_text())
print(json.dumps(dict(resources=report['resources'],progress=report['progress'],coverage=report['coverage'][0],stability=report['stability'],prediction_error=report['prediction_error']),indent=2))
