import sys, os, json, random, tempfile, unittest, subprocess, datetime as dt, collections
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import risk
from risk import RiskManager, InstanceLocked
import risk as _risk
LOGDIR=tempfile.mkdtemp()
def init_db(db,bank,mode='backtest',**k): return _risk.init_db(db,bank,i_understand=True,mode=mode,init_logs=[os.path.join(LOGDIR,'init_log.jsonl')],**k)
def RM(db,cfg,bank,txc,mode='backtest',**k): return RiskManager(db,cfg,bank,txc,mode=mode,**k)
CFG=json.load(open(os.path.join(ROOT,'config.json')))
TZ=dt.timezone(dt.timedelta(hours=3)); BANK=50/102.06
def T(s):
    f='%Y-%m-%d %H:%M:%S' if s.count(':')==2 else '%Y-%m-%d %H:%M'
    return dt.datetime.strptime(s,f).replace(tzinfo=TZ).timestamp()
def newdb(): return os.path.join(tempfile.mkdtemp(),'s.sqlite')
def mk(txc,db=None,**k):
    db=db or newdb(); init_db(db,BANK); return RM(db,CFG,BANK,txc,**k),db
def run_rm(rm,trades,outcome=lambda i:0.0,exit_ok=lambda i:True,fees=None):
    fees=fees if fees is not None else rm.txc
    ev=[]
    for i,(en,ex) in enumerate(trades): ev.append((T(en) if isinstance(en,str) else en,0,i)); ev.append((T(ex) if isinstance(ex,str) else ex,1,i))
    opened=set()
    for t,kind,i in sorted(ev):
        if kind==0:
            size,why=rm.size_for_entry(t)
            if size: rm.open(f'p{i}',f'm{i}',t,size,fees); opened.add(i)
        elif i in opened:
            p=next(p for p in rm.open_positions() if p['id']==f'p{i}')
            rm.close(f'p{i}',t,p['size']*outcome(i),fees,exit_ok=exit_ok(i))
    return opened
def check_days(tc,rm,msg=''):
    for d in rm.day_table(): tc.assertGreaterEqual(d['realized'],-0.10*d['start_equity']-1e-9,(msg,d))
def check_books(tc,rm):

    if not rm.open_positions(): tc.assertAlmostEqual(rm.cash-BANK,sum(d['realized'] for d in rm.day_table()),places=9)

SYN=[('2026-10-29 23:50','2026-10-30 00:10'),('2026-10-29 23:52','2026-10-30 00:12'),('2026-10-30 00:20','2026-10-30 00:40'),('2026-10-30 00:22','2026-10-30 00:42')]
def legacy_v2(trades,TXC=0.0001):
    day=lambda t: dt.datetime.fromtimestamp(t,TZ).date()
    cash=BANK; open_pos=[]; day_pnl=collections.defaultdict(float); day_start={}; n=0; true=collections.defaultdict(float)
    for en,ex in trades:
        for p in [p for p in open_pos if p['ex']<=en]: cash+=p['size']+p['pnl']; day_pnl[p['day']]+=p['pnl']; true[day(p['ex'])]+=p['pnl']; open_pos.remove(p)
        d=day(en); eq=cash+sum(p['size'] for p in open_pos); day_start.setdefault(d,eq); lim=0.1*day_start[d]
        if day_pnl[d]-sum(0.02+2*TXC for p in open_pos if p['day']==d)-(0.02+2*TXC)<-lim: continue
        cash-=0.02; open_pos.append(dict(ex=ex,pnl=-0.02-2*TXC,size=0.02,day=d)); n+=1
    for p in open_pos: day_pnl[p['day']]+=p['pnl']; true[day(p['ex'])]+=p['pnl']
    return n,{str(k):v/BANK for k,v in day_pnl.items()},{str(k):v/BANK for k,v in true.items()}
