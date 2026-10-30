"""Prepare a new no-cap ledger without changing the old ledger or live config.

--check is read-only (the default). --prepare requires systemd service stopped.
The candidate config must use an independently verified new runtime identity.
No credentials are read, no provider requests are made, no service is restarted.
"""
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aml_retriever.aml_model_budget import BudgetPolicy
from aml_retriever.aml_semantic_host import SemanticHostConfig
from aml_retriever.aml_hosted import HostedConfig

SERVICE='flowgrid-memory-aml.service'

def inspect(source):
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as con:
        con.execute('BEGIN')
        binding=json.loads(con.execute('SELECT binding FROM policy WHERE id=1').fetchone()[0])
        digest=hashlib.sha256();count=amount=0
        for row in con.execute('SELECT * FROM calls ORDER BY token'):
            if row[2]!='settled':raise ValueError('unresolved_calls_retained_stop')
            digest.update((json.dumps(row,separators=(',',':'))+'\n').encode())
            count+=1;amount+=row[3]
        if con.execute('SELECT 1 FROM halt').fetchone():raise ValueError('halt_retained_stop')
        return binding,(count,amount,digest.hexdigest())

def proposal(data,source,target,runtime,concurrency):
    binding,receipt=inspect(source)
    old=BudgetPolicy(**binding)
    if json.loads(old.binding)!=json.loads(BudgetPolicy(**data['policy']).binding):
        raise ValueError('config_ledger_binding')
    new=replace(old,scope='aml-full-capacity-'+runtime[:12],
                implementation_sha256=runtime,budget_microusd=None,max_attempts=None,
                max_concurrent_calls=concurrency,isolate_unknown_calls=True)
    candidate=json.loads(json.dumps(data))
    candidate['policy']=json.loads(new.binding)
    candidate['semantic'].update(budget_db_path=str(target),model_concurrency=concurrency,
                                 isolate_provider_failures=True)
    h=HostedConfig(credential='synthetic-precheck',**candidate['hosted'])
    SemanticHostConfig(policy=new,**candidate['semantic']).validate(h)
    return candidate,receipt

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--target-ledger',type=Path,required=True)
    p.add_argument('--new-runtime-sha256',required=True)
    p.add_argument('--concurrency',type=int,default=4,choices=(2,4,8))
    p.add_argument('--prepare',action='store_true')
    p.add_argument('--candidate-config',type=Path)
    a=p.parse_args()
    data=json.loads(a.config.read_text())
    if 'credential' in data['hosted']:raise ValueError('inline_credential_rejected')
    source=Path(data['semantic']['budget_db_path'])
    target=a.target_ledger
    if (not source.is_absolute() or source!=source.resolve() or
        not target.is_absolute() or target!=target.resolve() or
        source.parent!=target.parent or target.exists()):
        raise ValueError('fresh_real_ledger_same_directory_required')
    candidate,receipt=proposal(data,source,target,a.new_runtime_sha256,a.concurrency)
    print('MIGRATION_CHECK_PASS:',json.dumps({'attempts':receipt[0],'charged_microusd':receipt[1],
        'fee_cap_enabled':False,'call_cap_enabled':False,'concurrency':a.concurrency}))
    if not a.prepare:return
    if not a.candidate_config or a.candidate_config.exists():raise ValueError('fresh_candidate_config_required')
    state=subprocess.check_output(['systemctl','show',SERVICE,'--property=ActiveState','--property=MainPID'],text=True)
    props=dict(line.split('=',1) for line in state.splitlines())
    if props!={'ActiveState':'inactive','MainPID':'0'}:raise ValueError('service_must_be_stopped')
    if inspect(source)[1]!=receipt:raise ValueError('ledger_changed')
    fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
        dst.execute('UPDATE policy SET binding=? WHERE id=1',
                    (BudgetPolicy(**candidate['policy']).binding,))
        dst.commit()
    if inspect(source)[1]!=receipt or inspect(target)[1]!=receipt:
        raise ValueError('receipt_copy_mismatch_retain_files')
    owner=source.stat();os.chown(target,owner.st_uid,owner.st_gid)
    fd=os.open(a.candidate_config,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as out:
        json.dump(candidate,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
    print('LEDGER_COPY_PREPARED; old ledger unchanged; receipts copied; live config unchanged; no service start')

if __name__=='__main__':
    try:main()
    except Exception as e:
        print('STOP:',str(e) if type(e) is ValueError and str(e).replace('_','').isalnum() else type(e).__name__)
        raise SystemExit(1)
