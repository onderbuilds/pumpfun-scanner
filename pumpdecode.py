import hashlib, struct, base64, base58
def disc(n): return hashlib.sha256(f"event:{n}".encode()).digest()[:8]
D_TRADE, D_CREATE, D_COMPLETE = disc("TradeEvent"), disc("CreateEvent"), disc("CompleteEvent")
pk=lambda b,o: base58.b58encode(b[o:o+32]).decode()
def _str(b,o):
    n=struct.unpack_from("<I",b,o)[0]; return b[o+4:o+4+n].decode("utf-8","replace"), o+4+n
def decode(data_b64):
    b=base64.b64decode(data_b64); d=b[:8]
    if d==D_TRADE and len(b)>=8+32+8+8+1+32+8+32:
        o=8; mint=pk(b,o); o+=32
        sol,tok=struct.unpack_from("<QQ",b,o); o+=16
        buy=b[o]==1; o+=1
        user=pk(b,o); o+=32
        ts,vs,vt,rs,rt=struct.unpack_from("<qQQQQ",b,o); o+=40
        ev=dict(e="T",mint=mint,sol=sol/1e9,tok=tok/1e6,buy=buy,user=user,ts=ts,vsol=vs/1e9,vtok=vt/1e6,rsol=rs/1e9,rtok=rt/1e6)
        try:
            o+=32; fbps,fee=struct.unpack_from("<QQ",b,o); o+=16; creator=pk(b,o); o+=32
            cbps,cfee=struct.unpack_from("<QQ",b,o)
            ev.update(fee_bps=fbps,fee=fee/1e9,creator=creator,cfee_bps=cbps,cfee=cfee/1e9)
        except Exception: pass
        return ev
    if d==D_CREATE:
        o=8; name,o=_str(b,o); sym,o=_str(b,o); uri,o=_str(b,o)
        mint=pk(b,o); o+=32; bc=pk(b,o); o+=32; user=pk(b,o); o+=32
        ev=dict(e="C",mint=mint,name=name[:60],symbol=sym[:30],uri=uri[:200],user=user)
        try: ev["creator"]=pk(b,o); o+=32; ev["ts"]=struct.unpack_from("<q",b,o)[0]
        except Exception: pass
        return ev
    if d==D_COMPLETE:
        return dict(e="G",user=pk(b,8),mint=pk(b,40),ts=struct.unpack_from("<q",b,104)[0] if len(b)>=112 else None)
    return None
