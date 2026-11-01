import sqlite3, json, os, sys, fcntl, time, datetime as dt
SCHEMA_VERSION=5
ROOT=os.path.dirname(os.path.abspath(__file__))
INIT_LOGS=[os.path.join(ROOT,'state','init_log.jsonl'),os.path.join(ROOT,'archive','init_log.jsonl')]
TZ=dt.timezone(dt.timedelta(hours=3))
class InstanceLocked(RuntimeError): pass
class Oversize(ValueError): pass
_CRASH_HOOK=None
def _crash(point):
    if _CRASH_HOOK: _CRASH_HOOK(point)
def _schema(c):
    c.execute("create table kv(k text primary key, v text)")
    c.execute("create table positions(id text primary key, mint text, entry_t real, size real, entry_fees real, worst real, entry_day text)")
    c.execute("create table days(day text primary key, start_equity real, realized real, halted int, halt_reason text)")
def _append_logs(rec, logs):
    ok=[]
    for p in logs:
        try:
            os.makedirs(os.path.dirname(p),exist_ok=True)
            with open(p,'a') as f: f.write(json.dumps(rec)+'\n')
            ok.append(p)
        except Exception: pass
    return ok
def init_db(db_path, bankroll_sol, i_understand=False, mode='paper', init_logs=None, now=None):

    if not i_understand: raise PermissionError('init_db needs i_understand=True (CLI: --i-understand): a new DB is a new risk ledger')
    if mode not in ('paper','live','backtest'): raise ValueError(mode)
    if os.path.exists(db_path): raise FileExistsError(f'{db_path} exists; refusing to re-initialise (fail-closed design)')
    now=time.time() if now is None else now; real=os.path.realpath(db_path)
    os.makedirs(os.path.dirname(real),exist_ok=True)
    c=sqlite3.connect(db_path,isolation_level=None); c.execute("pragma journal_mode=wal"); c.execute("begin immediate"); _schema(c)
    for k,v in [('schema',SCHEMA_VERSION),('cash',bankroll_sol),('bankroll',bankroll_sol),('mode',mode),('created',now)]:
        c.execute("insert into kv values(?,?)",(k,json.dumps(v)))
    day=dt.datetime.fromtimestamp(now,TZ).date().isoformat()
    if mode!='backtest':
        c.execute("insert into days values(?,?,?,?,?)",(day,bankroll_sol,0.0,1,f'fresh_init_halt: DB initialised {dt.datetime.fromtimestamp(now,TZ):%F %T} UTC+3 -> HALTED until 00:00 UTC+3'))
    c.execute("commit"); c.close()
    rec=dict(ts=now,local=dt.datetime.fromtimestamp(now,TZ).strftime('%F %T'),db=real,bankroll_sol=bankroll_sol,mode=mode,
             halted_day=None if mode=='backtest' else day,pid=os.getpid(),argv=sys.argv[:6])
    logs=init_logs if init_logs is not None else INIT_LOGS
    written=_append_logs(rec,logs)
    if not written: raise RuntimeError('init could not be logged anywhere')
    return rec
