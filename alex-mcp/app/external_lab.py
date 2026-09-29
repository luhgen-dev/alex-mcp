#!/usr/bin/env python3
from __future__ import annotations

"""External Alex engineering lab.

Runs Alex certification outside Home Assistant/WhatsApp. Every non-provider stage
is bound to disposable paths. Paid provider runs are opt-in and can be narrowed to
specific contracts so repair loops do not waste tokens.
"""

import argparse, json, os, subprocess, sys, tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

APP = Path(__file__).resolve().parent
ROOT = APP.parent.parent
CERT = APP / "behavior_cert.py"
KNOWN = APP / "behavior_known_failures.json"

def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def dump(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")

def contract_args(ids):
    out=[]
    for cid in ids: out += ["--contract", cid]
    return out

def child(cmd, env, out, err):
    r=subprocess.run(cmd,cwd=str(ROOT),env=env,text=True,capture_output=True,check=False)
    Path(out).write_text(r.stdout,encoding="utf-8")
    Path(err).write_text(r.stderr,encoding="utf-8")
    return r.returncode

def bucket(problem):
    p=str(problem).lower()
    if any(x in p for x in ("private","privacy","leak")): return "privacy"
    if any(x in p for x in ("attachment","original","queued")): return "attachment"
    if any(x in p for x in ("step budget","max_steps","exhausted")): return "tool_loop"
    if "required capability" in p or "required-all" in p: return "routing"
    if "state expectation" in p or "durable household-state" in p: return "state"
    if "tamil script" in p or "english output" in p: return "language"
    if "latency" in p: return "latency"
    if "expected term" in p or "contradictory phrase" in p: return "answer"
    return "other"

def failures(report):
    for row in report.get("prompt_results",[]):
        if row.get("status")=="FAIL":
            yield {"contract":row.get("contract"),"phase":row.get("phase"),"domain":row.get("domain"),"source":row.get("source"),"prompt":row.get("prompt"),"problems":row.get("problems",[]),"trace":row.get("trace",{}),"durable_state_diff":row.get("durable_state_diff",{}),"elapsed_ms":row.get("elapsed_ms")}
    for conv in report.get("conversation_results",[]):
        if conv.get("status")!="FAIL": continue
        for step in conv.get("steps",[]):
            if step.get("status")=="FAIL":
                yield {"contract":conv.get("contract"),"phase":conv.get("phase"),"domain":conv.get("domain"),"source":conv.get("source"),"step":step.get("step"),"prompt":step.get("prompt"),"problems":step.get("problems",[]),"trace":step.get("trace",{}),"durable_state_diff":step.get("durable_state_diff",{}),"elapsed_ms":step.get("elapsed_ms")}

def triage_report(report):
    rows=list(failures(report)); domains=Counter(); buckets=Counter(); queue=[]
    for row in rows:
        domains[row.get("domain") or "unknown"] += 1
        bs=sorted({bucket(p) for p in row.get("problems",[])})
        buckets.update(bs)
        calls=(row.get("trace") or {}).get("calls") or []
        queue.append({**row,"problem_buckets":bs,"tools_called":[c.get("tool") for c in calls if isinstance(c,dict) and c.get("tool")]})
    priority={"privacy":0,"state":1,"attachment":1,"routing":2,"tool_loop":3,"language":4,"answer":4,"latency":5,"other":6}
    queue.sort(key=lambda r:(min([priority.get(x,9) for x in r["problem_buckets"]] or [9]),str(r.get("contract")),int(r.get("step") or 0)))
    return {"source_status":report.get("status"),"source_summary":report.get("summary",{}),"failure_count":len(rows),"failures_by_domain":dict(domains),"problem_buckets":dict(buckets),"repair_queue":queue,"manual_gates_not_claimed":report.get("manual_gates_not_claimed",[])}

def certify(a):
    out=Path(a.report_dir).resolve(); out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="alex-external-lab-") as tmp:
        safe=Path(tmp); data=safe/"data"; data.mkdir(); options=safe/"options.json"; options.write_text("{}\n")
        env=os.environ.copy(); env.update({"ALEX_DATA_DIR":str(data),"ALEX_OPTIONS_PATH":str(options),"ALEX_HA_API_URL":"http://127.0.0.1:9/external-lab-no-ha","ALEX_CERT_NOW":"2026-09-29T02:00:00+00:00"}); env.pop("SUPERVISOR_TOKEN",None)
        cp=out/"catalog.json"
        rc1=child([sys.executable,str(CERT),"--mode","catalog","--report",str(cp),"--no-fail-exit"],env,out/"catalog.log",out/"catalog.err")
        catalog=load(cp) if cp.exists() else {"status":"ERROR"}
        op=out/"offline.json"
        cmd=[sys.executable,str(CERT),"--mode","offline","--phase",a.phase,"--report",str(op),"--no-fail-exit"]+contract_args(a.contract)
        rc2=child(cmd,env,out/"offline.log",out/"offline.err")
        offline=load(op) if op.exists() else {"status":"ERROR","failure_signatures":[]}
        known=set(load(KNOWN).get("failure_signatures",[])) if KNOWN.exists() else set(); current=set(offline.get("failure_signatures",[]))
        stages={"catalog":{"status":catalog.get("status"),"returncode":rc1},"offline":{"status":offline.get("status"),"returncode":rc2,"new_failure_signatures":sorted(current-known),"resolved_known_failure_signatures":sorted(known-current)}}
        live=None
        if a.live:
            lp=out/"live.json"
            cmd=[sys.executable,str(CERT),"--mode","live","--phase",a.phase,"--provider",a.provider,"--source-options",a.source_options,"--max-live-cost-usd",str(a.max_live_cost_usd),"--report",str(lp),"--no-fail-exit"]+contract_args(a.contract)
            rc3=child(cmd,env,out/"live.log",out/"live.err")
            live=load(lp) if lp.exists() else {"status":"ERROR","summary":{}}
            stages["live"]={"status":live.get("status"),"returncode":rc3,"summary":live.get("summary",{})}
            dump(out/"triage.json",triage_report(live))
    lab_ok=(rc1==0 and rc2==0 and catalog.get("status")=="PASS")
    regression_ok=not stages["offline"]["new_failure_signatures"]
    product_ok=offline.get("status")=="PASS" and (live is None or live.get("status")=="PASS")
    manifest={"mode":"external_lab","generated_at_utc":datetime.now(timezone.utc).isoformat(),"phase":a.phase,"contracts":a.contract,"live_enabled":a.live,"lab_health":"PASS" if lab_ok else "FAIL","regression_status":"PASS" if regression_ok else "FAIL","product_readiness":"PASS" if product_ok else "NOT_CERTIFIED","stages":stages,"manual_boundary":"Real WhatsApp transport/session/rendering and physical Home Assistant effects remain final manual gates."}
    dump(out/"manifest.json",manifest); print(json.dumps(manifest,indent=2))
    if not lab_ok or not regression_ok: raise SystemExit(1)
    if a.strict_product and not product_ok: raise SystemExit(2)
    return manifest