class TestDailyBreaker(unittest.TestCase):
    def test_a2_synthetic_legacy_bug(self):
        n,booked,true=legacy_v2([(T(a),T(b)) for a,b in SYN]); self.assertEqual(n,4)
        self.assertAlmostEqual(min(booked.values())*100,-8.25,places=1); self.assertAlmostEqual(true['2026-10-30']*100,-16.49,places=1)
    def test_a2_synthetic_fixed(self):
        rm,_=mk(0.0001); run_rm(rm,SYN); check_days(self,rm); check_books(self,rm)
    def test_a3_midnight_close_first_event(self):

        for txc in (0.000105,0.002):
            rm,_=mk(txc); tr=[(T('2026-10-29 23:59:59')+k*0.01,T('2026-10-30 00:00:00')) for k in range(5)]
            tr+=[(T(f'2026-10-30 02:{k*10:02d}:00'),T(f'2026-10-30 02:{k*10+5:02d}:00')) for k in range(6)]
            run_rm(rm,tr,fees=(1+rm.R)*txc,exit_ok=lambda i:False); check_days(self,rm,txc); check_books(self,rm)
            d=[d for d in rm.day_table() if d['day']=='2026-10-30'][0]
            print(f"\n  CASE2 txc={txc}: 30 Oct start {d['start_equity']:.5f} realized {100*d['realized']/d['start_equity']:.2f}% (limit -10%)")
    def test_a3_case1_and_restart_across_midnight(self):
        txc=0.000105; rm,db=mk(txc)
        run_rm(rm,[(T('2026-10-29 23:58:00'),T('2026-10-30 00:05:00')+1e6)][:0])
        for i,t in enumerate([T('2026-10-29 23:58:00'),T('2026-10-29 23:58:30')]):
            s,_=rm.size_for_entry(t); rm.open(f'r{i}','m',t,s,3*txc)
        rm.close_db(); rm=RM(db,CFG,BANK,txc)
        rm.close('r0',T('2026-10-30 00:05:00'),0.0,3*txc,exit_ok=False); rm.close('r1',T('2026-10-30 00:06:00'),0.0,3*txc,exit_ok=False)
        run_rm(rm,[(T(f'2026-10-30 03:{k*10:02d}:00'),T(f'2026-10-30 03:{k*10+5:02d}:00')) for k in range(6)],fees=3*txc,exit_ok=lambda i:False)
        check_days(self,rm); check_books(self,rm)
    def test_random_property_limit_never_breached(self):
        rnd=random.Random(7)
        for trial in range(25):
            rm,_=mk(rnd.choice([0.000105,0.002,0.005])); t0=T('2026-10-29 20:00'); tr=[]
            for i in range(200):
                en=rnd.choice([t0+rnd.uniform(0,3*86400), T('2026-10-29 23:59:59')+rnd.uniform(0,0.9)])
                tr.append((en,rnd.choice([en+rnd.choice([30,600,3600,6*3600]), T('2026-10-30 00:00:00')])) if en<T('2026-10-30 00:00:00') else (en,en+rnd.choice([30,600,3600])))
            run_rm(rm,tr,outcome=lambda i:rnd.choice([0,0,0.3,0.7,1.0,1.5,3.0]),exit_ok=lambda i:rnd.random()>0.3,fees=(1+rm.R)*rm.txc)
            check_days(self,rm,trial); check_books(self,rm); rm.close_db()
class TestPerTrade(unittest.TestCase):
    def test_worst_case_incl_retries_and_rent_le_5pct(self):
        for txc in (0.000105,0.002,0.005):
            rm,_=mk(txc); size,why=rm.size_for_entry(T('2026-10-29 20:00'))
            if size is None: self.assertIn('per_trade_cap',why); continue
            self.assertLessEqual(size+rm.worst_fees()+rm.rent,0.05*rm.equity()+1e-12)
    def test_failed_exit_loses_rent_and_stays_within_5pct(self):

        txc=0.002; rm,_=mk(txc); t=T('2026-10-29 20:00'); eq=rm.equity(); s,_=rm.size_for_entry(t)
        rm.open('a','m',t,s,3*txc); pnl,_=rm.close('a',t+60,0.0,3*txc,exit_ok=False)
        self.assertAlmostEqual(pnl,-(s+6*txc+rm.rent)); self.assertGreaterEqual(pnl/eq,-0.05-1e-12)
        print(f"\n  failed exit @0.002: loss {100*pnl/eq:.2f}% of equity (incl. rent {rm.rent})")
