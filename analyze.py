import json, glob, gzip, collections, os, sys, random, hashlib, datetime as dt
import pandas as pd
import config
from risk import RiskManager, init_db
CFG=config.load()
ZONE=dt.timezone(dt.timedelta(hours=CFG['day_boundary_tz_offset_hours']))
fmt=lambda t: dt.datetime.fromtimestamp(t,ZONE).strftime('%Y-%m-%d %H:%M:%S')

SOL_USD,SOL_SRC=float(CFG['sol_usd_fallback']),'not fetched yet (main() fetches live)'
BANK0=CFG['bankroll_usd']/SOL_USD
FEE=CFG['pump_curve_fee']; PSFEE=CFG['pumpswap_fee_conservative']; GRADFEE=CFG['graduation_fee_sol']
SIZE=CFG['target_size_sol']; R=CFG['max_tx_retries']; PFAILS=CFG['tx_fail_prob_scenarios']; TXC=CFG['tx_cost_scenarios_sol']
SUPPLY=1e9; CURVE_TOKENS=793.1e6; K_STD=30*1.073e9; POOL_TOK=SUPPLY-CURVE_TOKENS
WINDOW=int(os.environ.get("WIN","1800")); DATA=os.environ.get('DATA','v2'); MINTMODE=os.environ.get('MINTMODE','live')
DEC=None
def decide(**k):
    if DEC is None: return
    if 't' in k and isinstance(k['t'],(int,float)): k['time']=fmt(k['t'])
    DEC.write(json.dumps(k,default=str)+'\n')

def files():
    f=sorted(glob.glob('data/events_*.jsonl*'))
    return (sorted(glob.glob('raw/rpc_events_*.jsonl'))+f) if DATA=='all' else f
creates={}; trades=collections.defaultdict(list); grads={}; live={}; eligible=[]; BATCH={}
mc=lambda e: e['vsol']/e['vtok']*SUPPLY
def buy_tokens(vs,vt,sol): net=sol*(1-FEE); return vt-vs*vt/(vs+net)
def sell_curve(vs,vt,tok): return (vs-vs*vt/(vt+tok))*(1-FEE)
def sell_pool(pool_sol,tok,presell=None):

    f=CFG['pumpswap_migration_presell_frac'] if presell is None else presell
    ps,pt=pool_sol*(1-f),POOL_TOK/(1-f)
    return ps*tok/(pt+tok)*(1-PSFEE)
def std_curve(T): return (not T) or (0.97<T[0]['vsol']*T[0]['vtok']/K_STD<1.03 and T[0].get('fee_bps',95)>0)
def grad_ts(m,T):
    g=grads.get(m,{}).get('ts'); c=next((t['ts'] for t in T if t['rtok']<=0),None)
    return min([x for x in (g,c) if x],default=None)

def mint_ok_at(m,e):

    if MINTMODE=='live':
        lv=live.get(m)
        if not lv: return 'no_live_mint_check'
        if lv['rt']>e['rt']: return f'live_mint_check_pending(arrives {lv["rt"]-e["rt"]:.2f}s later)'
        return '' if lv['ok'] else 'mint:'+lv['reason']
    mi=BATCH.get(m); return 'mint_unverified' if not mi else ('' if mi['ok'] else 'mint:'+mi['reason'])
def mandatory(m,T,i,dev,g):
    r=[]; e=T[i]; w=mint_ok_at(m,e)
    if w: r.append(w)
    if not std_curve(T): r.append('nonstandard_curve')
    if e['rtok']<=0 or (g is not None and g<=e['ts']): r.append('curve_complete_or_graduated')
    if not any((not t['buy']) and t['user']!=dev and t['sol']>0 for t in T[:i+1]): r.append('no_successful_nondev_sell_yet')
    return '; '.join(r)
WAITABLE=('no_successful_nondev_sell_yet','live_mint_check_pending')
def entry_with_mandatory(m,T,i,dev,g,wait=60):
    if i is None: return None,'no_signal'
    for j in range(i,len(T)):
        if T[j]['ts']>T[i]['ts']+wait: break
        why=mandatory(m,T,j,dev,g)
        if not why: return j,''
        if any(not p.strip().startswith(WAITABLE) for p in why.split(';')): return None,why
    return None,'wait_timeout: '+(mandatory(m,T,min(i,len(T)-1),dev,g) or '?')

