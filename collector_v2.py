import asyncio, json, time, os, gzip, shutil, collections, websockets, threading, urllib.request
from pumpdecode import decode
from mintlib import check_account
PUMP="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RPC_URL=os.environ.get("RPC_WS","wss://api.mainnet-beta.solana.com")
N_CONN=int(os.environ.get("N_CONN","4"))
HTTP_RPC="https://api.mainnet-beta.solana.com"
PP_URL="wss://pumpportal.fun/api/data"
D=os.path.join(os.path.dirname(os.path.abspath(__file__)),"data"); os.makedirs(D,exist_ok=True)
LOG=open(f"{D}/collector.log","a",buffering=1)
def log(*a): LOG.write(time.strftime('%Y-%m-%d %H:%M:%S ')+' '.join(map(str,a))+'\n')
class Out:
    def __init__(s): s.hour=None; s.f=None
    def write(s,obj):
        h=time.strftime('%Y%m%d_%H')
        if h!=s.hour:
            if s.f:
                s.f.close(); old=s.path
                threading.Thread(target=s._gz,args=(old,),daemon=True).start()
            s.hour=h; s.path=f"{D}/events_{h}.jsonl"; s.f=open(s.path,"a",buffering=1<<16); log("rotate ->",s.path)
        s.f.write(json.dumps(obj,separators=(',',':'))+"\n")
    def _gz(s,p):
        try:
            with open(p,'rb') as a, gzip.open(p+'.gz','wb') as b: shutil.copyfileobj(a,b)
            os.remove(p)
        except Exception as e: log("gzip error",p,repr(e))
    def flush(s):
        if s.f: s.f.flush()
OUT=Out()
seen=collections.OrderedDict(); MAXSEEN=300000
up=set(); stats=collections.Counter(); gap_s=0.0
minute_sigs=[]
def dedupe(key,i=0):
    if key in seen: seen[key]|=(1<<i); return False
    seen[key]=(1<<i); minute_sigs.append(key)
    if len(seen)>MAXSEEN: seen.popitem(last=False)
    return True
async def rpc_conn(i):
    await asyncio.sleep(i*4)
    while True:
        t0=time.time(); n=0
        try:
            async with websockets.connect(RPC_URL,max_size=None,ping_interval=20,ping_timeout=30,max_queue=None,open_timeout=15) as ws:
                await ws.send(json.dumps({"jsonrpc":"2.0","id":1,"method":"logsSubscribe","params":[{"mentions":[PUMP]},{"commitment":"confirmed"}]}))
                up.add(i); log(f"rpc[{i}] connected")
                while True:
                    m=json.loads(await asyncio.wait_for(ws.recv(),30)); now=time.time()
                    v=m.get("params",{}).get("result",{}).get("value")
                    if not v: continue
                    n+=1; stats[f"rpc{i}_msgs"]+=1
                    sig=v["signature"]
                    if not dedupe(sig,i): stats["dup_tx"]+=1; continue
                    stats["uniq_tx"]+=1
                    if v.get("err"):
                        stats["failed_tx"]+=1
                        for l in v["logs"]:
                            if "Error Code:" in l: stats["err_"+l.split("Error Code:")[1].split(".")[0].strip()]+=1; break
                        continue
                    slot=m["params"]["result"]["context"]["slot"]
                    for l in v["logs"]:
                        if l.startswith("Program data: "):
                            try: e=decode(l[14:])
                            except Exception: e=None
                            if e:
                                e["rt"]=round(now,3); e["slot"]=slot; e["sig"]=sig; e["src"]=f"rpc{i}"
                                OUT.write(e); stats["events_"+e["e"]]+=1
                                if e["e"]=="C": PENDING.setdefault(e["mint"],[now,0])
        except Exception as ex:
            up.discard(i)
            log(f"rpc[{i}] closed after {time.time()-t0:.1f}s msgs={n} reason={type(ex).__name__}: {str(ex)[:120]}")
            stats["reconnects"]+=1
            await asyncio.sleep(0.5 if time.time()-t0>5 else 5)