class RiskManager:
    def __init__(s, db_path, cfg, bankroll_cap_sol, txc, log=None, wallet_balance_fn=None, mode='paper'):
        s.cfg=cfg; s.txc=txc; s.log=log or (lambda **k: None); s.wallet_balance_fn=wallet_balance_fn; s.mode=mode
        s.tz=dt.timezone(dt.timedelta(hours=cfg['day_boundary_tz_offset_hours']))
        s.R=int(cfg['max_tx_retries']); s.rent=cfg['ata_rent_sol']; s.cap=bankroll_cap_sol; s.fail_closed=''; s.db=None; s._dbf=None
        s.path=os.path.realpath(db_path)
        s._lockf=open(s.path+'.lock','a+')
        try: fcntl.flock(s._lockf,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            s._lockf.seek(0); other=s._lockf.read().strip(); s._lockf.close(); raise InstanceLocked(f'{s.path} is locked by another RiskManager instance ({other})')
        s._lockf.seek(0); s._lockf.truncate(); s._lockf.write(f'pid={os.getpid()} since={time.strftime("%F %T")}\n'); s._lockf.flush()
        if not os.path.exists(s.path):
            s.fail_closed=f'state DB {s.path} missing -> HALT (python3 risk.py init {s.path} <bankroll_sol> --mode {mode} --i-understand)'; return
        try:
            s._dbf=open(s.path,'rb')
            fcntl.flock(s._dbf,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            s._dbf.close(); s._dbf=None; s._release_lock(); raise InstanceLocked(f'{s.path} (inode) is locked by another RiskManager instance')
        try:
            s.db=sqlite3.connect(f'file:{s.path}?mode=rw',uri=True,isolation_level=None,timeout=5)
            s.db.execute("pragma journal_mode=wal")
            if int(s._get('schema'))!=SCHEMA_VERSION: raise ValueError('schema version mismatch')
            if s._get('mode')!=mode: raise ValueError(f"DB mode {s._get('mode')!r} != requested {mode!r}")
            s.bankroll_db=float(s._get('bankroll')); s.cash
        except Exception as e:
            s.fail_closed=f'state DB {s.path} unreadable/foreign ({type(e).__name__}: {e}) -> HALT'
            if s.db: s.db.close()
            s.db=None; return
        gap=s.ledger_gap()
        if abs(gap)>1e-9: s.fail_closed=f'ledger inconsistent by {gap:.9f} SOL (cash != bankroll + realized - open cost) -> HALT; manual review required'

    def _release_lock(s):
        if s._lockf and not s._lockf.closed:
            try: fcntl.flock(s._lockf,fcntl.LOCK_UN)
            finally: s._lockf.close()
    def release(s):
        if s.db: s.db.close(); s.db=None
        if s._dbf:
            try: fcntl.flock(s._dbf,fcntl.LOCK_UN)
            finally: s._dbf.close(); s._dbf=None
        s._release_lock()
    close_db=release
    def __enter__(s): return s
    def __exit__(s,*a): s.release(); return False
    def __del__(s):
        try: s.release()
        except Exception: pass

    def _get(s,k):
        r=s.db.execute("select v from kv where k=?",(k,)).fetchone(); return None if r is None else json.loads(r[0])
    def _set(s,k,v): s.db.execute("insert or replace into kv values(?,?)",(k,json.dumps(v)))
    class _Tx:
        def __init__(s,db): s.db=db
        def __enter__(s): s.db.execute("begin immediate")
        def __exit__(s,et,ev,tb):
            s.db.execute("commit" if et is None else "rollback"); return False
    def tx(s):
        if not s.db: raise RuntimeError('fail-closed: '+s.fail_closed)
        return RiskManager._Tx(s.db)
    @property
    def cash(s): return float(s._get('cash'))
    def open_positions(s):
        if not s.db: return []
        return [dict(zip(['id','mint','entry_t','size','entry_fees','worst','entry_day'],r)) for r in s.db.execute("select * from positions")]
    def open_cost(s): return sum(p['size']+s.rent+p['entry_fees'] for p in s.open_positions())
    def equity(s): return s.cash+sum(p['size']+s.rent for p in s.open_positions())
    def ledger_gap(s):
        realized=s.db.execute("select coalesce(sum(realized),0) from days").fetchone()[0]
        return s.cash-(s.bankroll_db+realized-s.open_cost())
    def wallet_equity(s):

        if s.wallet_balance_fn is None: return None
        return float(s.wallet_balance_fn())+s.open_cost()
    def bankroll(s):
        b=min(s.cap,s.bankroll_db); w=s.wallet_equity()
        return b if w is None else min(b,w)
    def day_of(s,t): return dt.datetime.fromtimestamp(t,s.tz).date().isoformat()
    def _day(s,t):
        d=s.day_of(t); r=s.db.execute("select start_equity,realized,halted,halt_reason from days where day=?",(d,)).fetchone()
        if r is None:
            book_eq=s.bankroll_db+s.db.execute("select coalesce(sum(realized),0) from days").fetchone()[0]
            w=s.wallet_equity()
            start=book_eq if w is None else min(book_eq,w)
            r=(start,0.0,0,''); s.db.execute("insert into days values(?,?,?,?,?)",(d,*r))
            s.log(t=t,action='DAY_START',reason=f'new Istanbul day {d}; start equity {start:.6f} = min(book {book_eq:.6f}, wallet {w if w is None else round(w,6)}); carried-over open {len(s.open_positions())} reserved {sum(p["worst"] for p in s.open_positions()):.6f}')
        D=dict(start_equity=r[0],realized=r[1],halted=bool(r[2]),halt_reason=r[3])
        lim=s.cfg['daily_loss_limit_frac']*D['start_equity']
        if not D['halted'] and D['realized']<=-lim+1e-12:
            D['halted']=True; D['halt_reason']=f'derived: realized {D["realized"]:.6f} <= -{lim:.6f} (flag was clear)'
            s.db.execute("update days set halted=1, halt_reason=? where day=?",(D['halt_reason'],d))
        return d,D
    def day(s,t):
        with s.tx(): return s._day(t)

    def worst_fees(s): return 2*(1+s.R)*s.txc
    def worst_extra(s): return s.worst_fees()+s.rent
    def _allowed(s,t):

        if abs(s.ledger_gap())>1e-9: s.fail_closed='ledger inconsistent -> HALT'; return None,'FAIL_CLOSED: '+s.fail_closed
        bank=s.bankroll()
        d,D=s._day(t); c=s.cfg
        if D['halted']: return None,f'circuit_breaker_halted_for_istanbul_day {d} ({D["halt_reason"]})'
        lim=c['daily_loss_limit_frac']*D['start_equity']; ops=s.open_positions(); eq=min(s.equity(),bank)
        if len(ops)>=c['max_concurrent']: return None,'max_concurrent_positions'
        we=s.worst_extra()
        size=min(c['target_size_sol'],c['max_loss_per_trade_frac']*eq-we)
        if size<c['min_size_sol']: return None,f'per_trade_cap: 5% of equity {c["max_loss_per_trade_frac"]*eq:.5f} - worst-case fees incl. {s.R} retries/side + rent {we:.5f} leaves {size:.5f} < min {c["min_size_sol"]}'
        reserved=sum(p['worst'] for p in ops); room=lim+min(D['realized'],0)-reserved-we
        if room<size:
            if room<c['min_size_sol']: return None,f'daily_risk_budget: limit {lim:.5f} + realized_today {min(D["realized"],0):.5f} - open worst-case {reserved:.5f} (incl. carried-over) - new fees+rent {we:.5f} = {room:.5f} < min size'
            size=room
        exposure=sum(p['size']+s.rent+(1+s.R)*s.txc for p in ops)
        if exposure+size+s.rent+s.worst_fees()+c['fee_reserve_sol']>min(bank,eq)+1e-12:
            return None,f'hard_budget_cap: exposure {exposure:.5f} + new {size+s.rent+s.worst_fees():.5f} + fee reserve {c["fee_reserve_sol"]} > min(bankroll {bank:.5f}, equity {eq:.5f})'
        return size,''
    def size_for_entry(s,t,mint=''):
        if s.fail_closed: return None,'FAIL_CLOSED: '+s.fail_closed
        try:
            with s.tx(): return s._allowed(t)
        except Exception as e:
            if isinstance(e,sqlite3.Error): raise
            return None,f'FAIL_CLOSED: {type(e).__name__}: {e}'
    def open(s,pid,mint,t,size,entry_fees):
        if s.fail_closed: raise RuntimeError('fail-closed: '+s.fail_closed)
        with s.tx():
            allowed,why=s._allowed(t)
            if allowed is None or size>allowed+1e-12: raise Oversize(f'open {pid}: size {size} > allowed {allowed} ({why})')
            if entry_fees>(1+s.R)*s.txc+1e-12: raise Oversize(f'entry fees {entry_fees} > cap {(1+s.R)*s.txc}')
            d,_=s._day(t); worst=size+s.worst_extra()
            s._set('cash',s.cash-size-s.rent-entry_fees); _crash('open:after_cash')
            s.db.execute("insert into positions values(?,?,?,?,?,?,?)",(pid,mint,t,size,entry_fees,worst,d))
    def _book(s,t,pnl):
        d,D=s._day(t); realized=D['realized']+pnl
        s.db.execute("update days set realized=? where day=?",(realized,d))
        lim=s.cfg['daily_loss_limit_frac']*D['start_equity']
        if realized<=-lim+1e-12 and not D['halted']:
            r=f'realized {realized:.6f} <= -{lim:.6f} (10% of start-of-day equity)'
            s.db.execute("update days set halted=1, halt_reason=? where day=?",(r,d))
            s.log(t=t,action='HALT',reason=r+'; no new entries until 00:00 UTC+3')
        return realized
    def book(s,t,pnl,why=''):
        with s.tx():
            s._day(t); s._set('cash',s.cash+pnl); return s._book(t,pnl)
    def failed_entry(s,t,fees):
        with s.tx():
            s._day(t); s._set('cash',s.cash-fees); return s._book(t,-fees)
    def close(s,pid,t,proceeds,exit_fees,exit_ok=True):
        with s.tx():
            s._day(t)
            p=next(p for p in s.open_positions() if p['id']==pid)
            s.db.execute("delete from positions where id=?",(pid,)); _crash('close:after_delete')
            refund=s.rent if exit_ok else 0.0
            if not exit_ok: proceeds=0.0
            s._set('cash',s.cash+proceeds+refund-exit_fees); _crash('close:after_cash')
            pnl=proceeds-p['size']-p['entry_fees']-exit_fees-(s.rent-refund)
            assert pnl>=-p['worst']-1e-12, 'loss exceeded reserved worst case'
            return pnl,s._book(t,pnl)
    def day_table(s):
        if not s.db: return []
        return [dict(zip(['day','start_equity','realized','halted','halt_reason'],r)) for r in s.db.execute("select * from days order by day")]
if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser(description='create a NEW risk state DB (logged; starts HALTED for the rest of today unless --mode backtest)')
    ap.add_argument('cmd',choices=['init']); ap.add_argument('db'); ap.add_argument('bankroll_sol',type=float)
    ap.add_argument('--mode',default='paper',choices=['paper','live','backtest']); ap.add_argument('--i-understand',action='store_true')
    a=ap.parse_args()
    if not a.i_understand: sys.exit('refused: add --i-understand (a new DB is a new risk ledger; it starts HALTED until 00:00 UTC+3)')
    print(json.dumps(init_db(a.db,a.bankroll_sol,i_understand=True,mode=a.mode)))
