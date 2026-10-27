#!/usr/bin/env python3
"""Small artifact checks; no simulator, no invented scientific validation."""
from pathlib import Path
from zipfile import ZipFile
from PIL import Image
import csv
import re
import json
import hashlib
import subprocess
import argparse
from xml.etree import ElementTree as ET

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
    figure_blocks=re.findall(r'^@figure (\S+) ([\d.]+)\n@caption (.+)\n@caption_en (.+)$',source,re.M)
    assert len(figure_blocks)==len(re.findall(r'^@figure ',source,re.M)),'Bilingual figure caption missing'
    for name,width,ru,en in figure_blocks:
        assert float(width)<=14 and ru.startswith('Рис. ') and en.startswith('Fig. ')
        assert ru.endswith('.') and en.endswith('.')
        with Image.open(ROOT/'figures'/f'{name}.png') as img:
            dpi=img.info.get('dpi',(0,0));effective_dpi=img.width/(float(width)/2.54)
            assert min(dpi)>=299 and effective_dpi>=300,(name,dpi,effective_dpi)
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
    operator=json.loads((ROOT/'data/operator03_summary.json').read_text())
    assert operator['flight_uav_ids']==[3] and operator['other_four_grounded']
    for stage in operator['parameters'].values():
        assert len(stage['components'])==5
        assert all(c['distinct_valid_indices']==1388 and not c['missing_indices'] for c in stage['components'])
    assert len(operator['operator_commands'])==6 and operator['before_after_changes']==54
    assert all(c['all_five_stages_same_hash'] and c['uart_inside_lease'] for c in operator['operator_commands'])
    sensitivity=rows('sensitivity_runs.csv')
    assert len(sensitivity)==18 and len({r['run_id'] for r in sensitivity})==18
    assert sum(r['run_status']=='failed' for r in sensitivity)==3
    assert all(r['network_eligible']=='True' and r['measurement_window_complete']=='True' for r in sensitivity)
    changed=[r for r in sensitivity if float(r['handover_changed'])==1]
    assert len(changed)==6 and sum(float(r['handover_by4s_where_changed'])==1 for r in changed)==4
    assert sorted(round(float(r['common_UART_recovery_s']),9) for r in changed if float(r['common_UART_recovery_s'])>4)==[4.123131166,4.134810747]
    independent=json.loads((ROOT/'data/sensitivity_independent_summary.json').read_text())
    assert independent['parameter_tables']=={'complete':165,'partial':10} and independent['unrecorded_tables']==5
    assert independent['independent_UART_physical_RF_endpoints_match']
    for name,record in json.loads((ROOT/'data/followup_provenance.json').read_text())['files'].items():
        assert hashlib.sha256((ROOT/'data'/name).read_bytes()).hexdigest()==record['paper_sha256'],name
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
        ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        body=list(ET.fromstring(z.read('word/document.xml')).find('w:body',ns))
        ptext=lambda node:''.join(t.text or '' for t in node.findall('.//w:t',ns))
        figures=tables=0
        for index,node in enumerate(body):
            if node.tag.endswith('}tbl'):
                tables+=1
                assert ptext(body[index-2]).startswith(f'Табл. {tables}.')
                assert ptext(body[index-1]).startswith(f'Table {tables}.')
                assert body[index-1].find('w:pPr/w:jc',ns).get('{'+ns['w']+'}val')=='left'
            elif node.find('.//w:drawing',ns) is not None:
                figures+=1
                assert ptext(body[index+1]).startswith(f'Рис. {figures}.')
                assert ptext(body[index+2]).startswith(f'Fig. {figures}.')
        assert figures==len(figure_blocks)
        assert tables==len(re.findall(r'^@table ',source,re.M))
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
            'sensitivity_attempted':18,'sensitivity_completed':15,'sensitivity_failed':3,
            'sensitivity_pairs_per_nominal_duration':3,'changed_relay_by4s':4,'changed_relay_total':6,
            'technical_sources':sorted(cited),'anonymous_pdf_author_empty':True,
            'docx_macro_free':True,'font_substitution':'Liberation Serif/Sans in PDF; TNR/Arial names in DOCX',
            'pending_results':pending,
            'docx_sha256':hashlib.sha256(document.read_bytes()).hexdigest(),
            'pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest()}
    (ROOT/'build/artifact_checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