def triage(a):
    out=Path(a.report_dir); out.mkdir(parents=True,exist_ok=True)
    result=triage_report(load(a.input)); dump(out/"triage.json",result)
    print(json.dumps({"failure_count":result["failure_count"],"problem_buckets":result["problem_buckets"]},indent=2)); return result

def parser():
    p=argparse.ArgumentParser(description="Run Alex outside Home Assistant as an engineering lab"); sub=p.add_subparsers(dest="command",required=True)
    c=sub.add_parser("certify"); c.add_argument("--phase",choices=("all","phase1","phase2","phase3"),default="all"); c.add_argument("--contract",action="append",default=[]); c.add_argument("--report-dir",default=str(ROOT/".alex-lab")); c.add_argument("--live",action="store_true"); c.add_argument("--provider",choices=("auto","gemini","grok","openai"),default="auto"); c.add_argument("--source-options",default=os.environ.get("ALEX_CERT_SOURCE_OPTIONS","/data/options.json")); c.add_argument("--max-live-cost-usd",type=float,default=0.25); c.add_argument("--strict-product",action="store_true")
    t=sub.add_parser("triage"); t.add_argument("--input",required=True); t.add_argument("--report-dir",default=str(ROOT/".alex-lab/triage"))
    return p

def main():
    a=parser().parse_args(); return certify(a) if a.command=="certify" else triage(a)

if __name__=="__main__": main()