def after_slots(n): return lambda T,t0,dev: next((i for i,t in enumerate(T) if t['slot']>=T[0]['slot']+n),None)
def filtered(T,t0,dev,obs=60):
    W=[t for t in T if t['ts']<=t0+obs]
    if not W: return None
    devbuy=sum(t['tok'] for t in W if t['user']==dev and t['buy']); devsell=any(t['user']==dev and not t['buy'] for t in W)
    buyers={t['user'] for t in W if t['buy']}; bundle=len({t['user'] for t in T if t['buy'] and t['slot']<=T[0]['slot']+1 and t['user']!=dev})
    netsol=sum(t['sol'] if t['buy'] else -t['sol'] for t in W); by=collections.Counter()
    for t in W: by[t['user']]+= t['tok'] if t['buy'] else -t['tok']
    top=max(by.values()) if by else 0; rs=[]
    if devbuy/SUPPLY>0.05: rs.append(f'dev_buy {100*devbuy/SUPPLY:.1f}%>5%')
    if devsell: rs.append('dev_sold_in_60s')
    if len(buyers)<10: rs.append(f'unique_buyers {len(buyers)}<10')
    if bundle>3: rs.append(f'bundled_buyers_slot0-1 {bundle}>3')
    if netsol<=0.5: rs.append(f'net_inflow {netsol:.2f}<=0.5 SOL')
    if top/SUPPLY>0.08: rs.append(f'top_wallet {100*top/SUPPLY:.1f}%>8%')
    filtered.last='; '.join(rs)
    return None if rs else next((i for i,t in enumerate(T) if t['ts']>t0+obs),None)
def momentum(pct,within=1800):
    def f(T,t0,dev):
        for i,t in enumerate(T):
            if t['ts']>t0+within: return None
            if (CURVE_TOKENS-t['rtok'])/CURVE_TOKENS>=pct and t['rtok']>0: return min(i+1,len(T)-1)
    return f
RULES={
 'A_snipe_slot+1':dict(entry=after_slots(1),tp=.5,sl=.3,hold=600),
 'A_snipe_slot+3':dict(entry=after_slots(3),tp=.5,sl=.3,hold=600),
 'A_snipe_slot+8':dict(entry=after_slots(8),tp=.5,sl=.3,hold=600),
 'A_snipe_slot+25':dict(entry=after_slots(25),tp=.5,sl=.3,hold=600),
 'B_filtered_60s':dict(entry=filtered,tp=.5,sl=.3,hold=600),
 'C_momentum_30pct':dict(entry=momentum(.30),tp=.5,sl=.25,hold=900),
 'C_momentum_60pct':dict(entry=momentum(.60),tp=.3,sl=.2,hold=900),
}
def rng_for(*a): return random.Random(int(hashlib.sha256('|'.join(map(str,a)).encode()).hexdigest()[:12],16))
def attempts(rnd,p):

    for k in range(1,R+2):
        if rnd.random()>=p: return k,True
    return R+1,False