PENDING={}
def _gma(mints):
    r=urllib.request.Request(HTTP_RPC,json.dumps({"jsonrpc":"2.0","id":1,"method":"getMultipleAccounts","params":[mints,{"encoding":"jsonParsed","commitment":"confirmed"}]}).encode(),
        {"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
    return json.load(urllib.request.urlopen(r,timeout=10))["result"]
async def live_mint_check():

    backoff=1
    while True:
        await asyncio.sleep(1)
        if not PENDING: continue
        batch=list(PENDING)[:100]; t_req=time.time()
        try: res=await asyncio.to_thread(_gma,batch); backoff=1
        except Exception as ex:
            stats["mintchk_http_err"]+=1; log("mintchk error",type(ex).__name__,str(ex)[:100]); await asyncio.sleep(backoff); backoff=min(backoff*2,30); continue
        now=time.time(); slot=res["context"]["slot"]
        for m,a in zip(batch,res["value"]):
            if not a and PENDING[m][1]<3: PENDING[m][1]+=1; continue
            c=check_account(a); PENDING.pop(m,None)
            OUT.write({"e":"MINTCHK","mint":m,"rt":round(now,3),"req_rt":round(t_req,3),"slot":slot,**c}); stats["mintchk_"+("ok" if c["ok"] else "fail")]+=1
async def pumpportal():
    backoff=1
    while True:
        try:
            async with websockets.connect(PP_URL,ping_interval=20,ping_timeout=30) as ws:
                await ws.send(json.dumps({"method":"subscribeNewToken"})); await ws.send(json.dumps({"method":"subscribeMigration"}))
                log("pumpportal connected"); backoff=1
                async for msg in ws:
                    d=json.loads(msg)
                    if d.get("txType") in ("create","migrate"):
                        OUT.write({"e":"PP","rt":round(time.time(),3),"txType":d["txType"],"mint":d.get("mint"),"pool":d.get("pool"),
                                   "sig":d.get("signature"),"trader":d.get("traderPublicKey"),"initialBuy":d.get("initialBuy"),"mcapSol":d.get("marketCapSol"),
                                   "name":(d.get("name") or "")[:60],"symbol":(d.get("symbol") or "")[:30],"uri":(d.get("uri") or "")[:200]})
                        stats["pp_"+d["txType"]]+=1
        except Exception as ex:
            log("pumpportal closed",type(ex).__name__,str(ex)[:120],"retry in",backoff); await asyncio.sleep(backoff); backoff=min(backoff*2,60)
async def monitor():
    global gap_s
    last=time.time()
    while True:
        await asyncio.sleep(1); now=time.time()
        if not up: gap_s+=now-last; stats["sec_no_rpc_conn"]+=1
        last=now
        if int(now)%10==0:
            json.dump({"ts":now,"local":time.strftime('%F %T'),"rpc_up":sorted(up),"pid":os.getpid()},open(f"{D}/heartbeat.json","w"))
        if int(now)%60==0:
            OUT.flush()

            prev=minute_sigs[:-0 or None]; cnt=collections.Counter(bin(seen.get(k,0)).count("1") for k in prev); minute_sigs.clear()
            per=[sum(1 for k in prev if seen.get(k,0)>>j&1) for j in range(N_CONN)]
            pbar=(sum(per)/N_CONN/len(prev)) if prev else 0
            s=dict(stats); s.update(rpc_up=len(up),seen_by_n_conns=dict(sorted(cnt.items())),per_conn_share=[round(x/len(prev),3) if prev else 0 for x in per],
                    est_union_coverage=round(1-(1-pbar)**N_CONN,4) if pbar else 0)
            OUT.write({"e":"STAT","rt":now,**s})
            log("stat",json.dumps(s)); stats.clear()
async def main():
    log(f"start pid={os.getpid()} N_CONN={N_CONN} url={RPC_URL}")
    await asyncio.gather(monitor(),pumpportal(),live_mint_check(),*[rpc_conn(i) for i in range(N_CONN)])
def _single_instance_lock(path=f"{D}/collector.lock"):

    import fcntl, sys
    f=open(path,"a+")
    try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0); log("REFUSING TO START: another collector holds",path,f.read().strip()); sys.exit(3)
    f.seek(0); f.truncate(); f.write(f"pid={os.getpid()} since={time.strftime('%F %T')}\n"); f.flush()
    return f
if __name__=="__main__":
    _LOCK=_single_instance_lock()
    asyncio.run(main())
