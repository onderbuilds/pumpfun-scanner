import json, time, urllib.request
from mintlib import check_account
PUBLIC_RPC="https://api.mainnet-beta.solana.com"
def http_rpc(method, params, url=PUBLIC_RPC, timeout=2.0):
    r=urllib.request.Request(url,json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode(),
        {"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
    return json.load(urllib.request.urlopen(r,timeout=timeout))
class EntryGate:
    def __init__(s, rpc=http_rpc, timeout=2.0, log=None):
        s.rpc=rpc; s.timeout=timeout; s.log=log or (lambda **k: None)
    def check(s, mint):

        t0=time.time()
        try: resp=s.rpc("getAccountInfo",[mint,{"encoding":"jsonParsed","commitment":"confirmed"}],timeout=s.timeout)
        except Exception as e:
            return s._out(mint,False,f'REJECT no_rpc_response ({type(e).__name__})',{},t0)
        if not isinstance(resp,dict) or 'error' in resp or 'result' not in resp:
            return s._out(mint,False,f'REJECT rpc_error ({str(resp.get("error") if isinstance(resp,dict) else resp)[:80]})',{},t0)
        res=resp['result']
        if not isinstance(res,dict) or 'value' not in res:
            return s._out(mint,False,'REJECT malformed_response',{},t0)
        c=check_account(res['value'])
        detail=dict(c,slot=(res.get('context') or {}).get('slot'))
        return s._out(mint,c['ok'],'PASS' if c['ok'] else 'REJECT '+c['reason'],detail,t0)
    def _out(s,mint,ok,reason,detail,t0):
        detail=dict(detail,latency_s=round(time.time()-t0,3)); s.log(mint=mint,action='ENTRY_GATE',ok=ok,reason=reason,detail=detail)
        return ok,reason,detail
if __name__=='__main__':
    import sys; print(EntryGate().check(sys.argv[1]))
