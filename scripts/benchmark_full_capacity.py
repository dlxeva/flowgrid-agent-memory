"""Measure the actual admission scheduler with synthetic fixed backend latency."""
import concurrent.futures
import json
import statistics
import sys
import threading
import time
from types import SimpleNamespace
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aml_retriever.aml_semantic_host import _AdmissionService

def one(concurrency):
    def backend(payload):
        time.sleep(.02)
        return payload['user_id']
    service=_AdmissionService(SimpleNamespace(official_add=backend),capacity=16,concurrency=concurrency)
    gate=threading.Barrier(16)
    def invoke(i):
        gate.wait()
        return service.official_add({'user_id':str(i)})
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        start=time.perf_counter()
        results=list(pool.map(invoke,range(16)))
        elapsed=time.perf_counter()-start
    assert results==list(map(str,range(16)))
    return elapsed

def summary(values):
    ordered=sorted(values)
    return {'samples_seconds':values,'median_seconds':statistics.median(values),
            'p95_seconds':ordered[-1], 'min_seconds':min(values),'max_seconds':max(values),
            'coefficient_of_variation':statistics.stdev(values)/statistics.mean(values)}

if __name__=='__main__':
    for _ in range(2):one(1);one(4)
    before=[];after=[]
    for i in range(7):
        if i%2:after.append(one(4));before.append(one(1))
        else:before.append(one(1));after.append(one(4))
    report={'workload':'actual admission scheduler; 16 distinct tenants; fixed 20ms synthetic backend',
            'warmups':2,'measured_runs':7,'before':summary(before),'after':summary(after),
            'external_network_calls':0,'production_capacity_verified':False}
    report['speedup_ratio']=report['before']['median_seconds']/report['after']['median_seconds']
    report['reduction_percent']=(1-1/report['speedup_ratio'])*100
    print(json.dumps(report,indent=2))
