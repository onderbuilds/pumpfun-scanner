import sys, os, unittest
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT); os.chdir(ROOT)
import analyze as A
K=30*1.073e9; V0=1.073e9; RES=V0-793.1e6
def trade(m,i,vtok,user,buy,ts):
    vs=K/vtok; return dict(e='T',mint=m,sol=0.5,tok=1e6,buy=buy,user=user,ts=ts,rt=ts+0.3,slot=1000+i,sig=f's{i}',vsol=vs,vtok=vtok,rsol=vs-30,rtok=max(0.0,vtok-RES),fee_bps=95)
def synthetic(m='SYNTHgrad'):
    t0=1793300000; T=[trade(m,0,V0*0.97,'dev',True,t0)]
    prog=[0.05,0.10,0.20,0.30,0.40,0.50,0.58,0.61,0.62,0.63]
    for k,p in enumerate(prog): T.append(trade(m,k+1,V0-p*793.1e6,f'u{k}',True,t0+5+k*5))
    sell=trade(m,2,T[2]['vtok']+1e6,'seller',False,t0+12); sell['rt']+=0.1; T.append(sell)
    T.append(trade(m,50,RES,'whale',True,t0+80))
    T.sort(key=lambda t:(t['slot'],t['rt']))
    return m,dict(e='C',mint=m,name='SYN',symbol='SYN',user='dev',creator='dev',ts=t0,rt=t0+0.2,slot=999),T,dict(e='G',mint=m,ts=t0+80)
class TestPumpSwapExit(unittest.TestCase):
    def setUp(self):
        m,c,T,g=synthetic(); A.creates={m:c}; A.trades={m:T}; A.grads={m:g}; A.live={m:dict(mint=m,rt=c['rt']+0.6,ok=True,reason='')}
        A.eligible=[m]; A.MINTMODE='live'; A.WINDOW=1800; A.DEC=None; self.m=m; self.T=T
    def test_graduated_position_exits_via_pumpswap(self):
        recs=A.simulate('C_momentum_60pct',0.0,log=False); self.assertEqual(len(recs),1); r=recs[0]
        self.assertEqual(r['venue'],'pumpswap'); self.assertTrue(r['reason'].startswith('graduated_exit_pumpswap'))
        last=[t for t in self.T if t['rtok']<=0][0]; self.assertAlmostEqual(r['pool_sol'],last['rsol']-A.GRADFEE)
        got=A.buy_tokens(r['evs'],r['evt'],0.02); pr=A.proceeds(r,0.02)
        f=A.CFG['pumpswap_migration_presell_frac']; ps,pt=r['pool_sol']*(1-f),A.POOL_TOK/(1-f)
        self.assertAlmostEqual(pr,ps*got/(pt+got)*(1-A.PSFEE))
        curve_px=A.sell_curve(last['vsol'],last['vtok'],got); no_presell=A.sell_pool(r['pool_sol'],got,presell=0.0)
        self.assertLess(pr,no_presell); self.assertAlmostEqual(no_presell/curve_px,1.0,delta=0.03)
        print(f"\n  synthetic graduation: curve-price exit {curve_px:.5f} SOL, PumpSwap no pre-sell {no_presell:.5f}, with {int(100*f)}% pre-sell {pr:.5f} (entry 0.02)")
    def test_failed_exit_at_migration_valued_zero(self):
        recs=A.simulate('C_momentum_60pct',0.999999,log=False)
        if recs and recs[0]['entered']: self.assertEqual(A.proceeds(recs[0],0.02),0.0)
if __name__=='__main__': unittest.main(verbosity=2)
