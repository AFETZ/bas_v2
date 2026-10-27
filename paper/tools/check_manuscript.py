#!/usr/bin/env python3
"""Small artifact checks; no simulator, no invented scientific validation."""
from pathlib import Path
from zipfile import ZipFile
import csv
import re
import json
import hashlib
import subprocess
import argparse

ROOT=Path(__file__).resolve().parents[1]
def rows(name):
    with (ROOT/'data'/name).open(newline='') as f:return list(csv.DictReader(f))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--final',action='store_true',help='Reject planned-result placeholders')
    args=parser.parse_args()
    source=(ROOT/'src/manuscript.md').read_text()
    english=re.search(r'^@abstract_en (.+)$',source,re.M).group(1)
    assert 200<=len(english.split())<=300,len(english.split())
    keys=[re.search(r'^@'+k+r' (.+)$',source,re.M).group(1).split(';') for k in ['keywords','keywords_en']]
    assert len(keys[0])==len(keys[1]) and len(keys[0])<=10
    pending=re.findall(r'^@pending (\S+)',source,re.M)
    if args.final:assert not pending,('Results still pending',pending)
    cited={int(n) for group in re.findall(r'\[([\d, ]+)\]',source) for n in group.split(',')}
    bibliography={int(n) for n in re.findall(r'^\[(\d+)\]',(ROOT/'refs/references.txt').read_text(),re.M)}
    assert cited==bibliography,(cited,bibliography)
    cells=rows('six_cells.csv')
    assert len(cells)==6 and sum(int(r['runs']) for r in cells)==30
    assert sum(int(r['offered_total']) for r in cells)==27000
    assert all(int(r['runs'])==5 and int(r['errors'])==0 for r in cells)
    decomp=[r for r in rows('decomposition.csv') if r['group']=='all_five']
    for policy,expected in [('fixed',67.7147237686),('failover',21.302193034)]:
        selected=[r for r in decomp if r['policy']==policy]
        assert len(selected)==5 and all(r['status']=='observed' for r in selected)
        assert abs(sum(float(r['fault_to_completion_s']) for r in selected)/5-expected)<1e-8
        for r in selected:
            assert abs(sum(float(r[k]) for k in ['fault_to_goal_creation_s','goal_creation_to_first_UART_s',
                        'first_UART_to_completion_s'])-float(r['fault_to_completion_s']))<1e-8
    document=ROOT/'build/manuscript_anonymous.docx'
    manifest=json.loads((ROOT/'build/build_manifest.json').read_text())
    for name,digest in manifest['inputs_sha256'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,('stale DOCX input',name)
    assert hashlib.sha256(document.read_bytes()).hexdigest()==manifest['docx_sha256']
    with ZipFile(document) as z:
        assert not any('vba' in n.lower() or 'comments' in n.lower() for n in z.namelist())
        xml=b'\n'.join(z.read(n) for n in z.namelist() if n.endswith(('.xml','.rels')))
        assert not any(token in xml for token in [b'/home/',b'file:///home/',b'Ivanov',b'Petrov',
                   'Иванов'.encode(),'Петров'.encode(),b'attachedTemplate'])
    pdf=ROOT/'build/manuscript_anonymous.pdf'
    assert pdf.stat().st_mtime_ns>=document.stat().st_mtime_ns,'PDF predates DOCX'
    info=subprocess.check_output(['pdfinfo',str(pdf)],text=True)
    pages=int(re.search(r'^Pages:\s*(\d+)',info,re.M).group(1))
    assert 10<=pages<=12,pages
    assert not re.search(r'^Author:\s*\S',info,re.M)
    text=subprocess.check_output(['pdftotext',str(pdf),'-'],text=True)
    assert all(x not in text for x in ['/home/','Иванов','Петров','Степанянц','Зорошилов','Нархов'])
    assert all(x in text for x in ['32,511','21,302','3,963','14,973','разведоч'])
    result={'checks':'passed','pages':pages,'run_count':30,'offered':27000,'paired_n':5,
            'technical_sources':sorted(cited),'anonymous_pdf_author_empty':True,
            'docx_macro_free':True,'font_substitution':'Liberation Serif/Sans in PDF; TNR/Arial names in DOCX',
            'pending_results':pending,
            'docx_sha256':hashlib.sha256(document.read_bytes()).hexdigest(),
            'pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest()}
    (ROOT/'build/artifact_checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
