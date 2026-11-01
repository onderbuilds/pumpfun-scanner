import sys, os, unittest, socket
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from entry_gate import EntryGate
T22="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
def acct(mint_auth=None,freeze=None,supply="1000000000000000",ext=('metadataPointer','tokenMetadata'),owner=T22):
    return {"owner":owner,"data":{"parsed":{"info":{"mintAuthority":mint_auth,"freezeAuthority":freeze,"supply":supply,"decimals":6,
            "extensions":[{"extension":e} for e in ext]}}}}
def mock(value=None,raise_=None,raw=None):
    def rpc(method,params,timeout=None):
        assert method=='getAccountInfo' and params[1]['commitment']=='confirmed'
        if raise_: raise raise_
        if raw is not None: return raw
        return {"jsonrpc":"2.0","result":{"context":{"slot":1},"value":value}}
    return rpc
class TestEntryGate(unittest.TestCase):
    def g(self,**k): return EntryGate(rpc=mock(**k)).check('MintXYZ')
    def test_pass(self): ok,r,d=self.g(value=acct()); self.assertTrue(ok); self.assertEqual(r,'PASS')
    def test_mint_authority(self): ok,r,_=self.g(value=acct(mint_auth='Evil1')); self.assertFalse(ok); self.assertIn('mint_authority_set',r)
    def test_freeze_authority(self): ok,r,_=self.g(value=acct(freeze='Evil2')); self.assertFalse(ok); self.assertIn('freeze_authority_set',r)
    def test_bad_extension(self): ok,r,_=self.g(value=acct(ext=('transferHook',))); self.assertFalse(ok); self.assertIn('transferHook',r)
    def test_2b_supply(self): ok,r,_=self.g(value=acct(supply="2000000000000000")); self.assertFalse(ok)
    def test_unknown_program(self): ok,r,_=self.g(value=acct(owner='Fake111')); self.assertFalse(ok)
    def test_account_missing(self): ok,r,_=self.g(value=None); self.assertFalse(ok); self.assertIn('account_not_found',r)
    def test_timeout_rejects(self): ok,r,_=self.g(raise_=socket.timeout('t')); self.assertFalse(ok); self.assertIn('no_rpc_response',r)
    def test_conn_error_rejects(self): ok,r,_=self.g(raise_=ConnectionError('x')); self.assertFalse(ok); self.assertIn('no_rpc_response',r)
    def test_rpc_error_rejects(self): ok,r,_=self.g(raw={"error":{"code":429,"message":"Too many"}}); self.assertFalse(ok); self.assertIn('rpc_error',r)
    def test_malformed_rejects(self): ok,r,_=self.g(raw={"result":"weird"}); self.assertFalse(ok); self.assertIn('malformed',r)
if __name__=='__main__': unittest.main(verbosity=2)
