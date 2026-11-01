import os, sys, json, time, glob, gzip, fcntl, random, hashlib, collections, signal
ROOT=os.path.dirname(os.path.abspath(__file__)); os.chdir(ROOT); sys.path.insert(0,ROOT)
import analyze as A
from risk import RiskManager, Oversize
from entry_gate import EntryGate
P=os.environ.get('PAPER_DIR',os.path.join(ROOT,'paper')); DATA=os.environ.get('PAPER_DATA',os.path.join(ROOT,'data'))
RULE_NAMES=os.environ.get('PAPER_RULES','A_snipe_slot+8,B_filtered_60s,C_momentum_60pct').split(',')
TXC=float(os.environ.get('PAPER_TXC','0.000105')); PFAIL=float(os.environ.get('PAPER_PFAIL',str(A.CFG['tx_fail_prob_scenarios'][0])))
R=A.CFG['max_tx_retries']; SIZE_CAP_SOL=A.CFG['bankroll_usd']/A.CFG['sol_usd_fallback']
WINDOW=1800; SHADOW_EVERY=20
def now_local(): return time.strftime('%F %T')
class Bot:
    def __init__(s,gate=None):
        os.makedirs(f'{P}/state',exist_ok=True)
        s.lockf=open(f'{P}/paper_bot.lock','a+')
        try: fcntl.flock(s.lockf,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: print('another paper_bot holds the lock'); sys.exit(3)
        s.lockf.seek(0); s.lockf.truncate(); s.lockf.write(f'pid={os.getpid()} since={now_local()}\n'); s.lockf.flush()
        s.dec=open(f'{P}/decisions.jsonl','a',buffering=1)
        sol_usd,src=A.config.sol_usd(A.CFG,live=os.environ.get('PAPER_LIVEPRICE','1')=='1'); s.bank=A.CFG['bankroll_usd']/sol_usd
        s.log(action='START',reason=f'paper bot start pid {os.getpid()}; rules {RULE_NAMES}; txc {TXC}; p_fail {PFAIL}; bankroll cap {s.bank:.5f} SOL (SOL ${sol_usd}, {src})')
        s.rm={}
        for r in RULE_NAMES:
            rm=RiskManager(f'{P}/state/{r}.sqlite',A.CFG,s.bank,TXC,log=lambda _r=r,**k: s.log(rule=_r,**k),mode='paper')
            rm.wallet_balance_fn=(lambda _rm=rm: _rm.cash)
            s.rm[r]=rm
            s.log(rule=r,action='RISK_STATE',reason=rm.fail_closed or 'ok',open=len(rm.open_positions()),days=rm.day_table()[-1:] )
        s.gate=gate or EntryGate(log=lambda **k: None); s.last_shadow=0
        s.creates={}; s.trades=collections.defaultdict(list); s.grads={}; s.seen=set()
        s.st={r:{} for r in RULE_NAMES}
        s.pos={r:{} for r in RULE_NAMES}
        for r,rm in s.rm.items():
            for p in rm.open_positions(): s.pos[r][p['id']]=dict(mint=p['mint'],restored=True,size=p['size'],entry_t=p['entry_t'])
        s.last_rt=0; s.n_ev=0; s.stats=collections.Counter(); s.stop=False
        signal.signal(signal.SIGTERM,lambda *a: setattr(s,'stop',True))
    def log(s,**k):
        k.setdefault('time',now_local()); s.dec.write(json.dumps(k,default=str)+'\n')

    def files(s): return sorted(glob.glob(f'{DATA}/events_*.jsonl'))
    def follow(s):
        cur=s.files()[-1]; f=open(cur); f.seek(0,2); buf=''
        s.log(action='FEED',reason=f'tailing {cur} from end (no replay)')
        last_hb=0
        while not s.stop:
            chunk=f.read(1<<20)
            if chunk:
                buf+=chunk; *lines,buf=buf.split('\n')
                for l in lines: s.on_line(l)
            else:
                fs=s.files()
                if fs and fs[-1]!=cur:
                    rest=f.read(); buf+=rest
                    for l in buf.split('\n'): s.on_line(l)
                    buf=''; f.close(); cur=fs[-1]; f=open(cur); s.log(action='FEED',reason=f'rotated to {cur}')
                time.sleep(0.2)
            s.tick()
            if time.time()-last_hb>10:
                last_hb=time.time()
                json.dump(dict(ts=time.time(),local=now_local(),pid=os.getpid(),events=s.n_ev,feed_lag_s=round(time.time()-s.last_rt,1) if s.last_rt else None,
                    open={r:len(v) for r,v in s.pos.items()},stats=dict(s.stats)),open(f'{P}/heartbeat.json','w'))
        s.log(action='STOP',reason='SIGTERM'); [rm.release() for rm in s.rm.values()]
    def on_line(s,l):
        if not l: return
        try: e=json.loads(l)
        except Exception: s.stats['bad_line']+=1; return
        k=e.get('e')
        if k not in ('C','T','G'): return
        s.n_ev+=1; s.last_rt=max(s.last_rt,e.get('rt',0))
        m=e['mint']
        if k=='C': s.creates.setdefault(m,e); return
        if k=='G': s.grads.setdefault(m,e); s.on_mint(m,grad=True); return
        key=(e['sig'][:20],m,e['user'],e['tok'],e['buy'])
        if key in s.seen or not(e['vsol']>0 and e['vtok']>0): return
        s.seen.add(key)
        if m not in s.creates: return
        s.trades[m].append(e); s.on_mint(m)

    def rnd(s,*a): return random.Random(int(hashlib.sha256('|'.join(map(str,a)).encode()).hexdigest()[:12],16))
    def on_mint(s,m,grad=False):
        c=s.creates.get(m)
        if not c: return
        T=s.trades[m]; dev=c.get('creator') or c['user']
        for r in RULE_NAMES:
            st=s.st[r].setdefault(m,dict(phase='watch'))
            ph=st['phase']
            if ph=='watch' and T and T[-1]['ts']<=c['ts']+WINDOW and not grad:
                i=A.RULES[r]['entry'](T,c['ts'],dev)
                if i is not None: st.update(phase='pending',sig_i=i,sig_t=T[i]['ts']); s.log(rule=r,mint=m,action='SIGNAL',reason=f'{r} signal at trade {i}')
                ph=st['phase']
            if ph=='pending': s.try_enter(r,m,st,T,dev)
            elif ph=='filling' and not grad: s.fill_attempt(r,m,st,T)
            elif ph=='held': s.check_exit(r,m,st,T,dev,grad)
            elif ph=='exiting' and not grad: s.exit_attempt(r,m,st,T)
            elif ph in('filling','exiting') and grad: s.force_grad(r,m,st,T)
    def mandatory(s,m,T,dev):
        r=[]; e=T[-1]; g=s.grads.get(m)
        if not A.std_curve(T): r.append('nonstandard_curve')
        if e['rtok']<=0 or g: r.append('curve_complete_or_graduated')
        if not any((not t['buy']) and t['user']!=dev and t['sol']>0 for t in T): r.append('no_successful_nondev_sell_yet')
        return r
    def try_enter(s,r,m,st,T,dev):
        why=s.mandatory(m,T,dev)
        if why:
            if why==['no_successful_nondev_sell_yet'] and T[-1]['ts']<=st['sig_t']+60: return
            st['phase']='done'; s.log(rule=r,mint=m,action='REJECT',reason='; '.join(why)); return
        rm=s.rm[r]; t=T[-1]['rt']
        size,rwhy=rm.size_for_entry(t,m)
        if size is None:
            st['phase']='done'; s.log(rule=r,mint=m,action='SKIP',reason=rwhy)
            if time.time()-s.last_shadow>=SHADOW_EVERY:
                s.last_shadow=time.time(); ok,gr,det=s.gate.check(m); s.stats['shadow_gate']+=1
                s.log(rule=r,mint=m,action='ENTRY_GATE_SHADOW',ok=ok,reason=gr,latency_s=det.get('latency_s'),note='risk said no; gate run only to verify it works live')
            return
        ok,gr,det=s.gate.check(m); s.stats['gate_calls']+=1
        s.log(rule=r,mint=m,action='ENTRY_GATE',ok=ok,reason=gr,latency_s=det.get('latency_s'))
        if not ok: st['phase']='done'; return
        st.update(phase='filling',size=size,attempts=0,gate_done_rt=time.time(),rnd=s.rnd(m,r,'fill'))
    def fill_attempt(s,r,m,st,T):
        e=T[-1]
        if e['rt']<st['gate_done_rt']: return
        st['attempts']+=1; rm=s.rm[r]
        if st['rnd'].random()<PFAIL:
            s.log(rule=r,mint=m,action='TX_FAIL',reason=f"simulated entry attempt {st['attempts']}/{1+R} failed")
            if st['attempts']>=1+R:
                rm.failed_entry(e['rt'],st['attempts']*TXC); st['phase']='done'
                s.log(rule=r,mint=m,action='ENTRY_FAILED',reason=f"all {1+R} attempts failed; fees {st['attempts']*TXC:.6f} booked")
            return
        pid=f"{m[:10]}_{int(e['rt']*1000)}"
        try: rm.open(pid,m,e['rt'],st['size'],st['attempts']*TXC)
        except Oversize as x: st['phase']='done'; s.log(rule=r,mint=m,action='SKIP',reason=f'open re-check: {x}'); return
        got=A.buy_tokens(e['vsol'],e['vtok'],st['size'])
        st.update(phase='held',pid=pid,p0=e['vsol']/e['vtok'],got=got,entry_ts=e['ts'],entry_rt=e['rt'],last=e)
        s.pos[r][pid]=dict(mint=m,size=st['size'],entry_t=e['rt'])
        s.log(rule=r,mint=m,action='ENTER',pid=pid,size_sol=round(st['size'],6),entry_mcap_sol=round(A.mc(e),2),attempts=st['attempts'],reason='paper fill at observed curve state (no tx sent)')
    def check_exit(s,r,m,st,T,dev,grad=False):
        if grad or T[-1]['rtok']<=0: return s.force_grad(r,m,st,T)
        e=T[-1]; st['last']=e; rule=A.RULES[r]; x=(e['vsol']/e['vtok'])/st['p0']-1
        why=('take_profit' if x>=rule['tp'] else 'stop_loss' if x<=-rule['sl'] else 'time_stop' if e['ts']-st['entry_ts']>=rule['hold']
             else 'dev_sell_exit' if (e['user']==dev and not e['buy']) else None)
        if why: st.update(phase='exiting',why=why,xattempts=0,xrnd=s.rnd(m,r,'exit')); s.log(rule=r,mint=m,action='EXIT_SIGNAL',reason=why,ret_pct=round(100*x,1))
    def exit_attempt(s,r,m,st,T,state=None):
        e=state or T[-1]; st['xattempts']+=1; st['x_last_try']=time.time()
        if st['xrnd'].random()<PFAIL and st['xattempts']<1+R:
            s.log(rule=r,mint=m,action='TX_FAIL',reason=f"simulated exit attempt {st['xattempts']}/{1+R} failed"); return
        ok=not(st['xattempts']>=1+R and st['xrnd'].random()<PFAIL)
        pr=A.sell_curve(e['vsol'],e['vtok'],st['got']) if ok else 0.0
        s.finish(r,m,st,pr,ok,e['rt'] if 'rt' in e else time.time(),venue='curve')
    def force_grad(s,r,m,st,T):
        if st['phase'] not in ('held','exiting'): st['phase']='done'; return
        last=next((t for t in reversed(T) if t['rtok']<=0),T[-1] if T else None)
        rnd=st.get('xrnd') or s.rnd(m,r,'exit'); n=0; ok=False
        while n<1+R:
            n+=1
            if rnd.random()>=PFAIL: ok=True; break
        pr=A.sell_pool(max(0.0,last['rsol']-A.GRADFEE),st['got']) if ok else 0.0
        st['xattempts']=n; st['why']='graduated_exit_pumpswap(ASSUMPTION: 25% pre-sell, unverified)'
        s.finish(r,m,st,pr,ok,time.time(),venue='pumpswap_model')
    def finish(s,r,m,st,pr,ok,t,venue):
        rm=s.rm[r]; pnl,day=rm.close(st['pid'],t,pr,st['xattempts']*TXC,exit_ok=ok); st['phase']='done'; s.pos[r].pop(st['pid'],None)
        s.log(rule=r,mint=m,action='EXIT',reason=st['why']+('' if ok else ' + EXIT_FAILED_all_attempts=>valued_0, rent lost'),venue=venue,
              attempts=st['xattempts'],pnl_sol=round(pnl,6),day_realized_sol=round(day,6),cash_sol=round(rm.cash,6))
    def tick(s):

        now=time.time()
        if int(now)%5 or getattr(s,'_last_tick',0)==int(now): return
        s._last_tick=int(now)
        for r in RULE_NAMES:
            for pid,p in list(s.pos[r].items()):
                st=s.st[r].get(p['mint'])
                if p.get('restored') and (st is None or st.get('phase')!='held'):

                    T=s.trades.get(p['mint'])
                    if now-p['entry_t']>A.RULES[r]['hold'] or T:
                        rm=s.rm[r]; e=T[-1] if T else None
                        got=None
                        s.log(rule=r,mint=p['mint'],action='EXIT',reason='restart recovery: price path unknown -> conservative exit valued 0 (rent lost)')
                        rm.close(pid,now,0.0,TXC,exit_ok=False); s.pos[r].pop(pid,None)
                    continue
                if st and st.get('phase')=='exiting' and now-st.get('x_last_try',now)>5:
                    s.exit_attempt(r,p['mint'],st,None,state=st['last']); continue
                if st and st.get('phase')=='held' and now-st['entry_rt']>=A.RULES[r]['hold']+30:
                    st.update(phase='exiting',why='time_stop(no trades)',xattempts=0,xrnd=s.rnd(p['mint'],r,'exit')); s.exit_attempt(r,p['mint'],st,None,state=st['last'])
            for m,st in s.st[r].items():
                if st.get('phase')=='filling' and now-st['gate_done_rt']>30:
                    st['phase']='done'
                    if st['attempts']: s.rm[r].failed_entry(now,st['attempts']*TXC)
                    s.log(rule=r,mint=m,action='ENTRY_ABANDONED',reason=f"no trade to fill against within 30 s; failed-attempt fees {st['attempts']*TXC:.6f} booked")
        if int(now)%60==0:
            cut=now-2*WINDOW
            for m in [m for m,c in s.creates.items() if c['rt']<cut and not any(st.get(m,{}).get('phase') in ('held','filling','exiting') for st in s.st.values())]:
                s.creates.pop(m,None); s.trades.pop(m,None); [st.pop(m,None) for st in s.st.values()]
            if len(s.seen)>400000: s.seen=set(list(s.seen)[-200000:])
if __name__=='__main__':
    Bot().follow()
