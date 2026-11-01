import sys, os, json, tempfile, unittest, subprocess, sqlite3, datetime as dt
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
import risk
from risk import RiskManager, InstanceLocked, Oversize
CFG=json.load(open(os.path.join(ROOT,'config.json'))); BANK=50/102.06; TZ=dt.timezone(dt.timedelta(hours=3))
T=lambda s: dt.datetime.strptime(s,'%Y-%m-%d %H:%M').replace(tzinfo=TZ).timestamp()
def tmp(): return tempfile.mkdtemp()
class TestReinit(unittest.TestCase):
    def test_delete_and_reinit_same_day_stays_halted(self):
        d=tmp(); db=os.path.join(d,'s.sqlite'); log=os.path.join(d,'init_log.jsonl'); now=T('2026-10-30 10:00')
        risk.init_db(db,BANK,i_understand=True,mode='paper',init_logs=[log],now=now-86400)
        rm=RiskManager(db,CFG,BANK,0.000105)
        for k in range(8):
            s,_=rm.size_for_entry(now+k*60)
            if s: rm.open(f'h{k}','m',now+k*60,s,3*0.000105); rm.close(f'h{k}',now+k*60+30,0.0,3*0.000105,exit_ok=False)
        rm.book(now+600,-0.01,'push to limit'); self.assertTrue(rm.day_table()[-1]['halted']); rm.release()
        os.remove(db)
        with self.assertRaises(PermissionError): risk.init_db(db,BANK,init_logs=[log],now=now+700)
        risk.init_db(db,BANK,i_understand=True,mode='paper',init_logs=[log],now=now+700)
        rm=RiskManager(db,CFG,BANK,0.000105); n=0
        for k in range(10):
            s,why=rm.size_for_entry(now+800+k*60)
            if s: n+=1
        self.assertEqual(n,0); self.assertIn('fresh_init_halt',why)
        self.assertIsNotNone(rm.size_for_entry(T('2026-10-31 00:01'))[0])
        L=[json.loads(l) for l in open(log)]; self.assertEqual(len(L),2); self.assertEqual(L[1]['halted_day'],'2026-10-30')
        rm.release(); print(f"\n  re-init same day: 0 new entries ({why[:70]}); init log has {len(L)} entries")
    def test_cli_requires_flag(self):
        db=os.path.join(tmp(),'x.sqlite')
        r=subprocess.run([sys.executable,os.path.join(ROOT,'risk.py'),'init',db,'0.49'],capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0); self.assertIn('--i-understand',r.stderr+r.stdout); self.assertFalse(os.path.exists(db))
    def test_backtest_db_refused_by_paper(self):
        db=os.path.join(tmp(),'b.sqlite'); risk.init_db(db,BANK,i_understand=True,mode='backtest',init_logs=[db+'.log'])
        rm=RiskManager(db,CFG,BANK,0.0001,mode='paper'); self.assertIn('FAIL_CLOSED',rm.size_for_entry(T('2026-10-30 10:00'))[1]); rm.release()