def simulate(name,p,log=True):

    rule=RULES[name]; out=[]
    for m in eligible:
        c=creates[m]; t0=c['ts']; dev=c.get('creator') or c['user']; T=[t for t in trades[m] if t['ts']<=t0+WINDOW]
        if not T:
            if log: decide(rule=name,pfail=p,mint=m,action='NO_TRADES',reason='no trades after create in window');
            continue
        g=grad_ts(m,T); filtered.last=''
        sig=rule['entry'](T,t0,dev); i,why=entry_with_mandatory(m,T,sig,dev,g)
        if i is None:
            if log: decide(rule=name,pfail=p,mint=m,action='NO_SIGNAL' if why=='no_signal' else 'REJECT',reason=(filtered.last or why) if why=='no_signal' and name.startswith('B') else why);
            continue
        rnd=rng_for(m,name,p); na,ok=attempts(rnd,p); j=min(i+na-1,len(T)-1)
        if not ok:
            out.append(dict(mint=m,entry_t=T[i]['ts'],exit_t=T[j]['ts'],entered=False,n_entry=na,n_exit=0,venue='-',reason='entry_failed_all_attempts'))
            if log: decide(rule=name,pfail=p,mint=m,t=T[i]['ts'],action='ENTRY_FAILED',reason=f'{na} attempts failed (cap 1+{R}); fees lost');
            continue
        e=T[j]; p0=e['vsol']/e['vtok']; reason='window_end'; trig=None
        for k in range(j+1,len(T)):
            t=T[k]
            if t['rtok']<=0 or (g is not None and t['ts']>=g): reason='graduated_exit_pumpswap'; trig=k; break
            r=(t['vsol']/t['vtok'])/p0-1
            if r>=rule['tp']: reason='take_profit'
            elif r<=-rule['sl']: reason='stop_loss'
            elif t['ts']-e['ts']>=rule['hold']: reason='time_stop'
            elif t['user']==dev and not t['buy']: reason='dev_sell_exit'
            else: continue
            trig=k; break
        if trig is None: trig=len(T)-1
        nx,okx=attempts(rnd,p)
        if reason=='graduated_exit_pumpswap':
            last=next((t for t in reversed(T) if t['rtok']<=0),None) or T[trig-1]
            x=dict(venue='pumpswap',pool_sol=max(0.0,last['rsol']-GRADFEE),ts=g or last['ts'])
        else:
            xi=min(trig+nx,len(T)-1); xt=T[xi]
            if xt['rtok']<=0: xt=next(t for t in reversed(T[:xi+1]) if t['rtok']>0)
            x=dict(venue='curve',vs=xt['vsol'],vt=xt['vtok'],ts=xt['ts'])
        rec=dict(mint=m,entry_t=e['ts'],exit_t=x['ts'],entered=True,n_entry=na,n_exit=nx,exit_ok=okx,venue=x['venue'],reason=reason if okx else reason+'+EXIT_FAILED_all_attempts=>valued_0',
                 evs=e['vsol'],evt=e['vtok'],entry_mc=mc(e),xvs=x.get('vs'),xvt=x.get('vt'),pool_sol=x.get('pool_sol'),signal_t=T[sig]['ts'] if sig is not None else None)
        out.append(rec)
        if log: decide(rule=name,pfail=p,mint=m,t=e['ts'],action='SIGNAL_ENTRY',reason=f'signal ok + mandatory ok (live mint check {live[m]["rt"]-c["rt"]:.2f}s after create)' if MINTMODE=='live' else 'signal ok + mandatory ok (batch mint mode)',
                       entry_attempts=na,exit=rec['reason'],exit_attempts=nx,venue=x['venue'])
    return out
def proceeds(rec,size):
    if not rec['entered'] or not rec.get('exit_ok',True): return 0.0
    got=buy_tokens(rec['evs'],rec['evt'],size)
    return sell_pool(rec['pool_sol'],got) if rec['venue']=='pumpswap' else sell_curve(rec['xvs'],rec['xvt'],got)
def load_batch_cache():

    v=sorted(glob.glob('results/mint_cache/mint_cache_*.json'))
    return json.load(open(v[-1])) if v else {}
ARCHIVE2=os.path.join(os.path.dirname(os.path.abspath(__file__)),'archive','runs')
STORE_HASHES=os.path.join(os.path.dirname(os.path.abspath(__file__)),'archive','hashes')
def seal_run(d,run_tools=True):

    import subprocess, shutil
    if run_tools:
        for s_ in ('budget.py','chart.py'): subprocess.run([sys.executable,s_,d],check=True,stdout=open(f'{d}/{s_[:-3]}_stdout.txt','w'))
    rows=[]
    for root,dirs,fs in os.walk(d):
        dirs[:]=[x for x in dirs if x!='__pycache__']
        for f in sorted(fs):
            p=os.path.join(root,f)
            if f=='SHA256SUMS': continue
            rows.append(f"{hashlib.sha256(open(p,'rb').read()).hexdigest()}  {os.path.relpath(p,d)}")
    open(f'{d}/SHA256SUMS','w').write('# excludes __pycache__/ by design\n'+'\n'.join(sorted(rows,key=lambda r:r[66:]))+'\n')
    dst=os.path.join(ARCHIVE2,os.path.basename(d)); os.makedirs(ARCHIVE2,exist_ok=True); shutil.copytree(d,dst,ignore=shutil.ignore_patterns('__pycache__'))
    for base in (d,dst):
        for root,dirs,fs in os.walk(base,topdown=False):
            for f in fs: os.chmod(os.path.join(root,f),0o444)
            os.chmod(root,0o555)
    ok=subprocess.run(['sha256sum','-c','--quiet','SHA256SUMS'],cwd=dst).returncode==0
    os.makedirs(STORE_HASHES,exist_ok=True); sp=os.path.join(STORE_HASHES,os.path.basename(d)+'.SHA256SUMS')
    if not os.path.exists(sp): shutil.copyfile(f'{d}/SHA256SUMS',sp)
    print(f"sealed {d}: {len(rows)} files hashed, read-only, copy {dst} verified={ok}, hashes also in {sp}")