class TestPersistenceAndSafety(unittest.TestCase):
    def test_halt_and_day_pnl_survive_restart(self):
        rm,db=mk(0.0001); t=T('2026-10-30 10:00')
        s,_=rm.size_for_entry(t); rm.open('a','m',t,s,0.0001); rm.close('a',t+60,0.0,0.0001); rm.book(t+120,-0.10*BANK,'forced')
        before=rm.day_table(); self.assertTrue(before[0]['halted']); cash=rm.cash; rm.close_db()
        rm2=RM(db,CFG,BANK,0.0001); self.assertEqual(rm2.day_table(),before); self.assertAlmostEqual(rm2.cash,cash)
        self.assertIn('circuit_breaker_halted',rm2.size_for_entry(t+3600)[1]); self.assertIsNotNone(rm2.size_for_entry(T('2026-10-31 00:01'))[0])
    def test_open_positions_restored_and_reserved(self):
        rm,db=mk(0.0001); t=T('2026-10-30 23:55'); s,_=rm.size_for_entry(t); rm.open('x','m',t,s,0.0001); rm.close_db()
        rm2=RM(db,CFG,BANK,0.0001); self.assertEqual(len(rm2.open_positions()),1)
        s2,_=rm2.size_for_entry(t+600); d=rm2.day_table()[-1]
        self.assertLessEqual((s2 or 0)+rm2.worst_extra()+rm2.open_positions()[0]['worst'],0.1*d['start_equity']+1e-12)
    def test_fail_closed_missing_db(self):
        db=newdb(); rm=RM(db,CFG,BANK,0.0001); s,why=rm.size_for_entry(T('2026-10-30 10:00'))
        self.assertIsNone(s); self.assertTrue(why.startswith('FAIL_CLOSED')); self.assertFalse(os.path.exists(db))
        self.assertRaises(RuntimeError,rm.open,'a','m',0,0.01,0)
    def test_fail_closed_deleted_after_halt_and_corrupt(self):
        rm,db=mk(0.0001); t=T('2026-10-30 10:00'); rm.book(t,-0.06,'x'); self.assertTrue(rm.day_table()[0]['halted']); rm.close_db()
        os.remove(db); [os.remove(db+x) for x in ('-wal','-shm') if os.path.exists(db+x)]
        rm=RM(db,CFG,BANK,0.0001); self.assertIn('FAIL_CLOSED',rm.size_for_entry(t+60)[1]); rm.close_db()
        open(db,'wb').write(b'garbage'*100); rm=RM(db,CFG,BANK,0.0001); self.assertIn('unreadable',rm.size_for_entry(t+60)[1]); rm.close_db()
        self.assertRaises(FileExistsError,init_db,db,BANK)
    def test_single_instance_lock(self):
        rm,db=mk(0.0001)
        with self.assertRaises(InstanceLocked): RM(db,CFG,BANK,0.0001)
        out=subprocess.run([sys.executable,'-c',f"import sys,json;sys.path.insert(0,{ROOT!r});import risk;c=json.load(open({os.path.join(ROOT,'config.json')!r}))\ntry: risk.RiskManager({db!r},c,1,0.0001,mode='backtest')\nexcept risk.InstanceLocked as e: print('LOCKED',e)"],capture_output=True,text=True)
        self.assertIn('LOCKED',out.stdout); rm.close_db(); RM(db,CFG,BANK,0.0001).close_db()
    def test_wallet_balance_hook(self):
        db=newdb(); init_db(db,BANK)
        rm=RM(db,CFG,BANK,0.0001,wallet_balance_fn=lambda: 0.3); self.assertAlmostEqual(rm.bankroll(),0.3)
        s,why=rm.size_for_entry(T('2026-10-30 10:00')); self.assertLessEqual(s+rm.worst_extra(),0.05*0.3+1e-12)
        rm.close_db(); rm=RM(db,CFG,BANK,0.0001,wallet_balance_fn=lambda: 0.02)
        s,why=rm.size_for_entry(T('2026-10-30 10:00')); self.assertIsNone(s); self.assertIn('per_trade_cap',why); rm.close_db()
        def boom(): raise ConnectionError('rpc down')
        rm=RM(db,CFG,BANK,0.0001,wallet_balance_fn=boom); self.assertIn('FAIL_CLOSED',rm.size_for_entry(T('2026-10-30 10:00'))[1]); rm.close_db()