class TestLocks(unittest.TestCase):
    def mk(self):
        d=tmp(); db=os.path.join(d,'s.sqlite'); risk.init_db(db,BANK,i_understand=True,mode='backtest',init_logs=[db+'.log']); return d,db
    def test_symlink_hardlink_relative(self):
        d,db=self.mk(); a=RiskManager(db,CFG,BANK,0.0001,mode='backtest')
        ln=os.path.join(tmp(),'link.sqlite'); os.symlink(db,ln)
        with self.assertRaises(InstanceLocked): RiskManager(ln,CFG,BANK,0.0001,mode='backtest')
        hl=os.path.join(d,'hard.sqlite'); os.link(db,hl)
        with self.assertRaises(InstanceLocked): RiskManager(hl,CFG,BANK,0.0001,mode='backtest')
        sub=os.path.join(d,'sub'); os.makedirs(sub); rel=os.path.join('..','s.sqlite')
        r=subprocess.run([sys.executable,'-c',f"import sys,json;sys.path.insert(0,{ROOT!r});import risk\ntry: risk.RiskManager({rel!r},json.load(open({os.path.join(ROOT,'config.json')!r})),1,0.0001,mode='backtest'); print('OPENED')\nexcept risk.InstanceLocked as e: print('LOCKED')"],cwd=sub,capture_output=True,text=True)
        self.assertIn('LOCKED',r.stdout,r.stderr)
        a.release()
        with RiskManager(ln,CFG,BANK,0.0001,mode='backtest') as b: self.assertIsNotNone(b.db)
        with RiskManager(db,CFG,BANK,0.0001,mode='backtest') as c: self.assertIsNotNone(c.db)
    def test_collector_lock_is_absolute(self):
        r=subprocess.run([sys.executable,'-c',"import collector_v2 as c,os; print(os.path.isabs(c.D), c.D)"],cwd=ROOT,capture_output=True,text=True)
        self.assertIn('True',r.stdout,r.stderr)
        lock=os.path.join(tmp(),'c.lock'); code="import sys,time;sys.path.insert(0,%r);import collector_v2 as c; c.log=lambda *a: print(*a,flush=True); L=c._single_instance_lock(%r); print('GOT',flush=True); time.sleep(6)"
        a=subprocess.Popen([sys.executable,'-c',code%(ROOT,lock)],cwd=ROOT,stdout=subprocess.PIPE,text=True); self.assertEqual(a.stdout.readline().strip(),'GOT')
        b=subprocess.run([sys.executable,'-c',code%(ROOT,lock)],cwd='/tmp',capture_output=True,text=True,timeout=20)
        self.assertEqual(b.returncode,3,b.stdout+b.stderr); a.kill(); a.wait()
class TestOpenRecheckWalletDerivedHalt(unittest.TestCase):
    def mk(self,**k):
        db=os.path.join(tmp(),'s.sqlite'); risk.init_db(db,BANK,i_understand=True,mode='backtest',init_logs=[db+'.log']); return RiskManager(db,CFG,BANK,0.002,mode='backtest',**k)
    def test_open_rejects_oversize(self):
        rm=self.mk(); t=T('2026-10-30 10:00'); s,_=rm.size_for_entry(t)
        with self.assertRaises(Oversize): rm.open('big','m',t,0.05,3*0.002)
        with self.assertRaises(Oversize): rm.open('fee','m',t,s,10*0.002)
        rm.open('ok','m',t,s,3*0.002); self.assertEqual(len(rm.open_positions()),1)
        rm.book(t+1,-0.06,'halt');
        with self.assertRaises(Oversize): rm.open('halted','m',t+2,0.005,0.002)
        rm.release()
    def test_start_equity_min_book_wallet(self):
        rm=self.mk(wallet_balance_fn=lambda: 0.30); d,D=rm.day(T('2026-10-30 10:00'))
        self.assertAlmostEqual(D['start_equity'],0.30)
        rm.release(); rm=self.mk(wallet_balance_fn=lambda: 5.0); d,D=rm.day(T('2026-10-30 10:00')); self.assertAlmostEqual(D['start_equity'],BANK); rm.release()
        def boom(): raise ConnectionError('rpc down')
        rm=self.mk(wallet_balance_fn=boom); self.assertIn('FAIL_CLOSED',rm.size_for_entry(T('2026-10-30 10:00'))[1]); rm.release()
    def test_cleared_halt_flag_is_rederived(self):
        rm=self.mk(); t=T('2026-10-30 10:00'); rm.book(t,-0.05,'x'); p=rm.path; rm.release()
        c=sqlite3.connect(p); c.execute("update days set halted=0"); c.commit(); c.close()
        rm=RiskManager(p,CFG,BANK,0.002,mode='backtest'); s,why=rm.size_for_entry(t+10); self.assertIsNone(s); self.assertIn('halted',why); rm.release()
if __name__=='__main__': unittest.main(verbosity=2)
