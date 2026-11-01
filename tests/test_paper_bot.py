import sys, os, json, tempfile, unittest, subprocess, importlib
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0,ROOT); sys.path.insert(0,os.path.dirname(__file__))
import risk
from test_sim_pumpswap import synthetic
class FakeGate:
    def __init__(s,ok=True,reason='PASS'): s.calls=[]; s.ok=ok; s.reason=reason
    def check(s,m): s.calls.append(m); return s.ok,s.reason,{'latency_s':0.01}
def mkbot(gate,pfail='0.0'):
    d=tempfile.mkdtemp(); os.makedirs(f'{d}/state')
    os.environ.update(PAPER_DIR=d,PAPER_LIVEPRICE='0',PAPER_PFAIL=pfail,PAPER_RULES='C_momentum_60pct')
    import paper_bot; importlib.reload(paper_bot)
    bank=50/102.06
    risk.init_db(f'{d}/state/C_momentum_60pct.sqlite',bank,i_understand=True,mode='paper',init_logs=[f'{d}/init.jsonl'],now=0)
    return paper_bot.Bot(gate=gate),d
def feed(bot,shift_rt=True):
    m,c,T,g=synthetic('PBmint'); import time
    base=time.time()-c['ts']-100
    c=dict(c,rt=c['rt']+base); bot.on_line(json.dumps(c))
    for t in T:
        t=dict(t,rt=t['rt']+base+200)
        if t['rtok']<=0: continue
        bot.on_line(json.dumps(t))
    return m
def decisions(d): return [json.loads(l) for l in open(f'{d}/decisions.jsonl')]
class TestPaperBot(unittest.TestCase):
    def test_gate_pass_enters_paper_fill(self):
        g=FakeGate(); bot,d=mkbot(g); m=feed(bot)
        A=[x['action'] for x in decisions(d) if x.get('mint')==m]
        self.assertEqual(g.calls,[m]); self.assertIn('ENTRY_GATE',A); self.assertIn('ENTER',A)
        self.assertEqual(len(bot.rm['C_momentum_60pct'].open_positions()),1); [r.release() for r in bot.rm.values()]
    def test_gate_reject_blocks_entry(self):
        for ok,reason in [(False,'REJECT mint_authority_set'),(False,'REJECT no_rpc_response (timeout)')]:
            g=FakeGate(ok,reason); bot,d=mkbot(g); m=feed(bot)
            A=[x['action'] for x in decisions(d) if x.get('mint')==m]
            self.assertIn('ENTRY_GATE',A); self.assertNotIn('ENTER',A); self.assertEqual(bot.rm['C_momentum_60pct'].open_positions(),[])
            [r.release() for r in bot.rm.values()]
    def test_second_bot_refuses(self):
        g=FakeGate(); bot,d=mkbot(g)
        r=subprocess.run([sys.executable,'-c',"import paper_bot; paper_bot.Bot()"],cwd=ROOT,env=dict(os.environ,PAPER_DIR=d,PAPER_LIVEPRICE='0'),capture_output=True,text=True,timeout=30)
        self.assertEqual(r.returncode,3,r.stdout+r.stderr); [x.release() for x in bot.rm.values()]
if __name__=='__main__': unittest.main(verbosity=2)