CRASH_CHILD='''
import sys,os,json; sys.path.insert(0,{root!r}); import risk
c=json.load(open({cfg!r})); rm=risk.RiskManager({db!r},c,{bank},{txc},mode='backtest')
def hook(p):
    if p=={point!r}: os._exit(9)          # hard crash mid-transaction (no commit, no cleanup)
risk._CRASH_HOOK=hook
if {point!r}.startswith('open'): rm.open('new','m',{t},0.01,{fee})
else: rm.close('x0',{t},0.0,{fee},exit_ok=False)
'''
class TestCrashInjection(unittest.TestCase):
    def test_partial_foreign_write_is_detected(self):

        txc=0.000105; rm,db=mk(txc); t=T('2026-10-30 10:00')
        for k in range(2): s,_=rm.size_for_entry(t); rm.open(f'x{k}','m',t,s,3*txc)
        for k in range(2):
            rm.db.execute("delete from positions where id=?",(f'x{k}',)); rm.db.execute("update kv set v=? where k='cash'",(json.dumps(rm.cash+rm.rent-3*txc),))
        rm.close_db(); rm=RM(db,CFG,BANK,txc); s,why=rm.size_for_entry(t+3600)
        self.assertIsNone(s); self.assertIn('ledger inconsistent',why); print(f"\n  CASE4-style partial write: {why[:90]}"); rm.close_db()
    def test_crash_inside_close_and_open_is_atomic(self):
        txc=0.000105; t=T('2026-10-30 10:00')
        for point in ('close:after_delete','close:after_cash','open:after_cash'):
            rm,db=mk(txc)
            for k in range(1 if point.startswith('open') else 2): s,_=rm.size_for_entry(t); rm.open(f'x{k}','m',t,s,3*txc)
            snap=(rm.cash,rm.open_positions(),rm.day_table()); rm.close_db()
            r=subprocess.run([sys.executable,'-c',CRASH_CHILD.format(root=ROOT,cfg=os.path.join(ROOT,'config.json'),db=db,bank=BANK,txc=txc,point=point,t=t+100,fee=3*txc)])
            self.assertEqual(r.returncode,9,point)
            rm=RM(db,CFG,BANK,txc)
            self.assertEqual((rm.cash,rm.open_positions(),rm.day_table()),snap,point)
            for k in range(10):
                tt=t+3600+k*60; s,_=rm.size_for_entry(tt)
                if s: rm.open(f'y{k}','m',tt,s,3*txc); rm.close(f'y{k}',tt+30,0.0,3*txc,exit_ok=False)
            for p in rm.open_positions(): rm.close(p['id'],t+9000,0.0,3*txc,exit_ok=False)
            check_days(self,rm,point); check_books(self,rm); self.assertAlmostEqual(rm.ledger_gap(),0,places=9)
            print(f"\n  crash at {point}: rolled back; day loss after continuing {100*(BANK-rm.cash)/BANK:.2f}% of $50 (= booked {100*rm.day_table()[0]['realized']/BANK:.2f}%)")
            rm.close_db()
if __name__=='__main__': unittest.main(verbosity=2)
