import json, urllib.request, time, statistics
RPC="https://solana-rpc.publicnode.com"; PUMP="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TIPS={"96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5","HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe","Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY","ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49","DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh","ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt","DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL","3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"}
def rpc(m,p):
    r=urllib.request.Request(RPC,json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),{"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
    return json.load(urllib.request.urlopen(r,timeout=60))["result"]
slot=rpc("getSlot",[{"commitment":"confirmed"}])-50
rows=[]
for k in range(0,40,4):
    try: b=rpc("getBlock",[slot-k,{"encoding":"json","maxSupportedTransactionVersion":1,"transactionDetails":"full","rewards":False,"commitment":"confirmed"}])
    except Exception as e: print("blk err",e); time.sleep(5); continue
    if not b: continue
    for tx in b["transactions"]:
        meta=tx["meta"]; msg=tx["transaction"]["message"]
        keys=msg["accountKeys"]+meta.get("loadedAddresses",{}).get("writable",[])+meta.get("loadedAddresses",{}).get("readonly",[])
        if PUMP not in keys or meta["err"]: continue
        logs=" ".join(meta.get("logMessages") or [])
        kind="buy" if "Instruction: Buy" in logs else "sell" if "Instruction: Sell" in logs else "other"
        if kind=="other": continue
        pre,post=meta["preBalances"],meta["postBalances"]
        tip=sum(post[i]-pre[i] for i,a in enumerate(keys[:len(pre)]) if a in TIPS)
        nsig=len(tx["transaction"]["signatures"])
        rows.append(dict(kind=kind,fee=meta["fee"]/1e9,prio=(meta["fee"]-5000*nsig)/1e9,cu=meta.get("computeUnitsConsumed"),jito=tip/1e9))
    time.sleep(2)
json.dump(rows,open("raw/fee_blocks.json","w"))
q=lambda a,p:a[min(len(a)-1,int(len(a)*p))]
for k in ["buy","sell"]:
    r=[x for x in rows if x["kind"]==k]
    if not r: continue
    f=sorted(x["fee"] for x in r); cu=sorted(x["cu"] for x in r if x["cu"]); j=sorted(x["jito"] for x in r if x["jito"]>0)
    tot=sorted(x["fee"]+x["jito"] for x in r)
    print(f"{k}: n={len(r)} network fee SOL p25={q(f,.25):.6f} p50={q(f,.5):.6f} p75={q(f,.75):.6f} p90={q(f,.9):.6f} | CU p50={q(cu,.5)} | txs with Jito tip {len(j)} ({100*len(j)/len(r):.0f}%), tip p50={q(j,.5) if j else 0:.6f} p90={q(j,.9) if j else 0:.6f} | fee+tip p50={q(tot,.5):.6f} p90={q(tot,.9):.6f}")