def main():
    global RUN,OUTD,DEC,creates,trades,grads,seen,pp,live,stat_rows,first_rt,last_rt,END,BATCH,first_live,eligible,SOL_USD,SOL_SRC,BANK0
    SOL_USD,SOL_SRC=config.sol_usd(CFG, live=os.environ.get('LIVEPRICE','1')=='1'); BANK0=CFG['bankroll_usd']/SOL_USD
    RUN=dt.datetime.now(ZONE).strftime('%Y%m%d_%H%M%S')+'_'+os.environ.get('LABEL',f'{DATA}_{MINTMODE}_win{WINDOW}')
    OUTD=f'results/runs/{RUN}'; os.makedirs(OUTD,exist_ok=False)
    if os.path.islink('results/latest'): os.remove('results/latest')
    os.symlink(f'runs/{RUN}','results/latest')
    DEC=open(f'{OUTD}/decisions.jsonl','w')

    creates={}; trades=collections.defaultdict(list); grads={}; seen=set(); pp={}; live={}; stat_rows=[]; first_rt=1e20; last_rt=0
    for f in files():
        op=gzip.open if f.endswith('.gz') else open
        with op(f,'rt') as fh:
            for l in fh:
                try: e=json.loads(l)
                except Exception: continue
                k=e.get('e')
                if k=='STAT': stat_rows.append(e); continue
                if k=='PP':
                    if e.get('txType')=='create' and e.get('pool','pump')=='pump': pp.setdefault(e['mint'],e)
                    continue
                if k=='MINTCHK':
                    if e['mint'] not in live or e['rt']<live[e['mint']]['rt']: live[e['mint']]=e
                    continue
                if k not in ('C','T','G'): continue
                first_rt=min(first_rt,e['rt']); last_rt=max(last_rt,e['rt'])
                if k=='C':
                    if e['mint'] not in creates or e['rt']<creates[e['mint']]['rt']: creates[e['mint']]=e
                elif k=='G': grads.setdefault(e['mint'],e)
                else:
                    key=(e['sig'][:20],e['mint'],e['user'],e['tok'],e['buy'])
                    if key in seen: continue
                    seen.add(key)
                    if e['vsol']>0 and e['vtok']>0: trades[e['mint']].append(e)
    for m in trades: trades[m].sort(key=lambda t:(t['slot'],t['rt']))
    END=last_rt
    BATCH=json.load(open('results/mint_cache.json')) if os.path.exists('results/mint_cache.json') else {}
    BATCH=load_batch_cache()
    first_live=min([v['rt'] for v in live.values()],default=1e20)
    pool=[m for m,c in creates.items() if c['rt']+WINDOW<=END+1]
    eligible=[m for m in pool if (MINTMODE!='live' or creates[m]['rt']>=first_live)]

    lo=max(min([p['rt'] for p in pp.values()],default=0),first_rt)+60; hi=END-60
    ppw=[m for m,p in pp.items() if lo<=p['rt']<=hi]; rpcw=[m for m,c in creates.items() if lo<=c['rt']<=hi]
    cov=dict(window=f'{fmt(lo)}..{fmt(hi)}',pumpportal_creates=len(ppw),rpc_saw_of_those=sum(m in creates for m in ppw),
             rpc_create_coverage_vs_pumpportal=round(sum(m in creates for m in ppw)/max(1,len(ppw)),4),rpc_creates=len(rpcw),pumpportal_saw_of_rpc=round(sum(m in pp for m in rpcw)/max(1,len(rpcw)),4))
    hdr=(f"PRELIMINARY run {RUN} | data {fmt(first_rt)} -> {fmt(last_rt)} UTC+3 ({DATA}) | creates {len(creates)} | live MINTCHK {len(live)} (first {fmt(first_live) if live else '-'})"
         f" | eligible {len(eligible)} (window {WINDOW}s, mint mode {MINTMODE}) | SOL ${SOL_USD} [{SOL_SRC}] | bankroll {BANK0:.4f} SOL")
    print(hdr); print('CREATE COVERAGE',cov)

    start_mc=30/1073e6*SUPPLY; rows=[]; dev_launch=collections.Counter(c.get('creator') or c['user'] for c in creates.values())
    for m in eligible:
        c=creates[m]; dev=c.get('creator') or c['user']; t0=c['ts']; T=[t for t in trades[m] if t['ts']<=t0+WINDOW]
        lv=live.get(m); base=dict(mint=m,name=c['name'],symbol=c['symbol'],created=fmt(c['rt']),live_mint_check=(('pass' if lv['ok'] else lv['reason']) if lv else 'none'),
            live_check_delay_s=round(lv['rt']-c['rt'],2) if lv else None,std_curve=std_curve(T),dev_launches_in_data=dev_launch[dev])
        if not T: rows.append(dict(base,n_trades=0,status='no_trades',dead=True,graduated=False,dev_sold_majority=False)); continue
        mcs=[mc(t) for t in T]; peak=max(mcs); final=mcs[-1]; g=grad_ts(m,T)
        devb=sum(t['tok'] for t in T if t['user']==dev and t['buy']); devs=sum(t['tok'] for t in T if t['user']==dev and not t['buy'])
        grad=g is not None and g<=t0+WINDOW+5; dead=(not grad) and (final<0.2*peak or (final<1.5*start_mc and T[-1]['ts']-t0<WINDOW-600))
        rows.append(dict(base,n_trades=len(T),unique_traders=len({t['user'] for t in T}),dev_sold_majority=devb>0 and devs>=0.5*devb,peak_mc_sol=peak,final_mc_sol=final,
            peak_x=peak/start_mc,graduated=grad,dead=dead,status='graduated' if grad else ('dead' if dead else 'alive')))
    tok=pd.DataFrame(rows); tok.to_csv(f'{OUTD}/tokens.csv',index=False)
    summ=dict(eligible=len(tok)) if tok.empty else dict(eligible=len(tok),live_mint_check_fail=int(((tok.live_mint_check!='pass')&(tok.live_mint_check!='none')).sum()),no_live_check=int((tok.live_mint_check=='none').sum()),
         median_live_check_delay_s=float(tok.live_check_delay_s.median()) if tok.live_check_delay_s.notna().any() else None,no_trades=int((tok.n_trades==0).sum()),dead_incl_no_trades=int(tok.dead.sum()),
         graduated=int(tok.graduated.sum()),dev_sold_majority=int(tok.dev_sold_majority.sum()),peak_ge_2x=int((tok.peak_x>=2).sum()),peak_ge_5x=int((tok.peak_x>=5).sum()))
    print("TOKEN SUMMARY",summ)

    res=[]; allt=[]; SIM={}
    for p in PFAILS:
        for name in RULES:
            SIM[(name,p)]=recs=simulate(name,p)
            for txn,txc in TXC.items():
                d=[]
                for r in recs:
                    fees=(r['n_entry']+r['n_exit'])*txc
                    pnl=(proceeds(r,SIZE)-SIZE-fees) if r['entered'] else -fees
                    d.append(dict(r,rule=name,pfail=p,txcost=txn,size=SIZE,fees=fees,pnl=pnl,ret=pnl/SIZE))
                d=pd.DataFrame(d)
                if d.empty: res.append(dict(rule=name,pfail=p,txcost=txn,trades=0)); continue
                allt.append(d)
                res.append(dict(rule=name,pfail=p,txcost=txn,trades=len(d),entry_failed=int((~d.entered).sum()),exit_failed=int(d.reason.str.contains('EXIT_FAILED').sum()),
                    pumpswap_exits=int((d.venue=='pumpswap').sum()),win_rate=round((d.pnl>0).mean(),3),avg_ret_pct=round(100*d.ret.mean(),2),median_ret_pct=round(100*d.ret.median(),2),
                    total_pnl_sol=round(d.pnl.sum(),5),worst_sol=round(d.pnl.min(),5),worst_pct_of_bank=round(100*d.pnl.min()/BANK0,2),avg_fees_sol=round(d.fees.mean(),6)))
    S=pd.DataFrame(res); S.to_csv(f'{OUTD}/sim_summary.csv',index=False)
    (pd.concat(allt) if allt else pd.DataFrame()).to_csv(f'{OUTD}/sim_trades.csv',index=False)
    pd.set_option('display.width',250); pd.set_option('display.max_columns',30)
    print("\nFIXED-SIZE (0.02 SOL) PER-TRADE STATS, incl. failed-tx fees (capped retries)"); print(S.to_string(index=False))

    P=[]
    for (name,p),recs in SIM.items():
        for txn,txc in TXC.items():
            tag=dict(rule=name,pfail=p,txcost=txn)
            dbp=f"{OUTD}/state/{name}_p{p}_{txn}.sqlite"; os.makedirs(f'{OUTD}/state',exist_ok=True)
            init_db(dbp,BANK0,i_understand=True,mode='backtest',init_logs=[f'{OUTD}/state/init_log.jsonl']); rm=RiskManager(dbp,CFG,BANK0,txc,log=lambda **k: decide(**tag,**k),mode='backtest')
            ev=[]
            for n,r in enumerate(recs): ev.append((r['entry_t'],0,n)); ev.append((r['exit_t'],1,n))
            opened={}; worst=0; ent=0; skips=collections.Counter()
            for t,kind,n in sorted(ev):
                r=recs[n]
                if kind==0:
                    size,why=rm.size_for_entry(t,r['mint'])
                    if size is None: skips[why.split(':')[0].split(' ')[0]]+=1; decide(**tag,mint=r['mint'],t=t,action='SKIP',reason=why); continue
                    if not r['entered']:
                        rm.failed_entry(t,r['n_entry']*txc); decide(**tag,mint=r['mint'],t=t,action='ENTRY_FAILED',reason=f"{r['n_entry']} failed attempts, fees {r['n_entry']*txc:.6f} booked"); continue
                    rm.open(f'p{n}',r['mint'],t,size,r['n_entry']*txc); opened[n]=size; ent+=1
                    decide(**tag,mint=r['mint'],t=t,action='ENTER',size_sol=round(size,6),reason=f"{name}: signal+mandatory ok; size=min({SIZE}, 5% equity - worst fees {rm.worst_fees():.5f}); attempts {r['n_entry']}")
                elif n in opened:
                    pr=proceeds(r,opened[n]); pnl,day_real=rm.close(f'p{n}',t,pr,r['n_exit']*txc,exit_ok=r.get('exit_ok',True)); worst=min(worst,pnl)
                    decide(**tag,mint=r['mint'],t=t,action='EXIT',reason=r['reason'],venue=r['venue'],exit_attempts=r['n_exit'],pnl_sol=round(pnl,6),day_realized_sol=round(day_real,6))
            assert not rm.open_positions(), 'positions left open at end of sim'
            days=rm.day_table(); end=rm.cash; rm.close_db()
            P.append(dict(**tag,signals=len(recs),entered=ent,**{f'skip_{k}':v for k,v in skips.items()},halted_days=sum(d['halted'] for d in days),
                worst_trade_pct_of_bank=round(100*worst/BANK0,2),worst_day_pct=round(100*min(d['realized']/d['start_equity'] for d in days),2) if days else 0,
                end_sol=round(end,5),pnl_sol=round(end-BANK0,5),pnl_pct=round(100*(end-BANK0)/BANK0,2)))
    P=pd.DataFrame(P).fillna(0); P.to_csv(f'{OUTD}/portfolio_risk_managed.csv',index=False)
    print(f"\nRISK-MANAGED via risk.RiskManager (strict only; PnL booked on exit day; carried positions reserved; bankroll {BANK0:.4f} SOL = ${CFG['bankroll_usd']} @ ${SOL_USD})")
    print(P.to_string(index=False))
    hours=(END-first_rt)/3600; ehrs=max(1e-9,(max([creates[m]['rt'] for m in eligible],default=first_rt)-min([creates[m]['rt'] for m in eligible],default=first_rt))/3600)
    man=dict(preliminary=True,run=RUN,code='analyze.py v4',data=DATA,mint_mode=MINTMODE,window_s=WINDOW,data_start=fmt(first_rt),data_end=fmt(last_rt),hours_observed=round(hours,3),
        eligible_create_span_hours=round(ehrs,3),creates_per_hour=round(len(creates)/max(hours,1e-9),1),summary=summ,create_coverage=cov,sol_usd=SOL_USD,sol_usd_source=SOL_SRC,bankroll_sol=BANK0,
        config=CFG,est_union_coverage_median=float(pd.Series([s.get('est_union_coverage') for s in stat_rows if s.get('est_union_coverage')]).median()) if stat_rows else None)
    json.dump(man,open(f'{OUTD}/run_manifest.json','w'),indent=1,default=str); DEC.close()
    print(f"\nrun folder: {OUTD}  (decisions: {sum(1 for _ in open(f'{OUTD}/decisions.jsonl'))} lines)")
    seal_run(OUTD)

if __name__=='__main__': main()
